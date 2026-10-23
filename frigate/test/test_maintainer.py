import asyncio
import datetime
import os
import shutil
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# Mock complex imports before importing maintainer
sys.modules["frigate.comms.inter_process"] = MagicMock()
sys.modules["frigate.comms.detections_updater"] = MagicMock()
sys.modules["frigate.comms.recordings_updater"] = MagicMock()
sys.modules["frigate.config.camera.updater"] = MagicMock()

# Now import the class under test
from peewee import SqliteDatabase  # noqa: E402

from frigate.config import FrigateConfig  # noqa: E402
from frigate.const import CACHE_SEGMENT_FORMAT  # noqa: E402
from frigate.models import ReviewSegment  # noqa: E402
from frigate.record.maintainer import RecordingMaintainer  # noqa: E402


class TestMaintainer(unittest.IsolatedAsyncioTestCase):
    async def test_move_files_survives_bad_filename(self):
        config = MagicMock(spec=FrigateConfig)
        config.cameras = {}
        stop_event = MagicMock()

        maintainer = RecordingMaintainer(config, stop_event)

        # We need to mock end_time_cache to avoid key errors if logic proceeds
        maintainer.end_time_cache = {}

        # Mock filesystem
        # One bad file, one good file
        files = ["bad_filename.mp4", "camera@20210101000000+0000.mp4"]

        with patch("os.listdir", return_value=files):
            with patch("os.path.isfile", return_value=True):
                with patch(
                    "frigate.record.maintainer.psutil.process_iter", return_value=[]
                ):
                    with patch("frigate.record.maintainer.logger.warning") as warn:
                        # Mock validate_and_move_segment to avoid further logic
                        maintainer.validate_and_move_segment = MagicMock()

                        try:
                            await maintainer.move_files()
                        except ValueError as e:
                            if "not enough values to unpack" in str(e):
                                self.fail("move_files() crashed on bad filename!")
                            raise e
                        except Exception:
                            # Ignore other errors (like DB connection) as we only care about the unpack crash
                            pass

                        # The bad filename is encountered in multiple loops, but should only warn once.
                        matching = [
                            c
                            for c in warn.call_args_list
                            if c.args
                            and isinstance(c.args[0], str)
                            and "Skipping unexpected files in cache" in c.args[0]
                        ]
                        self.assertEqual(
                            1,
                            len(matching),
                            f"Expected a single warning for unexpected files, got {len(matching)}",
                        )


class CopyTracker:
    """Stands in for the ffmpeg copy; records how many run at once."""

    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.in_flight = 0
        self.max_in_flight = 0

    async def exec(self, *args, **kwargs):
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        src = args[args.index("-i") + 1]
        dst = args[-1]
        tracker = self

        class Process:
            returncode = 0
            stderr = None

            async def wait(self) -> int:
                await asyncio.sleep(tracker.delay)
                shutil.copyfile(src, dst)
                tracker.in_flight -= 1
                return 0

        return Process()


async def fake_probe(ffmpeg, path, get_duration=False):
    return {
        "has_valid_video": True,
        "duration": 10.0,
        "fourcc": "h264",
        "width": 640,
        "height": 360,
    }


class MoverTestBase(unittest.IsolatedAsyncioTestCase):
    CAMERA = "front"

    def setUp(self):
        self.cache_dir = tempfile.mkdtemp()
        self.record_dir = tempfile.mkdtemp()
        self.copies = CopyTracker()
        self.space = MagicMock(return_value=(10**12, 10**12))

        for target, value in (
            ("frigate.record.maintainer.CACHE_DIR", self.cache_dir),
            ("frigate.record.maintainer.RECORD_DIR", self.record_dir),
            (
                "frigate.record.maintainer.psutil.process_iter",
                MagicMock(return_value=[]),
            ),
            ("frigate.record.maintainer.get_video_properties", fake_probe),
            ("frigate.record.maintainer.cache_space", self.space),
            (
                "frigate.record.maintainer.asyncio.create_subprocess_exec",
                self.copies.exec,
            ),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        # the pass reads open reviews; none exist here
        self.db = SqliteDatabase(":memory:")
        self.db.bind([ReviewSegment])
        self.db.create_tables([ReviewSegment])

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.cache_dir, ignore_errors=True)
        shutil.rmtree(self.record_dir, ignore_errors=True)

    def make_maintainer(
        self, continuous_days: float = 7, motion_days: float = 0, **pipeline
    ) -> RecordingMaintainer:
        config = FrigateConfig(
            **{
                "mqtt": {"host": "mqtt"},
                "record": {
                    "enabled": True,
                    "continuous": {"days": continuous_days},
                    "motion": {"days": motion_days},
                    "pipeline": pipeline,
                },
                "cameras": {
                    self.CAMERA: {
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
        return RecordingMaintainer(config, MagicMock())

    def add_segment(
        self, age: float, variant: str = "main", size: int = 1000
    ) -> tuple[str, datetime.datetime]:
        """A closed cache segment that started ``age`` seconds ago."""
        start = datetime.datetime.fromtimestamp(
            int(time.time() - age), datetime.timezone.utc
        )
        name = f"{self.CAMERA}@{variant}@{start.strftime(CACHE_SEGMENT_FORMAT)}.mp4"
        path = os.path.join(self.cache_dir, name)

        with open(path, "wb") as f:
            f.write(b"\0" * size)

        return path, start

    def recording(self, path: str, start: datetime.datetime, variant="main"):
        return {"cache_path": path, "start_time": start, "variant": variant}

    def process_frames_until(self, maintainer, until: float, motion=1) -> None:
        """Pretend frame processing has reached ``until``, with some motion."""
        for frame_time in range(int(until) - 120, int(until) + 1):
            maintainer.object_recordings_info[self.CAMERA].append(
                (frame_time, [], [(0, 0, 10, 10)] * motion, [(0, 0, 10, 10)])
            )

    def recorded_files(self) -> list[str]:
        return [
            os.path.join(root, f)
            for root, _, files in os.walk(self.record_dir)
            for f in files
        ]


class TestProcessingWait(MoverTestBase):
    async def test_timed_out_continuous_segment_moves_with_unknown_stats(self):
        maintainer = self.make_maintainer()
        path, start = self.add_segment(age=60)

        with self.assertLogs("frigate.record.maintainer", "WARNING") as logs:
            row = await maintainer.validate_and_move_segment(
                self.CAMERA, [], self.recording(path, start)
            )

        assert row is not None
        for field in ("motion", "objects", "regions", "dBFS"):
            assert row[field] is None, field
        assert row["variant"] == "main"
        assert not os.path.exists(path)
        assert len(self.recorded_files()) == 1
        assert any("Frame processing for front" in line for line in logs.output)

    async def test_processed_continuous_segment_moves_with_stats(self):
        maintainer = self.make_maintainer()
        path, start = self.add_segment(age=60)
        self.process_frames_until(maintainer, time.time())

        row = await maintainer.validate_and_move_segment(
            self.CAMERA, [], self.recording(path, start)
        )

        assert row is not None
        assert row["motion"] > 0
        assert row["objects"] == 0
        assert row["regions"] > 0
        assert row["dBFS"] == 0

    async def test_recent_unprocessed_continuous_segment_waits(self):
        maintainer = self.make_maintainer()
        path, start = self.add_segment(age=15)

        row = await maintainer.validate_and_move_segment(
            self.CAMERA, [], self.recording(path, start)
        )

        assert row is None
        assert os.path.exists(path)

    async def test_timeout_zero_waits_like_upstream(self):
        maintainer = self.make_maintainer(processing_timeout=0)
        path, start = self.add_segment(age=600)

        row = await maintainer.validate_and_move_segment(
            self.CAMERA, [], self.recording(path, start)
        )

        assert row is None
        assert os.path.exists(path)

    async def test_motion_only_camera_waits(self):
        maintainer = self.make_maintainer(continuous_days=0, motion_days=7)
        path, start = self.add_segment(age=600)

        row = await maintainer.validate_and_move_segment(
            self.CAMERA, [], self.recording(path, start)
        )

        assert row is None
        assert os.path.exists(path)

    async def test_lag_warning_is_rate_limited(self):
        maintainer = self.make_maintainer()

        with self.assertLogs("frigate.record.maintainer", "WARNING") as logs:
            maintainer.warn_processing_lag(self.CAMERA)
            maintainer.warn_processing_lag(self.CAMERA)
            maintainer.processing_lag_logged[self.CAMERA] -= 301
            maintainer.warn_processing_lag(self.CAMERA)

        assert len(logs.output) == 2


class TestCompleteStatsDiscard(MoverTestBase):
    def in_progress_review(self):
        return [
            SimpleNamespace(
                start_time=time.time() - 300,
                end_time=None,
                severity="alert",
            )
        ]

    async def test_continuous_segment_in_review_is_not_discarded_unprocessed(self):
        # before the fix the review path moved it in "motion" mode with no
        # processed frames, and should_discard_segment unlinked it silently
        maintainer = self.make_maintainer()
        path, start = self.add_segment(age=15)

        row = await maintainer.validate_and_move_segment(
            self.CAMERA, self.in_progress_review(), self.recording(path, start)
        )

        assert row is None
        assert os.path.exists(path)

        # once processing has passed it, it moves with complete stats
        self.process_frames_until(maintainer, time.time())
        row = await maintainer.validate_and_move_segment(
            self.CAMERA, self.in_progress_review(), self.recording(path, start)
        )
        assert row is not None and row["motion"] > 0

    async def test_motion_only_segment_in_review_waits_for_processing(self):
        maintainer = self.make_maintainer(continuous_days=0, motion_days=7)
        path, start = self.add_segment(age=15)

        row = await maintainer.validate_and_move_segment(
            self.CAMERA, self.in_progress_review(), self.recording(path, start)
        )

        assert row is None
        assert os.path.exists(path)

    async def test_review_only_segment_decided_after_processing(self):
        maintainer = self.make_maintainer(continuous_days=0, motion_days=0)
        path, start = self.add_segment(age=15)
        review = self.in_progress_review()

        row = await maintainer.validate_and_move_segment(
            self.CAMERA, review, self.recording(path, start)
        )
        assert row is None
        assert os.path.exists(path)

        self.process_frames_until(maintainer, time.time())
        row = await maintainer.validate_and_move_segment(
            self.CAMERA, review, self.recording(path, start)
        )
        assert row is not None and row["motion"] > 0


class TestMovePass(MoverTestBase):
    async def test_copies_are_capped(self):
        maintainer = self.make_maintainer(move_concurrency=2)
        self.copies.delay = 0.05

        for i in range(6):
            self.add_segment(age=120 + i * 10, variant="main")

        await maintainer.move_files()

        assert self.copies.max_in_flight == 2
        assert len(self.recorded_files()) == 6
        assert os.listdir(self.cache_dir) == []

    async def test_zero_concurrency_is_unbounded(self):
        maintainer = self.make_maintainer(move_concurrency=0)
        self.copies.delay = 0.05

        for i in range(6):
            self.add_segment(age=120 + i * 10, variant="main")

        await maintainer.move_files()

        assert self.copies.max_in_flight == 6

    async def test_backlog_moved_in_the_same_pass_is_not_discarded(self):
        # about 3 minutes of stall per variant, well over max_cached_segments
        maintainer = self.make_maintainer()

        for variant in ("main", "sub"):
            for i in range(18):
                self.add_segment(age=60 + i * 10, variant=variant)

        with patch("frigate.record.maintainer.logger.warning") as warn:
            await maintainer.move_files()

        assert len(self.recorded_files()) == 36
        assert os.listdir(self.cache_dir) == []
        texts = " ".join(str(c.args[0]) for c in warn.call_args_list)
        assert "Unable to keep up" not in texts
        assert "Too many unprocessed" not in texts

    async def test_backlog_limit_is_per_variant(self):
        # motion-only: nothing moves while frame processing is stalled
        maintainer = self.make_maintainer(continuous_days=0, motion_days=7)
        main = [self.add_segment(200 - i * 10, "main")[0] for i in range(9)]
        sub = [self.add_segment(200 - i * 10, "sub")[0] for i in range(3)]

        with self.assertLogs("frigate.record.maintainer", "WARNING") as logs:
            await maintainer.move_files()

        # the 3 oldest main segments go; sub is under the limit
        assert [os.path.exists(p) for p in main] == [False] * 3 + [True] * 6
        assert all(os.path.exists(p) for p in sub)
        assert any("Too many unprocessed" in line for line in logs.output)

    async def test_free_space_reserve_discards_oldest(self):
        maintainer = self.make_maintainer(continuous_days=0, motion_days=7)
        oldest = [
            self.add_segment(200 - i * 10, "main", size=2000)[0] for i in range(2)
        ]
        newer = [self.add_segment(150 - i * 10, "sub", size=2000)[0] for i in range(3)]
        # 5% free on a 100 kB cache: 5 kB short of a 10% reserve, so the three
        # oldest 2 kB segments go; the end-of-pass check then finds room
        self.space.side_effect = [(5_000, 100_000)] + [(50_000, 100_000)] * 10

        with self.assertLogs("frigate.record.maintainer", "ERROR") as logs:
            await maintainer.move_files()

        assert not any(os.path.exists(p) for p in oldest)
        assert not os.path.exists(newer[0])
        assert all(os.path.exists(p) for p in newer[1:])
        assert any("below the 10% reserve" in line for line in logs.output)
        assert any("front: 3" in line for line in logs.output)

    async def test_free_space_reserve_off(self):
        maintainer = self.make_maintainer(
            continuous_days=0, motion_days=7, cache_free_reserve=0
        )
        path, _ = self.add_segment(age=100)
        self.space.return_value = (0, 100_000)

        await maintainer.move_files()

        assert os.path.exists(path)


if __name__ == "__main__":
    unittest.main()
