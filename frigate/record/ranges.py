"""Precomputed playback-timeline ranges.

The playback UI needs "which parts of this day have footage" per camera. That
answer lives in ``recordings``, but at ~8,640 segment rows per camera-day it is
expensive to derive: a device recording 32 cameras reads a quarter of a million
rows to produce a few hundred merged ranges, which is seconds of CPU on
NAS-class hardware and megabytes of JSON if the rows are shipped raw.

So the merge is precomputed. ``RecordingCleanup`` rolls settled windows up into
``recording_ranges`` on its existing 60s tick and walks history backwards an
hour at a time; ``recording_range_coverage`` records how much of the timeline
those rows actually speak for. Anything outside that coverage is merged live
with the same SQL the rollup uses, so the endpoints are correct from the first
request and only get faster as the backfill lands.
"""

import datetime
import logging
from typing import Iterable, Optional, Sequence

from frigate.config import FrigateConfig
from frigate.const import MAX_SEGMENT_DURATION
from frigate.models import RecordingRangeCoverage, RecordingRanges, Recordings
from frigate.record.variants import RECORDING_VARIANT_ALL

logger = logging.getLogger(__name__)

# Gap the stored ranges are merged at. Frigate's 10s segments are meant to abut
# exactly but the encoder leaves jitter between them, which is not a recording
# gap -- treating it as one would store ~8,640 ranges per camera-day instead of
# ~40 and defeat the whole point. It has to stay at or below the smallest gap a
# caller asks for, since anything below it falls through to the live path
# (see recording_ranges).
#
# Measured across a 24-camera dual-stream fleet: most streams never exceed 1.0s,
# but a camera writing short segments (8.1s median instead of 10s, with ~1.9s
# holes) produced ~8,200 rows/day on its own at a 1.0s threshold -- three
# quarters of the whole fleet's row count. 2.0 absorbs that while keeping a
# margin below the 3.0s default callers send.
MATERIALIZED_GAP = 2.0

# How far back the first rollup for a camera reaches. Kept small so a fresh
# install does not scan its entire retained history on the first tick; the
# hourly backfill walks the rest.
ROLLUP_INITIAL_WINDOW = 3600.0

# How much further back one backfill step reaches.
BACKFILL_STEP = 86400.0

# Minimum newly-settled time before a camera is rolled up again. Without it the
# 60s tick would rewrite every camera's trailing range every minute -- on a
# 32-camera dual-stream device that is ~128 small writes a minute queued behind
# the segment inserts on the same database, to save work the live-tail fallback
# does for free. Batching to ~5 minutes cuts that by 5x and costs nothing: an
# unmaterialized tail is merged live either way.
ROLLUP_MIN_ADVANCE = 300.0

Range = tuple[float, float]

# Gap-and-islands merge. The MAX() window over all preceding rows is what
# implements "merge on the running max end" -- segments do overlap in the field,
# and comparing against the previous row's end instead would let a long segment
# be followed by a shorter one and drag the range's end backwards, losing
# footage. The running SUM over the gap test numbers the islands.
#
# The three WHERE clauses are the seekable overlap form from
# recordings_overlap_clause(): the redundant-looking lower bound on start_time
# is what gives SQLite a two-sided range so it can seek the composite index
# instead of walking the camera's whole retained history.
_MERGE_SQL = """
WITH win AS (
  SELECT camera, start_time, end_time
    FROM recordings
   WHERE camera IN ({cameras}){variant}
     AND start_time <= ? AND end_time >= ? AND start_time >= ?
),
marked AS (
  SELECT camera, start_time, end_time,
         MAX(end_time) OVER (PARTITION BY camera ORDER BY start_time
                             ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prev_end
    FROM win
),
grouped AS (
  SELECT camera, start_time, end_time,
         SUM(CASE WHEN prev_end IS NULL OR start_time - prev_end > ? THEN 1 ELSE 0 END)
           OVER (PARTITION BY camera ORDER BY start_time ROWS UNBOUNDED PRECEDING) AS grp
    FROM marked
)
SELECT camera, MIN(start_time), MAX(end_time)
  FROM grouped GROUP BY camera, grp ORDER BY camera, 2
"""


def merge_ranges(ranges: Iterable[Range], gap: float) -> list[Range]:
    """Merge start-ordered ranges, joining any pair separated by <= gap.

    Applied to ranges already merged at a smaller threshold this is equivalent
    to merging the underlying segments at ``gap``: merging is the transitive
    closure of "hole <= threshold", a larger threshold only adds joins, and a
    stored range's endpoints are the true endpoints of its run.
    """
    merged: list[list[float]] = []

    for start, end in ranges:
        if merged and start - merged[-1][1] <= gap:
            # running max, not last end -- see _MERGE_SQL
            if end > merged[-1][1]:
                merged[-1][1] = end
        else:
            merged.append([start, end])

    return [(start, end) for start, end in merged]


def live_ranges(
    cameras: Sequence[str],
    after: float,
    before: float,
    variant: str,
    gap: float,
) -> dict[str, list[Range]]:
    """Merge ``recordings`` directly for the given window.

    Ranges straddling the window edge are returned whole rather than clipped --
    a timeline that drops a segment starting before ``after`` shows a hole at
    midnight that is not really there.
    """
    if not cameras:
        return {}

    placeholders = ",".join("?" for _ in cameras)
    variant_clause = "" if variant == RECORDING_VARIANT_ALL else " AND variant = ?"
    sql = _MERGE_SQL.format(cameras=placeholders, variant=variant_clause)

    params: list[object] = list(cameras)

    if variant != RECORDING_VARIANT_ALL:
        params.append(variant)

    params += [before, after, after - MAX_SEGMENT_DURATION, gap]

    results: dict[str, list[Range]] = {camera: [] for camera in cameras}

    for camera, start, end in Recordings._meta.database.execute_sql(sql, params):
        results[camera].append((start, end))

    return results


def _stored_ranges(
    cameras: Sequence[str], after: float, before: float, variant: str
) -> dict[str, list[Range]]:
    """Read precomputed ranges overlapping the window."""
    results: dict[str, list[Range]] = {camera: [] for camera in cameras}

    query = (
        RecordingRanges.select(
            RecordingRanges.camera,
            RecordingRanges.start_time,
            RecordingRanges.end_time,
        )
        .where(
            RecordingRanges.camera << list(cameras),
            RecordingRanges.start_time <= before,
            RecordingRanges.end_time >= after,
        )
        .order_by(RecordingRanges.camera, RecordingRanges.start_time)
    )

    if variant != RECORDING_VARIANT_ALL:
        query = query.where(RecordingRanges.variant == variant)

    for camera, start, end in query.tuples():
        results[camera].append((start, end))

    return results


def _coverage(cameras: Sequence[str], variant: str) -> dict[str, Range]:
    """Return {camera: (covered_from, covered_to)} for rolled-up cameras."""
    query = RecordingRangeCoverage.select(
        RecordingRangeCoverage.camera,
        RecordingRangeCoverage.covered_from,
        RecordingRangeCoverage.covered_to,
    ).where(RecordingRangeCoverage.camera << list(cameras))

    if variant != RECORDING_VARIANT_ALL:
        query = query.where(RecordingRangeCoverage.variant == variant)

    covered: dict[str, Range] = {}

    for camera, covered_from, covered_to in query.tuples():
        existing = covered.get(camera)

        if existing is None:
            covered[camera] = (covered_from, covered_to)
        else:
            # with variant="all" a camera has one row per variant; the usable
            # window is the part every variant has been rolled up for
            covered[camera] = (
                max(existing[0], covered_from),
                min(existing[1], covered_to),
            )

    return covered


def recording_ranges(
    cameras: Sequence[str],
    after: float,
    before: float,
    variant: str,
    gap: float,
) -> dict[str, list[Range]]:
    """Merged coverage ranges per camera for [after, before].

    Served from the precomputed table where the rollup has reached, merged live
    from ``recordings`` where it has not, and stitched when the window spans
    both. A camera with no footage gets an empty list, not a missing key.
    """
    if not cameras:
        return {}

    # a caller wanting finer resolution than the stored merge cannot be served
    # from the table -- the joins it wants kept apart have already been made
    if gap < MATERIALIZED_GAP:
        live = live_ranges(cameras, after, before, variant, gap)
        return {camera: merge_ranges(rows, gap) for camera, rows in live.items()}

    covered = _coverage(cameras, variant)
    stored = _stored_ranges(cameras, after, before, variant)

    results: dict[str, list[Range]] = {}

    for camera in cameras:
        window = covered.get(camera)
        rows = list(stored.get(camera, []))

        if window is None:
            uncovered = [(after, before)]
        else:
            covered_from, covered_to = window
            uncovered = []

            if after < covered_from:
                uncovered.append((after, min(before, covered_from)))

            if before > covered_to:
                uncovered.append((max(after, covered_to), before))

        for gap_start, gap_end in uncovered:
            fresh = live_ranges([camera], gap_start, gap_end, variant, gap)
            rows.extend(fresh.get(camera, []))

        results[camera] = merge_ranges(sorted(rows), gap)

    return results


def _replace_window(camera: str, variant: str, lo: float, hi: float) -> None:
    """Recompute stored ranges for [lo, hi], preserving coverage outside it.

    Deleting and recomputing (rather than appending) is what makes the rollup
    self-healing: a segment row that arrives late, or a range that was still
    growing when it was last written, is simply recomputed.

    The subtlety is that stored ranges are *unclipped*, so a run of continuous
    recording is one row that can extend far past this window on either side.
    Deleting everything that overlaps [lo, hi] therefore throws away coverage
    the recompute will not regenerate -- ``live_ranges`` only reports what its
    own ``end_time >= lo`` / ``start_time <= hi`` filters admit. That is what
    made the incremental rollup eat the table from the left, keeping only its
    own window while the coverage row still claimed the whole span, so the live
    fallback that would have hidden the damage never ran.

    So the parts of a straddling range that lie outside the window are carried
    across explicitly and merged back in, which also keeps a continuous run as a
    single row instead of gaining a seam at every window boundary.
    """
    overlapping = (
        RecordingRanges.select(
            RecordingRanges.start_time,
            RecordingRanges.end_time,
        )
        .where(
            RecordingRanges.camera == camera,
            RecordingRanges.variant == variant,
            RecordingRanges.end_time >= lo,
            RecordingRanges.start_time <= hi,
        )
        .tuples()
    )

    carried: list[Range] = []

    for row_start, row_end in overlapping:
        if row_start < lo:
            carried.append((row_start, lo))
        if row_end > hi:
            carried.append((hi, row_end))

    RecordingRanges.delete().where(
        RecordingRanges.camera == camera,
        RecordingRanges.variant == variant,
        RecordingRanges.end_time >= lo,
        RecordingRanges.start_time <= hi,
    ).execute()

    fresh = live_ranges([camera], lo, hi, variant, MATERIALIZED_GAP).get(camera, [])
    rows = merge_ranges(sorted(carried + fresh), MATERIALIZED_GAP)

    if rows:
        RecordingRanges.insert_many(
            [
                {
                    "camera": camera,
                    "variant": variant,
                    "start_time": row_start,
                    "end_time": row_end,
                }
                for row_start, row_end in rows
            ]
        ).execute()


def _camera_variants(config: FrigateConfig) -> list[tuple[str, str]]:
    return [
        (camera_name, variant)
        for camera_name, camera in config.cameras.items()
        if camera.enabled_in_config
        for variant in camera.get_record_variants()
    ]


def _retention_floor(
    config: FrigateConfig, camera: str, variant: str, now: float
) -> float:
    """Oldest timestamp that can still have footage for this camera/variant.

    Mirrors the outer deletion bound in RecordingCleanup.expire_recordings: the
    motion window is what actually bounds deletion (segments older than it go
    regardless of motion), and a per-variant retain_days override raises it.
    Deliberately the outer bound rather than the continuous one -- used to
    decide how far back to precompute and how far back to keep precomputed
    rows, both of which must not cut off footage that is still on disk.
    """
    camera_config = config.cameras[camera]
    base_motion_days = max(
        camera_config.record.motion.days, camera_config.record.continuous.days
    )
    override = camera_config.get_variant_retain_days(variant)
    days = max(base_motion_days, override) if override is not None else base_motion_days
    return now - (days * 86400)


def roll_up_ranges(config: FrigateConfig, now: Optional[float] = None) -> None:
    """Materialize newly settled coverage. Cheap enough for a 60s tick.

    Only windows that can no longer change are stored: a segment row can still
    be written up to MAX_SEGMENT_DURATION after its start, so the watermark
    trails real time by that much.
    """
    now = datetime.datetime.now().timestamp() if now is None else now
    hi = now - MAX_SEGMENT_DURATION

    for camera, variant in _camera_variants(config):
        coverage = RecordingRangeCoverage.get_or_none(
            RecordingRangeCoverage.camera == camera,
            RecordingRangeCoverage.variant == variant,
        )

        if coverage is None:
            lo = hi - ROLLUP_INITIAL_WINDOW
            covered_from = lo
        else:
            if hi - coverage.covered_to < ROLLUP_MIN_ADVANCE:
                continue

            # redo the trailing overlap so a range that was still growing last
            # tick is recomputed rather than left truncated
            lo = coverage.covered_to - MAX_SEGMENT_DURATION
            covered_from = coverage.covered_from

        try:
            _replace_window(camera, variant, lo, hi)
        except Exception:
            logger.exception(f"Failed to roll up ranges for {camera}/{variant}")
            continue

        RecordingRangeCoverage.replace(
            camera=camera,
            variant=variant,
            covered_from=covered_from,
            covered_to=hi,
        ).execute()


def backfill_ranges(config: FrigateConfig, now: Optional[float] = None) -> None:
    """Extend coverage one step further into history. Runs hourly.

    Correctness never waits on this -- an uncovered window is merged live -- so
    it deliberately does one step per call rather than blocking startup with a
    full-history scan.
    """
    now = datetime.datetime.now().timestamp() if now is None else now

    for camera, variant in _camera_variants(config):
        coverage = RecordingRangeCoverage.get_or_none(
            RecordingRangeCoverage.camera == camera,
            RecordingRangeCoverage.variant == variant,
        )

        if coverage is None:
            continue

        floor = _retention_floor(config, camera, variant, now)

        if coverage.covered_from <= floor:
            continue

        target = max(floor, coverage.covered_from - BACKFILL_STEP)

        try:
            # overlap the existing edge so a range spanning the seam is
            # recomputed whole instead of being split in two
            _replace_window(
                camera, variant, target, coverage.covered_from + MAX_SEGMENT_DURATION
            )
        except Exception:
            logger.exception(f"Failed to backfill ranges for {camera}/{variant}")
            continue

        RecordingRangeCoverage.replace(
            camera=camera,
            variant=variant,
            covered_from=target,
            covered_to=coverage.covered_to,
        ).execute()


def trim_ranges(config: FrigateConfig, now: Optional[float] = None) -> None:
    """Drop precomputed ranges that retention has already deleted.

    Must run after expire_recordings so the ranges table follows the recordings
    table rather than advertising footage that is gone.
    """
    now = datetime.datetime.now().timestamp() if now is None else now

    for camera, variant in _camera_variants(config):
        floor = _retention_floor(config, camera, variant, now)

        RecordingRanges.delete().where(
            RecordingRanges.camera == camera,
            RecordingRanges.variant == variant,
            RecordingRanges.end_time < floor,
        ).execute()

        # a range straddling the cutoff keeps only the part still on disk
        RecordingRanges.update(start_time=floor).where(
            RecordingRanges.camera == camera,
            RecordingRanges.variant == variant,
            RecordingRanges.start_time < floor,
            RecordingRanges.end_time >= floor,
        ).execute()

        RecordingRangeCoverage.update(covered_from=floor).where(
            RecordingRangeCoverage.camera == camera,
            RecordingRangeCoverage.variant == variant,
            RecordingRangeCoverage.covered_from < floor,
        ).execute()
