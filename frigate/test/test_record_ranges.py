"""Tests for the precomputed playback-timeline ranges.

Two things are being guarded here. First, that the SQL gap-and-islands merge
agrees exactly with the obvious Python merge -- including the awkward cases the
consumer of this API called out: overlapping segments, and ranges straddling the
window edge. Second, that precomputing does not change the answer: a range read
back out of the rollup and re-merged at the caller's gap must equal merging the
raw segments at that gap, or the timeline quietly lies about where footage is.
"""

import datetime
import random

from frigate.config import FrigateConfig
from frigate.models import (
    Previews,
    RecordingRangeCoverage,
    RecordingRanges,
    Recordings,
    ReviewSegment,
    UserReviewStatus,
)
from frigate.record.ranges import (
    MATERIALIZED_GAP,
    ROLLUP_MIN_ADVANCE,
    backfill_ranges,
    live_ranges,
    merge_ranges,
    recording_ranges,
    roll_up_ranges,
    trim_ranges,
)
from frigate.record.variants import recordings_overlap_clause
from frigate.test.http_api.base_http_test import BaseTestHttp

DAY = 24 * 60 * 60
SEGMENT = 10.0


def reference_merge(segments, gap):
    """The obvious merge, written independently of the SQL under test.

    Deliberately naive apart from the running max: this is the shape the API
    consumer implements client-side, and it is what the SQL has to match.
    """
    merged = []

    for start, end in sorted(segments):
        if merged and start - merged[-1][1] <= gap:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    return [(s, e) for s, e in merged]


class RangesTestBase(BaseTestHttp):
    T = 1700000000.0

    def setUp(self):
        super().setUp(
            [
                Recordings,
                RecordingRanges,
                RecordingRangeCoverage,
                ReviewSegment,
                UserReviewStatus,
                Previews,
            ]
        )
        self.config = FrigateConfig(
            **{
                "mqtt": {"host": "mqtt"},
                "record": {"enabled": True, "continuous": {"days": 7}},
                "cameras": {
                    "back": {
                        "ffmpeg": {
                            "inputs": [
                                {
                                    "path": "rtsp://10.0.0.1:554/main",
                                    "roles": ["detect", "record"],
                                    "record_variant": "main",
                                },
                                {
                                    "path": "rtsp://10.0.0.1:554/sub",
                                    "roles": ["record"],
                                    "record_variant": "sub",
                                },
                            ]
                        },
                        "detect": {"height": 1080, "width": 1920, "fps": 5},
                    }
                },
            }
        )
        self._row = 0

    def add(self, start, end, camera="back", variant="sub"):
        self._row += 1
        Recordings.insert(
            id=f"r{self._row}",
            path=f"/rec/{self._row}",
            camera=camera,
            start_time=start,
            end_time=end,
            duration=end - start,
            motion=0,
            objects=0,
            variant=variant,
        ).execute()
        return (start, end)

    def add_run(self, start, count, camera="back", variant="sub", jitter=True):
        """A run of abutting 10s segments with realistic encoder jitter."""
        rng = random.Random(f"{camera}{variant}{start}")
        segments = []
        t = start

        for _ in range(count):
            offset = rng.choice([0.0, 0.15, 0.4]) if jitter else 0.0
            seg_start = t + offset
            seg_end = seg_start + SEGMENT - (rng.random() * 0.001 if jitter else 0)
            segments.append(self.add(seg_start, seg_end, camera, variant))
            t += SEGMENT

        return segments


class TestRangeMergeCorrectness(RangesTestBase):
    def test_ranges_match_python_merge(self):
        segments = []
        segments += self.add_run(self.T, 60)
        segments += self.add_run(self.T + 1200, 90)
        segments += self.add_run(self.T + 3600, 30)

        for gap in (1.0, 3.0, 30.0):
            for window in ((self.T, self.T + 5000), (self.T + 500, self.T + 2000)):
                after, before = window
                expected = reference_merge(
                    [s for s in segments if s[0] <= before and s[1] >= after], gap
                )
                actual = live_ranges(["back"], after, before, "sub", gap)["back"]
                assert actual == expected, (gap, window, actual, expected)

    def test_ranges_merge_uses_max_end(self):
        # a long segment whose end outlasts its successors -- taking the last
        # row's end instead of the running max would rewind the range and lose
        # the tail, which is real footage the timeline would stop offering
        self.add(self.T + 100, self.T + 160)
        self.add(self.T + 110, self.T + 120)
        self.add(self.T + 120, self.T + 130)
        self.add(self.T + 200, self.T + 210)

        actual = live_ranges(["back"], self.T, self.T + 1000, "sub", 3.0)["back"]

        assert actual == [(self.T + 100, self.T + 160), (self.T + 200, self.T + 210)]
        # the value a last-end merge would have produced
        assert actual[0][1] != self.T + 130

    def test_ranges_unclipped_at_window_edges(self):
        self.add(self.T - 60, self.T + 10)  # starts before the window
        self.add(self.T + 10, self.T + 20)
        self.add(self.T + 990, self.T + 1060)  # ends after the window

        actual = live_ranges(["back"], self.T, self.T + 1000, "sub", 3.0)["back"]

        assert actual[0][0] < self.T, actual
        assert actual[-1][1] > self.T + 1000, actual

    def test_ranges_gap_parameter(self):
        self.add_run(self.T, 3, jitter=False)
        self.add_run(self.T + 100, 3, jitter=False)

        tight = live_ranges(["back"], self.T, self.T + 1000, "sub", 3.0)["back"]
        loose = live_ranges(["back"], self.T, self.T + 1000, "sub", 100.0)["back"]

        assert len(tight) == 2
        assert len(loose) == 1

    def test_ranges_empty_is_empty_list(self):
        result = live_ranges(["back"], self.T, self.T + 1000, "sub", 3.0)

        assert result == {"back": []}

    def test_ranges_multi_camera_partitions(self):
        self.add_run(self.T, 5, camera="back")
        self.add(self.T + 5000, self.T + 5010, camera="front")

        result = live_ranges(["back", "front"], self.T, self.T + 9000, "sub", 3.0)

        assert len(result["back"]) == 1
        assert len(result["front"]) == 1
        assert result["back"][0][0] == self.T
        assert result["front"][0][0] == self.T + 5000

    def test_ranges_filter_by_variant(self):
        self.add_run(self.T, 5, variant="sub")
        self.add(self.T + 5000, self.T + 5010, variant="main")

        sub = live_ranges(["back"], self.T, self.T + 9000, "sub", 3.0)["back"]
        main = live_ranges(["back"], self.T, self.T + 9000, "main", 3.0)["back"]
        every = live_ranges(["back"], self.T, self.T + 9000, "all", 3.0)["back"]

        assert len(sub) == 1
        assert len(main) == 1
        assert len(every) == 2

    def test_predicate_matches_overlap_clause(self):
        # the merge SQL hand-writes the seekable overlap predicate rather than
        # reusing the peewee helper, so pin the two together
        self.add_run(self.T, 40)
        after, before = self.T + 137, self.T + 296

        via_clause = {
            (r.start_time, r.end_time)
            for r in Recordings.select().where(
                Recordings.camera == "back",
                Recordings.variant == "sub",
                recordings_overlap_clause(after, before),
            )
        }
        # gap 0 with no jitter tolerance keeps every segment its own range, so
        # the merged output is exactly the matched row set
        via_sql = live_ranges(["back"], after, before, "sub", -1.0)["back"]

        assert set(via_sql) == via_clause

    def test_query_plan_seeks_composite_index(self):
        for camera in ("back", "front"):
            for variant in ("main", "sub"):
                self.add_run(self.T, 50, camera=camera, variant=variant)

        database = Recordings._meta.database
        database.execute_sql("ANALYZE")

        from frigate.record.ranges import _MERGE_SQL

        sql = _MERGE_SQL.format(cameras="?", variant=" AND variant = ?")
        plan = " ".join(
            str(row)
            for row in database.execute_sql(
                "EXPLAIN QUERY PLAN " + sql,
                ["back", "sub", self.T + 1000, self.T, self.T - 600, 3.0],
            ).fetchall()
        )

        assert "recordings_camera_variant_start_time_end_time" in plan, plan
        # a two-sided start_time range in the plan detail proves the seek
        assert "start_time<" in plan and "start_time>" in plan, plan


class TestRematerializedMerge(RangesTestBase):
    """Precomputing must not change the answer.

    Ranges are stored merged at MATERIALIZED_GAP and re-merged at whatever the
    caller asks for. If that is not equivalent to merging the raw segments at
    the caller's gap, the timeline silently reports the wrong coverage.
    """

    def test_rematerialized_merge_equals_direct_merge(self):
        rng = random.Random(20260908)
        segments = []
        t = self.T

        # a day-ish of runs separated by holes of every interesting size:
        # sub-second jitter, a couple of seconds, and real outages
        for _ in range(25):
            run_length = rng.randint(1, 40)
            segments += self.add_run(t, run_length)
            t += run_length * SEGMENT
            t += rng.choice([0.2, 0.5, 1.5, 2.5, 4.0, 60.0, 900.0])

        after, before = self.T, t + 100
        stored = live_ranges(["back"], after, before, "sub", MATERIALIZED_GAP)["back"]

        for gap in (1.0, 2.0, 3.0, 5.0, 30.0, 120.0, 1000.0):
            direct = reference_merge(segments, gap)
            rematerialized = merge_ranges(stored, gap)
            assert rematerialized == direct, (gap, rematerialized, direct)

    def test_merge_ranges_keeps_running_max(self):
        # same trap as the SQL: a wholly-contained later range must not pull the
        # merged end backwards
        assert merge_ranges([(0.0, 60.0), (10.0, 20.0)], 3.0) == [(0.0, 60.0)]


class TestRollup(RangesTestBase):
    def test_rollup_advances_coverage_and_serves_from_table(self):
        now = datetime.datetime.now().timestamp()
        # inside the first rollup window (ROLLUP_INITIAL_WINDOW back from the
        # settled watermark), so the very first tick materializes it
        start = now - 2400
        self.add_run(start, 30)

        roll_up_ranges(self.config, now=now)

        coverage = RecordingRangeCoverage.get(
            RecordingRangeCoverage.camera == "back",
            RecordingRangeCoverage.variant == "sub",
        )
        assert coverage.covered_to <= now
        assert RecordingRanges.select().count() >= 1

        served = recording_ranges(["back"], start - 10, start + 400, "sub", 3.0)["back"]
        direct = live_ranges(["back"], start - 10, start + 400, "sub", 3.0)["back"]
        assert served == direct

    def test_first_rollup_is_bounded(self):
        # a fresh install must not scan its whole retained history on tick one
        now = datetime.datetime.now().timestamp()
        self.add_run(now - 5 * DAY, 10)
        self.add_run(now - 2400, 10)

        roll_up_ranges(self.config, now=now)

        coverage = RecordingRangeCoverage.get(
            RecordingRangeCoverage.camera == "back",
            RecordingRangeCoverage.variant == "sub",
        )
        assert coverage.covered_from > now - DAY
        # the old footage is outside coverage, so it is still served live
        old = recording_ranges(["back"], now - 5 * DAY, now - 5 * DAY + 200, "sub", 3.0)
        assert old["back"]

    def test_rollup_skips_cameras_with_nothing_new_settled(self):
        # the 60s tick must not rewrite every camera's trailing range every
        # minute -- those writes queue behind segment inserts on the same db
        now = datetime.datetime.now().timestamp()
        self.add_run(now - 2400, 30)

        roll_up_ranges(self.config, now=now)
        first = RecordingRangeCoverage.get(
            RecordingRangeCoverage.camera == "back",
            RecordingRangeCoverage.variant == "sub",
        ).covered_to

        # one tick later: nothing meaningful has settled, so nothing is written
        roll_up_ranges(self.config, now=now + 60)
        assert (
            RecordingRangeCoverage.get(
                RecordingRangeCoverage.camera == "back",
                RecordingRangeCoverage.variant == "sub",
            ).covered_to
            == first
        )

        # once enough has settled it advances again
        roll_up_ranges(self.config, now=now + ROLLUP_MIN_ADVANCE + 60)
        assert (
            RecordingRangeCoverage.get(
                RecordingRangeCoverage.camera == "back",
                RecordingRangeCoverage.variant == "sub",
            ).covered_to
            > first
        )

    def test_rollup_never_materializes_unsettled_window(self):
        now = datetime.datetime.now().timestamp()
        # footage right up to "now" -- still being written, so a segment could
        # still land inside it
        self.add_run(now - 120, 12)

        roll_up_ranges(self.config, now=now)

        coverage = RecordingRangeCoverage.get_or_none(
            RecordingRangeCoverage.camera == "back",
            RecordingRangeCoverage.variant == "sub",
        )
        assert coverage is not None
        assert coverage.covered_to < now

        for row in RecordingRanges.select():
            assert row.end_time <= coverage.covered_to + 600

    def test_rollup_recomputes_trailing_overlap(self):
        now = datetime.datetime.now().timestamp()
        first_tick = now - 1800
        self.add_run(first_tick - 1200, 30)

        roll_up_ranges(self.config, now=first_tick)
        before_ids = {r.id for r in RecordingRanges.select()}
        assert before_ids

        # a segment lands late, extending the run that was already rolled up
        last_end = max(r.end_time for r in RecordingRanges.select())
        self.add(last_end + 0.2, last_end + 10.2)

        roll_up_ranges(self.config, now=now)

        served = recording_ranges(["back"], first_tick - 2000, now, "sub", 3.0)["back"]
        direct = live_ranges(["back"], first_tick - 2000, now, "sub", 3.0)["back"]
        assert served == direct

    def test_partial_coverage_stitches_live_tail(self):
        now = datetime.datetime.now().timestamp()
        # one continuous run spanning the rollup watermark, so the answer is
        # only right if the stored head and the live tail are merged, not
        # concatenated
        self.add_run(now - 3600, 340)

        roll_up_ranges(self.config, now=now)

        served = recording_ranges(["back"], now - 3700, now, "sub", 3.0)["back"]
        direct = live_ranges(["back"], now - 3700, now, "sub", 3.0)["back"]
        assert served == direct
        assert len(served) == 1, served

    def test_uncovered_window_falls_back_to_live(self):
        now = datetime.datetime.now().timestamp()
        old = now - 3 * DAY
        self.add_run(old, 30)

        roll_up_ranges(self.config, now=now)  # only reaches back an hour

        served = recording_ranges(["back"], old - 100, old + 400, "sub", 3.0)["back"]
        direct = live_ranges(["back"], old - 100, old + 400, "sub", 3.0)["back"]
        assert served == direct
        assert served

    def test_gap_below_materialized_gap_uses_live_path(self):
        now = datetime.datetime.now().timestamp()
        start = now - 7200
        # 0.4s of jitter: merged at MATERIALIZED_GAP these are one range, but a
        # caller asking for a finer gap must see them split
        self.add(start, start + 10)
        self.add(start + 10.4, start + 20.4)

        roll_up_ranges(self.config, now=now)

        coarse = recording_ranges(["back"], start - 10, start + 100, "sub", 3.0)["back"]
        fine = recording_ranges(["back"], start - 10, start + 100, "sub", 0.1)["back"]

        assert len(coarse) == 1
        assert len(fine) == 2

    def test_backfill_extends_coverage_backwards(self):
        now = datetime.datetime.now().timestamp()
        old = now - 2 * DAY
        self.add_run(old, 30)
        self.add_run(now - 7200, 30)

        roll_up_ranges(self.config, now=now)
        first = RecordingRangeCoverage.get(
            RecordingRangeCoverage.camera == "back",
            RecordingRangeCoverage.variant == "sub",
        ).covered_from

        for _ in range(4):
            backfill_ranges(self.config, now=now)

        later = RecordingRangeCoverage.get(
            RecordingRangeCoverage.camera == "back",
            RecordingRangeCoverage.variant == "sub",
        ).covered_from

        assert later < first
        served = recording_ranges(["back"], old - 100, old + 400, "sub", 3.0)["back"]
        assert served == live_ranges(["back"], old - 100, old + 400, "sub", 3.0)["back"]


class TestRetentionTrim(RangesTestBase):
    def test_trim_drops_expired_ranges_and_raises_covered_from(self):
        now = datetime.datetime.now().timestamp()
        # retention is 7 days; this range is 30 days old
        RecordingRanges.insert(
            camera="back",
            variant="sub",
            start_time=now - 30 * DAY,
            end_time=now - 30 * DAY + 600,
        ).execute()
        RecordingRangeCoverage.replace(
            camera="back", variant="sub", covered_from=now - 30 * DAY, covered_to=now
        ).execute()

        trim_ranges(self.config, now=now)

        assert RecordingRanges.select().count() == 0
        coverage = RecordingRangeCoverage.get(
            RecordingRangeCoverage.camera == "back",
            RecordingRangeCoverage.variant == "sub",
        )
        assert coverage.covered_from >= now - 7 * DAY - 1

    def test_trim_clips_range_straddling_the_cutoff(self):
        now = datetime.datetime.now().timestamp()
        cutoff = now - 7 * DAY
        RecordingRanges.insert(
            camera="back",
            variant="sub",
            start_time=cutoff - 3600,
            end_time=cutoff + 3600,
        ).execute()

        trim_ranges(self.config, now=now)

        row = RecordingRanges.get()
        assert row.start_time >= cutoff - 1, row.start_time
        assert row.end_time == cutoff + 3600
