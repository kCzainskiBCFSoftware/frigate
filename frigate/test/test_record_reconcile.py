"""Tests for recovering segment rows after an unclean shutdown."""

import datetime
import multiprocessing as mp
import os
import shutil
import tempfile
from unittest.mock import patch

from frigate.config import FrigateConfig
from frigate.models import (
    Previews,
    RecordingRangeCoverage,
    RecordingRanges,
    Recordings,
    ReviewSegment,
    UserReviewStatus,
)
from frigate.record.cleanup import RecordingCleanup
from frigate.record.ranges import recording_ranges, reset_late_rows, roll_up_ranges
from frigate.record.reconcile import reconcile_recordings
from frigate.test.http_api.base_http_test import BaseTestHttp

UNPROBEABLE = "unprobeable"


async def fake_probe(ffmpeg, path, get_duration=False):
    with open(path) as f:
        if UNPROBEABLE in f.read():
            return {"has_valid_video": False, "duration": -1}

    return {
        "has_valid_video": True,
        "duration": 9.5,
        "fourcc": "h264",
        "width": 640,
        "height": 360,
    }


class TestReconcile(BaseTestHttp):
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
        reset_late_rows()
        self.record_dir = tempfile.mkdtemp()

        for target, value in (
            ("frigate.record.reconcile.RECORD_DIR", self.record_dir),
            ("frigate.record.reconcile.get_video_properties", fake_probe),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.config = FrigateConfig(
            **{
                "mqtt": {"host": "mqtt"},
                "record": {"enabled": True, "continuous": {"days": 7}},
                "cameras": {
                    "cam1": {
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
        self.now = datetime.datetime.now(datetime.timezone.utc).timestamp()

    def tearDown(self):
        super().tearDown()
        shutil.rmtree(self.record_dir, ignore_errors=True)

    def segment(self, age: float, variant: str, content: str = "video") -> str:
        """A segment file in the on-disk layout, started ``age`` seconds ago."""
        start = datetime.datetime.fromtimestamp(
            int(self.now - age), datetime.timezone.utc
        )
        path = os.path.join(
            self.record_dir,
            start.strftime("%Y-%m-%d/%H"),
            "cam1",
            variant,
            start.strftime("%M.%S.mp4"),
        )
        os.makedirs(os.path.dirname(path), exist_ok=True)

        with open(path, "w") as f:
            f.write(content)

        return path

    def with_row(self, path: str, age: float, variant: str) -> None:
        start = int(self.now - age)
        Recordings.insert(
            id=f"existing-{variant}-{age}",
            path=path,
            camera="cam1",
            start_time=start,
            end_time=start + 10,
            duration=10,
            motion=3,
            objects=0,
            dBFS=0,
            variant=variant,
        ).execute()

    def rows(self) -> dict[str, Recordings]:
        return {r.path: r for r in Recordings.select()}

    def test_orphans_in_the_window_get_rows(self):
        kept = self.segment(600, "main")
        self.with_row(kept, 600, "main")
        main_orphan = self.segment(300, "main")
        sub_orphan = self.segment(300, "sub")

        assert (
            reconcile_recordings(
                self.config, now=self.now, written_before=self.now + 60
            )
            == 2
        )

        rows = self.rows()
        assert rows[main_orphan].variant == "main"
        assert rows[sub_orphan].variant == "sub"

        for path in (main_orphan, sub_orphan):
            row = rows[path]
            assert row.duration == 9.5
            assert row.end_time == row.start_time + 9.5
            assert row.codec_name == "h264"
            assert row.motion is None and row.objects is None
            assert row.regions is None and row.dBFS is None

        # the existing row is untouched
        assert rows[kept].motion == 3

    def test_files_outside_the_window_are_ignored(self):
        old = self.segment(3 * 3600, "main")

        assert (
            reconcile_recordings(
                self.config, now=self.now, written_before=self.now + 60
            )
            == 0
        )
        assert old not in self.rows()

    def test_rerun_is_a_no_op(self):
        self.segment(300, "main")
        self.segment(300, "sub")

        assert (
            reconcile_recordings(
                self.config, now=self.now, written_before=self.now + 60
            )
            == 2
        )
        assert (
            reconcile_recordings(
                self.config, now=self.now, written_before=self.now + 60
            )
            == 0
        )
        assert Recordings.select().count() == 2

    def test_files_written_by_this_run_are_left_to_the_writer(self):
        fresh = self.segment(300, "main")

        assert (
            reconcile_recordings(
                self.config, now=self.now, written_before=self.now - 60
            )
            == 0
        )
        assert fresh not in self.rows()

    def test_recovered_rows_reach_the_precomputed_timeline(self):

        roll_up_ranges(self.config, now=self.now)
        orphan_age = 1800 + 600
        orphan = self.segment(orphan_age, "sub")

        reconcile_recordings(self.config, now=self.now, written_before=self.now + 60)
        roll_up_ranges(self.config, now=self.now + 60)

        start = int(self.now - orphan_age)
        served = recording_ranges(["cam1"], start - 60, start + 60, "sub", 3.0)
        assert served["cam1"] == [(start, start + 9.5)], (orphan, served)

    def test_unprobeable_file_is_logged_and_left_in_place(self):
        broken = self.segment(300, "main", content=UNPROBEABLE)

        with self.assertLogs("frigate.record.reconcile", "WARNING") as logs:
            assert (
                reconcile_recordings(
                    self.config, now=self.now, written_before=self.now + 60
                )
                == 0
            )

        assert os.path.exists(broken)
        assert broken not in self.rows()
        assert any(broken in line for line in logs.output)

    def test_cleanup_recovers_before_sync_only_after_unclean_shutdown(self):
        calls: list[str] = []
        self.config.record.sync_recordings = True

        with (
            patch(
                "frigate.record.cleanup.reconcile_recordings",
                lambda config: calls.append("reconcile"),
            ),
            patch(
                "frigate.record.cleanup.sync_recordings",
                lambda limited: calls.append("sync"),
            ),
        ):
            for unclean in (True, False):
                stop = mp.Event()
                stop.set()
                RecordingCleanup(self.config, stop, unclean).run()

        # unclean: recovery first, then sync; clean: no recovery scan
        assert calls == ["reconcile", "sync", "sync"]

    def test_recovery_can_be_turned_off(self):
        calls: list[str] = []
        self.config.record.pipeline.reconcile_on_unclean_shutdown = False

        with patch(
            "frigate.record.cleanup.reconcile_recordings",
            lambda config: calls.append("reconcile"),
        ):
            stop = mp.Event()
            stop.set()
            RecordingCleanup(self.config, stop, True).run()

        assert calls == []
