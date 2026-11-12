import asyncio
import datetime
import os
import shutil
import sys
import tempfile
import threading
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
from frigate.const import CACHE_SEGMENT_FORMAT, INSERT_MANY_RECORDINGS  # noqa: E402
from frigate.models import ReviewSegment  # noqa: E402
from frigate.record.cache_watch import filesystem_type  # noqa: E402
from frigate.record.maintainer import RecordingMaintainer  # noqa: E402
from frigate.record.writer import RecordingsWriter  # noqa: E402


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
        self.fs_type = MagicMock(return_value="tmpfs")

        for target, value in (
            ("frigate.record.maintainer.CACHE_DIR", self.cache_dir),
            ("frigate.record.maintainer.RECORD_DIR", self.record_dir),
            (
                "frigate.record.maintainer.psutil.process_iter",
                MagicMock(return_value=[]),
            ),
            ("frigate.record.maintainer.get_video_properties", fake_probe),
            ("frigate.record.maintainer.cache_space", self.space),
            ("frigate.record.maintainer.filesystem_type", self.fs_type),
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
        self,
        continuous_days: float = 7,
        motion_days: float = 0,
        alerts_mode: str = "motion",
        **pipeline,
    ) -> RecordingMaintainer:
        config = FrigateConfig(
            **{
                "mqtt": {"host": "mqtt"},
                "record": {
                    "enabled": True,
                    "continuous": {"days": continuous_days},
                    "motion": {"days": motion_days},
                    "alerts": {"retain": {"mode": alerts_mode}},
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
        # the media fields come from the shared row builder
        assert row["id"].startswith(f"{start.timestamp()}-")
        assert row["end_time"] == start.timestamp() + 10.0
        assert row["segment_size"] == round(1000 / 2**20, 2)
        assert row["bitrate"] == 800
        assert (row["codec_name"], row["width"], row["height"]) == ("h264", 640, 360)
        assert row["transcoded_from_main"] is False

    async def test_moved_segment_leaves_no_probe_cache_entry(self):
        # one entry per moved segment used to stay forever: ~300 MB a day on
        # 32 dual-stream cameras
        maintainer = self.make_maintainer()

        for i in range(3):
            self.add_segment(age=120 + i * 10, variant="main")
            self.add_segment(age=120 + i * 10, variant="sub")

        await maintainer.move_files()

        assert len(self.recorded_files()) == 6
        assert maintainer.end_time_cache == {}

    async def test_waiting_segment_keeps_its_probe_cache_entry(self):
        maintainer = self.make_maintainer()
        path, start = self.add_segment(age=15)

        await maintainer.validate_and_move_segment(
            self.CAMERA, [], self.recording(path, start)
        )

        assert path in maintainer.end_time_cache

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

    async def test_retain_all_review_segment_moves_on_the_timeout(self):
        # "all" never discards on activity, so a processing stall must not hold
        # it until the backlog limit deletes it
        maintainer = self.make_maintainer(
            continuous_days=0, motion_days=0, alerts_mode="all"
        )
        path, start = self.add_segment(age=60)

        with self.assertLogs("frigate.record.maintainer", "WARNING") as logs:
            row = await maintainer.validate_and_move_segment(
                self.CAMERA, self.in_progress_review(), self.recording(path, start)
            )

        assert row is not None
        for field in ("motion", "objects", "regions", "dBFS"):
            assert row[field] is None, field
        assert not os.path.exists(path)
        assert any("Frame processing for front" in line for line in logs.output)

    async def test_retain_all_review_segment_waits_within_the_timeout(self):
        maintainer = self.make_maintainer(
            continuous_days=0, motion_days=0, alerts_mode="all"
        )
        path, start = self.add_segment(age=15)

        row = await maintainer.validate_and_move_segment(
            self.CAMERA, self.in_progress_review(), self.recording(path, start)
        )

        assert row is None
        assert os.path.exists(path)

        # processed in time: moved with its real stats
        self.process_frames_until(maintainer, time.time())
        row = await maintainer.validate_and_move_segment(
            self.CAMERA, self.in_progress_review(), self.recording(path, start)
        )
        assert row is not None and row["motion"] > 0

    async def test_activity_mode_review_segment_ignores_the_timeout(self):
        maintainer = self.make_maintainer(continuous_days=0, motion_days=7)
        path, start = self.add_segment(age=60)

        row = await maintainer.validate_and_move_segment(
            self.CAMERA, self.in_progress_review(), self.recording(path, start)
        )

        assert row is None
        assert os.path.exists(path)

    async def test_retain_all_review_segment_waits_with_timeout_zero(self):
        maintainer = self.make_maintainer(
            continuous_days=0, motion_days=0, alerts_mode="all", processing_timeout=0
        )
        path, start = self.add_segment(age=600)

        row = await maintainer.validate_and_move_segment(
            self.CAMERA, self.in_progress_review(), self.recording(path, start)
        )

        assert row is None
        assert os.path.exists(path)


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
        # nothing moves (frame processing stalled): 5% free on a 100 kB cache is
        # above the half reserve the start of the pass keeps, then 5 kB short of
        # the full 10% after the moves, so the three oldest 2 kB segments go
        self.space.side_effect = [(5_000, 100_000)] * 2 + [(50_000, 100_000)] * 10

        with self.assertLogs("frigate.record.maintainer", "ERROR") as logs:
            await maintainer.move_files()

        assert not any(os.path.exists(p) for p in oldest)
        assert not os.path.exists(newer[0])
        assert all(os.path.exists(p) for p in newer[1:])
        assert any("below the 10% reserve" in line for line in logs.output)
        assert any("front: 3" in line for line in logs.output)

    async def test_start_of_pass_relieves_only_a_critically_full_cache(self):
        maintainer = self.make_maintainer(continuous_days=0, motion_days=7)
        oldest = [
            self.add_segment(200 - i * 10, "main", size=2000)[0] for i in range(2)
        ]
        newer = self.add_segment(150, "sub", size=2000)[0]
        # 2% free: 3 kB short of the 5% kept before the moves
        self.space.side_effect = [(2_000, 100_000)] + [(50_000, 100_000)] * 10

        with self.assertLogs("frigate.record.maintainer", "ERROR") as logs:
            await maintainer.move_files()

        assert not any(os.path.exists(p) for p in oldest)
        assert os.path.exists(newer)
        assert any("below the 5% reserve" in line for line in logs.output)

    async def test_start_of_pass_spares_segments_the_pass_moves(self):
        # 7% free is under the reserve, but moving the backlog frees that space
        # without losing it; the start of the pass used to discard it first
        maintainer = self.make_maintainer()
        for i in range(3):
            self.add_segment(age=120 + i * 10, size=2000)
        self.space.return_value = (7_000, 100_000)

        await maintainer.move_files()

        assert len(self.recorded_files()) == 3

    async def test_free_space_reserve_off(self):
        maintainer = self.make_maintainer(
            continuous_days=0, motion_days=7, cache_free_reserve=0
        )
        path, _ = self.add_segment(age=100)
        self.space.return_value = (0, 100_000)

        await maintainer.move_files()

        assert os.path.exists(path)

    async def test_free_space_reserve_skipped_off_tmpfs(self):
        # /tmp/cache on the recordings volume: its free space is the volume's,
        # and acting on it would discard every closed segment each pass
        maintainer = self.make_maintainer(continuous_days=0, motion_days=7)
        path, _ = self.add_segment(age=100)
        self.fs_type.return_value = "ext4"
        self.space.return_value = (1_000, 100_000)

        with self.assertLogs("frigate.record.maintainer", "WARNING") as logs:
            await maintainer.move_files()
            await maintainer.move_files()

        assert os.path.exists(path)
        self.space.assert_not_called()
        warnings = [line for line in logs.output if "is not applied" in line]
        assert len(warnings) == 1
        assert "ext4" in warnings[0]

    async def test_free_space_reserve_skipped_when_mounts_unreadable(self):
        maintainer = self.make_maintainer(continuous_days=0, motion_days=7)
        path, _ = self.add_segment(age=100)
        self.fs_type.return_value = None
        self.space.return_value = (1_000, 100_000)

        with self.assertLogs("frigate.record.maintainer", "WARNING"):
            await maintainer.move_files()

        assert os.path.exists(path)


class TestStop(MoverTestBase):
    def test_run_exits_promptly_on_stop(self):
        # the 5 s wait between passes used to ignore a stop, eating into the
        # time the writer has to drain within Docker's stop grace
        maintainer = self.make_maintainer()
        maintainer.stop_event = threading.Event()
        maintainer.detection_subscriber.check_for_update.return_value = (None, None)
        passes: list[float] = []

        async def one_pass():
            passes.append(time.monotonic())

        maintainer.move_files = one_pass
        maintainer.start()

        deadline = time.monotonic() + 5
        while not passes and time.monotonic() < deadline:
            time.sleep(0.01)
        assert passes

        stopped = time.monotonic()
        maintainer.stop_event.set()
        maintainer.join(2)

        assert not maintainer.is_alive()
        assert time.monotonic() - stopped < 1
        assert len(passes) == 1


class TestFilesystemType(unittest.TestCase):
    def setUp(self):
        fd, self.mounts = tempfile.mkstemp()
        os.close(fd)
        self.addCleanup(os.remove, self.mounts)

    def write_mounts(self, *lines: str) -> None:
        with open(self.mounts, "w") as f:
            f.write("\n".join(lines) + "\n")

    def test_deepest_mount_wins(self):
        # the compose file binds an SSD folder inside the tmpfs
        self.write_mounts(
            "overlay / overlay rw 0 0",
            "tmpfs /tmp/cache tmpfs rw,size=1757812k 0 0",
            "/dev/sda1 /tmp/cache/preview_frames ext4 rw 0 0",
            "/dev/md0 /media/frigate ext4 rw 0 0",
        )

        assert filesystem_type("/tmp/cache", self.mounts) == "tmpfs"
        assert filesystem_type("/tmp/cache/preview_frames", self.mounts) == "ext4"
        assert filesystem_type("/tmp/other", self.mounts) == "overlay"

    def test_prefix_is_not_a_parent(self):
        self.write_mounts(
            "overlay / overlay rw 0 0",
            "tmpfs /tmp/cach tmpfs rw 0 0",
        )

        assert filesystem_type("/tmp/cache", self.mounts) == "overlay"

    def test_later_mount_over_the_same_point_wins(self):
        self.write_mounts(
            "overlay / overlay rw 0 0",
            "/dev/md0 /tmp/cache ext4 rw 0 0",
            "tmpfs /tmp/cache tmpfs rw 0 0",
        )

        assert filesystem_type("/tmp/cache", self.mounts) == "tmpfs"

    def test_escaped_mount_point(self):
        self.write_mounts(
            "overlay / overlay rw 0 0",
            "tmpfs /tmp/my\\040cache tmpfs rw 0 0",
        )

        assert filesystem_type("/tmp/my cache", self.mounts) == "tmpfs"

    def test_unreadable_mount_table(self):
        assert filesystem_type("/tmp/cache", self.mounts + ".missing") is None


class TestWriterHandOff(MoverTestBase):
    async def test_passes_continue_while_the_database_blocks(self):
        maintainer = self.make_maintainer()
        maintainer.requestor = MagicMock()
        blocked = threading.Event()
        written: list[dict] = []

        class BlockedRequestor:
            def send_data(self, topic, rows):
                blocked.wait()
                written.extend(rows)
                return {"ok": True, "count": len(rows)}

            def stop(self):
                pass

        writer = RecordingsWriter(
            maintainer.config,
            requestor_factory=BlockedRequestor,
            publisher_factory=MagicMock,
        )
        writer.start()
        maintainer.writer = writer

        try:
            # two passes, each with segments to move, while no row can commit
            for first_age in (300, 120):
                for i in range(3):
                    self.add_segment(age=first_age + i * 10)
                await asyncio.wait_for(maintainer.move_files(), timeout=5)

            assert len(self.recorded_files()) == 6
            assert os.listdir(self.cache_dir) == []
            assert written == []
            maintainer.requestor.send_data.assert_not_called()
        finally:
            blocked.set()

        assert writer.stop(5)
        assert len(written) == 6

    async def test_full_row_queue_holds_segments_in_the_cache(self):
        maintainer = self.make_maintainer()
        # the mocked module hands every maintainer the same publisher
        maintainer.recordings_publisher = MagicMock()
        writer = MagicMock()
        writer.is_alive.return_value = True
        writer.has_room.return_value = False
        maintainer.writer = writer
        # frame info older than every cached segment: trimmed even while holding
        maintainer.object_recordings_info[self.CAMERA].append(
            (time.time() - 3600, [], [], [])
        )
        paths = [self.add_segment(age=200 - i * 10)[0] for i in range(8)]

        with patch("frigate.record.maintainer.logger.warning") as warn:
            await maintainer.move_files()

        # nothing moved or queued, and no "latest" heartbeat for the watchdog
        assert self.recorded_files() == []
        writer.submit.assert_not_called()
        maintainer.recordings_publisher.publish.assert_not_called()
        # the backlog limit still applies, quietly: has_room logged the reason
        assert [os.path.exists(p) for p in paths] == [False] * 2 + [True] * 6
        assert warn.call_count == 0
        assert maintainer.object_recordings_info[self.CAMERA] == []

        # room again: the held segments move
        writer.has_room.return_value = True
        await maintainer.move_files()

        assert len(self.recorded_files()) == 6
        rows, _saved = writer.submit.call_args.args
        assert len(rows) == 6

    async def test_stopped_writer_falls_back_to_the_blocking_request(self):
        maintainer = self.make_maintainer()
        maintainer.requestor = MagicMock()
        maintainer.requestor.send_data.return_value = {"ok": True}

        def gone():
            raise SystemExit

        writer = RecordingsWriter(
            maintainer.config, requestor_factory=gone, publisher_factory=MagicMock
        )
        writer.start()
        writer.join(5)
        assert not writer.is_alive()
        writer.rows.append({"path": "/stranded.mp4", "camera": self.CAMERA})
        maintainer.writer = writer

        for i in range(2):
            self.add_segment(age=120 + i * 10)

        with self.assertLogs("frigate.record.maintainer", "ERROR") as logs:
            await maintainer.move_files()
            await maintainer.move_files()

        assert len(self.recorded_files()) == 2
        sent = [
            r
            for call in maintainer.requestor.send_data.call_args_list
            for r in call.args[1]
        ]
        assert len(sent) == 3
        assert sent[0]["path"] == "/stranded.mp4"
        assert len([line for line in logs.output if "writer has stopped" in line]) == 1
        # the rows it held were handed over, so the stop counts as clean
        assert writer.stop(1)

    async def test_blocking_request_without_reply_is_logged(self):
        maintainer = self.make_maintainer()
        # the timed request got no reply from the main process
        maintainer.requestor = MagicMock()
        maintainer.requestor.send_data.return_value = ""

        def gone():
            raise SystemExit

        writer = RecordingsWriter(
            maintainer.config, requestor_factory=gone, publisher_factory=MagicMock
        )
        writer.start()
        writer.join(5)
        maintainer.writer = writer
        maintainer.writer_down_logged = True

        for i in range(2):
            self.add_segment(age=130 - i * 10)

        with self.assertLogs("frigate.record.maintainer", "ERROR") as logs:
            await maintainer.move_files()

        line = logs.output[-1]
        assert "Failed to write 2 recording rows" in line, logs.output
        assert "no reply" in line, line
        # the batch's first and last path: the files stay in recordings
        moved = self.recorded_files()
        assert len(moved) == 2
        assert all(path in line for path in moved), line

        # the next pass is not held up
        maintainer.requestor.send_data.return_value = {"ok": True}
        self.add_segment(age=100)
        await maintainer.move_files()
        assert len(self.recorded_files()) == 3

    async def test_blocking_request_is_timed(self):
        with patch("frigate.record.maintainer.InterProcessRequestor") as requestor:
            self.make_maintainer()

        # one enabled camera: the busy timeout of 60 s plus 30 s
        requestor.assert_called_once_with(timeout=90)

    async def test_blocking_mode_keeps_the_upstream_request(self):
        maintainer = self.make_maintainer(async_db_writes=False)
        maintainer.requestor = MagicMock()
        maintainer.requestor.send_data.return_value = {"ok": True, "count": 2}

        for i in range(2):
            self.add_segment(age=120 + i * 10)

        await maintainer.move_files()

        topic, rows = maintainer.requestor.send_data.call_args.args
        assert topic == INSERT_MANY_RECORDINGS
        assert len(rows) == 2


if __name__ == "__main__":
    unittest.main()
