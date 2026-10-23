"""Tests for RecordingCleanup per-variant retention expiry.

These exercise the actual expire path (RecordingCleanup.expire_recordings), which
was previously untested. The regression guarded here: a `sub` variant configured
with a longer per-variant `retain_days` override must NOT be deleted at the base
`continuous.days` window.
"""

import datetime
import multiprocessing as mp
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from frigate.config import FrigateConfig
from frigate.models import Previews, Recordings, ReviewSegment, UserReviewStatus
from frigate.record.cleanup import RecordingCleanup
from frigate.record.util import remove_empty_parent_directories
from frigate.test.http_api.base_http_test import BaseTestHttp

DAY = 24 * 60 * 60


class TestRecordingCleanup(BaseTestHttp):
    def setUp(self):
        super().setUp([Recordings, ReviewSegment, UserReviewStatus, Previews])

        # base continuous retention of 7 days, sub variant overridden to 30 days
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
                                    "retain_days": 30,
                                },
                            ]
                        },
                        "detect": {"height": 1080, "width": 1920, "fps": 5},
                    }
                },
            }
        )

    def _insert(self, id: str, variant: str, age_days: float) -> None:
        """Insert a motionless recording `age_days` old (no motion/audio/objects,
        so retention is driven purely by the time windows)."""
        now = datetime.datetime.now().timestamp()
        start = now - age_days * DAY
        Recordings.insert(
            id=id,
            path=id,
            camera="back",
            start_time=start,
            end_time=start + 20,
            duration=20,
            motion=0,
            objects=0,
            dBFS=0,
            variant=variant,
        ).execute()

    def _ids(self) -> set[str]:
        return {r.id for r in Recordings.select(Recordings.id)}

    def test_sub_variant_retained_to_override_window(self):
        # main (no override -> 7 day window)
        self._insert("main-3d", "main", 3)
        self._insert("main-8d", "main", 8)
        # sub (override -> 30 day window)
        self._insert("sub-3d", "sub", 3)
        self._insert("sub-8d", "sub", 8)
        self._insert("sub-20d", "sub", 20)
        self._insert("sub-35d", "sub", 35)

        RecordingCleanup(self.config, mp.Event()).expire_recordings()

        remaining = self._ids()

        # main respects the base 7-day window
        self.assertIn("main-3d", remaining)
        self.assertNotIn("main-8d", remaining)

        # sub respects its 30-day override (this is the regression: 8d/20d would
        # be wrongly deleted with the base 7-day window)
        self.assertIn("sub-3d", remaining)
        self.assertIn("sub-8d", remaining)
        self.assertIn("sub-20d", remaining)
        # beyond the override the hard cap deletes
        self.assertNotIn("sub-35d", remaining)


class TestUnknownStatsRetention(BaseTestHttp):
    """Rows moved on the processing timeout have NULL stats; keep, don't expire."""

    def setUp(self):
        super().setUp([Recordings, ReviewSegment, UserReviewStatus, Previews])

    def _config(self, **record) -> FrigateConfig:
        return FrigateConfig(
            **{
                "mqtt": {"host": "mqtt"},
                "record": {"enabled": True, **record},
                "cameras": {"back": _dual_stream_camera("10.0.0.1", 30)},
            }
        )

    def _insert(self, id: str, variant: str, age_days: float, stats) -> None:
        motion, objects, dbfs = stats
        start = datetime.datetime.now().timestamp() - age_days * DAY
        Recordings.insert(
            id=id,
            path=id,
            camera="back",
            start_time=start,
            end_time=start + 10,
            duration=10,
            motion=motion,
            objects=objects,
            dBFS=dbfs,
            regions=None if motion is None else 0,
            variant=variant,
        ).execute()

    def _expire(self, config: FrigateConfig) -> set[str]:
        RecordingCleanup(config, mp.Event()).expire_recordings()
        return {r.id for r in Recordings.select(Recordings.id)}

    def test_kept_until_the_outer_window(self):
        config = self._config(continuous={"days": 1}, motion={"days": 7})
        unknown = (None, None, None)
        self._insert("unknown-3d", "main", 3, unknown)
        self._insert("still-3d", "main", 3, (0, 0, 0))
        self._insert("unknown-8d", "main", 8, unknown)

        remaining = self._expire(config)

        # the continuous-only "no motion" rule expires known-still footage only
        assert remaining == {"unknown-3d"}

    def test_hard_cap_still_applies(self):
        config = self._config(continuous={"days": 7}, motion={"days": 7})
        unknown = (None, None, None)
        self._insert("sub-unknown-29d", "sub", 29, unknown)
        self._insert("sub-unknown-31d", "sub", 31, unknown)

        remaining = self._expire(config)

        assert remaining == {"sub-unknown-29d"}

    def test_hard_cap_applies_inside_a_longer_motion_window(self):
        config = self._config(continuous={"days": 7}, motion={"days": 60})
        self._insert("sub-unknown-31d", "sub", 31, (None, None, None))

        assert self._expire(config) == set()


class TestScopedDirectoryRemoval(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _mkdir(self, *parts: str) -> str:
        path = os.path.join(self.root, *parts)
        os.makedirs(path, exist_ok=True)
        return path

    def _touch(self, *parts: str) -> str:
        path = os.path.join(self.root, *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w").close()
        return path

    def test_removes_emptied_branch_up_to_root(self):
        emptied = self._mkdir("2026-09-23", "04", "cam1", "main")
        self._touch("2026-09-23", "05", "cam1", "main", "00.00.mp4")

        remove_empty_parent_directories([emptied], self.root)

        assert not os.path.exists(os.path.join(self.root, "2026-09-23", "04"))
        assert os.path.isdir(os.path.join(self.root, "2026-09-23", "05", "cam1"))
        assert os.path.isdir(self.root)

    def test_stops_at_first_non_empty_parent(self):
        emptied = self._mkdir("2026-09-23", "04", "cam1", "main")
        self._touch("2026-09-23", "04", "cam1", "sub", "00.00.mp4")

        remove_empty_parent_directories([emptied], self.root)

        assert not os.path.exists(emptied)
        assert os.path.isdir(os.path.join(self.root, "2026-09-23", "04", "cam1"))

    def test_sibling_branches_share_a_parent(self):
        main = self._mkdir("2026-09-23", "04", "cam1", "main")
        sub = self._mkdir("2026-09-23", "04", "cam1", "sub")

        remove_empty_parent_directories([main, sub], self.root)

        assert os.listdir(self.root) == []

    def test_leaves_directories_it_was_not_given(self):
        emptied_elsewhere = self._mkdir("2026-09-22", "10", "cam2", "main")
        emptied = self._mkdir("2026-09-23", "04", "cam1", "main")

        remove_empty_parent_directories([emptied], self.root)

        assert os.path.isdir(emptied_elsewhere)

    def test_never_removes_root_or_outside_it(self):
        outside = tempfile.mkdtemp()

        try:
            remove_empty_parent_directories([self.root, outside], self.root)
            assert os.path.isdir(self.root)
            assert os.path.isdir(outside)
        finally:
            shutil.rmtree(outside, ignore_errors=True)

    def test_cleanup_full_mode_walks_the_whole_tree(self):
        emptied_elsewhere = self._mkdir("2026-09-22", "10", "cam2", "main")
        config = FrigateConfig(
            **{
                "mqtt": {"host": "mqtt"},
                "record": {"pipeline": {"empty_directory_cleanup": "full"}},
                "cameras": {
                    "back": {
                        "ffmpeg": {
                            "inputs": [
                                {
                                    "path": "rtsp://10.0.0.1:554/video",
                                    "roles": ["detect"],
                                }
                            ]
                        },
                        "detect": {"height": 1080, "width": 1920, "fps": 5},
                    }
                },
            }
        )
        cleanup = RecordingCleanup(config, mp.Event())

        with patch("frigate.record.cleanup.RECORD_DIR", self.root):
            cleanup.remove_expired_directories(set())

        assert not os.path.exists(emptied_elsewhere)
        assert os.path.isdir(self.root)


def _dual_stream_camera(host: str, sub_retain_days=None) -> dict:
    inputs = [
        {
            "path": f"rtsp://{host}:554/main",
            "roles": ["detect", "record"],
            "record_variant": "main",
        },
    ]

    if sub_retain_days is not None:
        inputs.append(
            {
                "path": f"rtsp://{host}:554/sub",
                "roles": ["record"],
                "record_variant": "sub",
                "retain_days": sub_retain_days,
            }
        )

    return {
        "ffmpeg": {"inputs": inputs},
        "detect": {"height": 1080, "width": 1920, "fps": 5},
    }


class TestBoundedExpiryQueries(BaseTestHttp):
    """The start_time bounds change which index is read, never which rows."""

    def setUp(self):
        super().setUp([Recordings, ReviewSegment, UserReviewStatus, Previews])
        self.files = tempfile.mkdtemp()
        self.config = FrigateConfig(
            **{
                "mqtt": {"host": "mqtt"},
                "record": {
                    "enabled": True,
                    "continuous": {"days": 7},
                    "motion": {"days": 14},
                    "alerts": {"retain": {"days": 30, "mode": "motion"}},
                    "detections": {"retain": {"days": 30, "mode": "active_objects"}},
                },
                "cameras": {
                    "back": _dual_stream_camera("10.0.0.1", sub_retain_days=30),
                    # records main only, so its sub rows are orphans
                    "front": _dual_stream_camera("10.0.0.2"),
                },
            }
        )

    def tearDown(self):
        super().tearDown()
        shutil.rmtree(self.files, ignore_errors=True)

    def _fixture(self) -> None:
        now = datetime.datetime.now().timestamp()
        stats = {
            "still": (0, 0, 0),
            "motion": (5, 0, 0),
            "objects": (5, 2, 0),
            "audio": (0, 0, -40),
            "unknown": (None, None, None),
        }
        rows = []

        for camera, variants in (
            ("back", ("main", "sub")),
            ("front", ("main", "sub")),
            # no longer configured
            ("gone", ("main",)),
        ):
            for variant in variants:
                for age in (3, 8, 10, 15, 20, 31, 40):
                    for kind, (motion, objects, dbfs) in stats.items():
                        start = now - age * DAY
                        path = os.path.join(
                            self.files, camera, variant, f"{age}-{kind}.mp4"
                        )
                        os.makedirs(os.path.dirname(path), exist_ok=True)
                        open(path, "w").close()
                        rows.append(
                            {
                                "id": f"{camera}-{variant}-{age}-{kind}",
                                "path": path,
                                "camera": camera,
                                "variant": variant,
                                "start_time": start,
                                "end_time": start + 10,
                                "duration": 10,
                                "motion": motion,
                                "objects": objects,
                                "dBFS": dbfs,
                                "regions": None if motion is None else 0,
                            }
                        )

        Recordings.insert_many(rows).execute()

        # reviews that keep some windows past continuous retention
        for review_id, camera, age, severity in (
            ("alert-10", "back", 10, "alert"),
            ("detection-20", "back", 20, "detection"),
            ("alert-front-10", "front", 10, "alert"),
        ):
            start = now - age * DAY - 30
            ReviewSegment.insert(
                id=review_id,
                camera=camera,
                start_time=start,
                end_time=start + 60,
                severity=severity,
                thumb_path=os.path.join(self.files, f"{review_id}.webp"),
                data={},
            ).execute()

    def _expire(self) -> tuple[set[str], set[str]]:
        RecordingCleanup(self.config, mp.Event()).expire_recordings()
        rows = {r.id for r in Recordings.select(Recordings.id)}
        files = {
            os.path.join(root, f)
            for root, _, names in os.walk(self.files)
            for f in names
        }
        return rows, files

    def _reset(self) -> None:
        Recordings.delete().execute()
        ReviewSegment.delete().execute()
        shutil.rmtree(self.files, ignore_errors=True)
        os.makedirs(self.files)

    def test_bounded_queries_expire_the_same_rows_and_files(self):
        self._fixture()
        total = Recordings.select().count()

        with patch(
            "frigate.record.cleanup.expiry_seek_bound",
            lambda cutoff: Recordings.start_time.is_null(False),
        ):
            unbounded = self._expire()

        self._reset()
        self._fixture()
        bounded = self._expire()

        assert bounded == unbounded
        # the fixture exercises both outcomes
        assert 0 < len(bounded[0]) < total

    def _plan(self, query) -> str:
        sql, params = query.sql()
        return " ".join(
            str(row)
            for row in self.db.execute_sql(
                "EXPLAIN QUERY PLAN " + sql, params
            ).fetchall()
        )

    def _assert_seeks_start_time(self, query) -> None:
        self._fixture()
        self.db.execute_sql("ANALYZE")
        plan = self._plan(query)
        assert "start_time<" in plan, plan
        assert "SCAN recordings" not in plan, plan

    def test_removed_camera_query_seeks_start_time(self):
        cleanup = RecordingCleanup(self.config, mp.Event())
        cutoff = datetime.datetime.now().timestamp() - 14 * DAY
        self._assert_seeks_start_time(cleanup.removed_camera_query(cutoff))

    def test_variant_query_seeks_start_time(self):
        cleanup = RecordingCleanup(self.config, mp.Event())
        now = datetime.datetime.now().timestamp()
        query = cleanup.variant_expiry_query(
            "back", now - 30 * DAY, now - 30 * DAY, "sub", now - 30 * DAY
        )
        self._assert_seeks_start_time(query)

    def test_orphan_variant_query_seeks_start_time(self):
        cleanup = RecordingCleanup(self.config, mp.Event())
        cutoff = datetime.datetime.now().timestamp() - 14 * DAY
        self._assert_seeks_start_time(
            cleanup.orphan_variant_query("front", ["main"], cutoff)
        )


class TestVariantHardCap(BaseTestHttp):
    def setUp(self):
        super().setUp([Recordings, ReviewSegment, UserReviewStatus, Previews])

    def test_hard_cap_deletes_motion_inside_a_longer_motion_window(self):
        # variant-retention spec: motion kept 60 days, sub retain_days 30 ->
        # sub segments older than 30 days go, whatever motion they contain
        config = FrigateConfig(
            **{
                "mqtt": {"host": "mqtt"},
                "record": {
                    "enabled": True,
                    "continuous": {"days": 7},
                    "motion": {"days": 60},
                },
                "cameras": {"back": _dual_stream_camera("10.0.0.1", 30)},
            }
        )
        now = datetime.datetime.now().timestamp()

        for id, variant, age in (
            ("sub-29d", "sub", 29),
            ("sub-31d", "sub", 31),
            ("main-31d", "main", 31),
        ):
            start = now - age * DAY
            Recordings.insert(
                id=id,
                path=id,
                camera="back",
                start_time=start,
                end_time=start + 10,
                duration=10,
                motion=12,
                objects=0,
                dBFS=0,
                variant=variant,
            ).execute()

        RecordingCleanup(config, mp.Event()).expire_recordings()
        remaining = {r.id for r in Recordings.select(Recordings.id)}

        assert remaining == {"sub-29d", "main-31d"}
