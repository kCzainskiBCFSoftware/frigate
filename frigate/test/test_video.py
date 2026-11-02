import datetime
import logging
import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

import cv2
import numpy as np
from norfair.drawing.color import Palette
from norfair.drawing.drawer import Drawer

from frigate.config import FrigateConfig
from frigate.config.camera.ffmpeg import CameraRoleEnum
from frigate.const import CACHE_SEGMENT_FORMAT
from frigate.record.cache_watch import RecorderWatch
from frigate.util.image import intersection, transliterate_to_latin
from frigate.util.object import (
    get_cluster_boundary,
    get_cluster_candidates,
    get_cluster_region,
    get_region_from_grid,
    reduce_detections,
)


def draw_box(frame, box, color=(255, 0, 0), thickness=2):
    cv2.rectangle(
        frame,
        (box[0], box[1]),
        (box[2], box[3]),
        color,
        thickness,
    )


def save_clusters_image(name, boxes, candidates, regions=[]):
    canvas = np.zeros((1000, 2000, 3), np.uint8)
    for cluster in candidates:
        color = Palette.choose_color(np.random.rand())
        for b in cluster:
            box = boxes[b]
            draw_box(canvas, box, color, 2)
            # bottom right
            text_anchor = (
                box[2],
                box[3],
            )
            canvas = Drawer.text(
                canvas,
                str(b),
                position=text_anchor,
                size=None,
                color=(255, 255, 255),
                thickness=None,
            )
    for r in regions:
        draw_box(canvas, r, (0, 255, 0), 2)
    cv2.imwrite(
        f"debug/frames/{name}.jpg",
        canvas,
    )


def save_cluster_boundary_image(name, boxes, bounding_boxes):
    canvas = np.zeros((1000, 2000, 3), np.uint8)
    color = Palette.choose_color(np.random.rand())
    for box in boxes:
        draw_box(canvas, box, color, 2)
    for bound in bounding_boxes:
        draw_box(canvas, bound, (0, 255, 0), 2)
    cv2.imwrite(
        f"debug/frames/{name}.jpg",
        canvas,
    )


class TestRegion(unittest.TestCase):
    def setUp(self):
        self.frame_shape = (1000, 2000)
        self.min_region_size = 160

    def test_cluster_candidates(self):
        boxes = [(100, 100, 200, 200), (202, 150, 252, 200), (900, 900, 950, 950)]

        cluster_candidates = get_cluster_candidates(
            self.frame_shape, self.min_region_size, boxes
        )

        # save_clusters_image("cluster_candidates", boxes, cluster_candidates)

        assert len(cluster_candidates) == 2

    def test_transliterate_to_latin(self):
        self.assertEqual(transliterate_to_latin("frégate"), "fregate")
        self.assertEqual(transliterate_to_latin("utilité"), "utilite")
        self.assertEqual(transliterate_to_latin("imágé"), "image")

    def test_cluster_boundary(self):
        boxes = [(100, 100, 200, 200), (215, 215, 325, 325)]
        boundary_boxes = [
            get_cluster_boundary(box, self.min_region_size) for box in boxes
        ]

        # save_cluster_boundary_image("bound", boxes, boundary_boxes)
        assert len(boundary_boxes) == 2

    def test_cluster_regions(self):
        boxes = [(100, 100, 200, 200), (202, 150, 252, 200), (900, 900, 950, 950)]

        cluster_candidates = get_cluster_candidates(
            self.frame_shape, self.min_region_size, boxes
        )

        regions = [
            get_cluster_region(self.frame_shape, self.min_region_size, candidate, boxes)
            for candidate in cluster_candidates
        ]

        # save_clusters_image("regions", boxes, cluster_candidates, regions)
        assert len(regions) == 2

    def test_box_too_small_for_cluster(self):
        boxes = [(100, 100, 600, 600), (655, 100, 700, 145)]

        cluster_candidates = get_cluster_candidates(
            self.frame_shape, self.min_region_size, boxes
        )

        regions = [
            get_cluster_region(self.frame_shape, self.min_region_size, candidate, boxes)
            for candidate in cluster_candidates
        ]

        save_clusters_image("too_small", boxes, cluster_candidates, regions)

        assert len(cluster_candidates) == 2
        assert len(regions) == 2

    def test_redundant_clusters(self):
        boxes = [(100, 100, 200, 200), (305, 305, 415, 415)]

        cluster_candidates = get_cluster_candidates(
            self.frame_shape, self.min_region_size, boxes
        )

        regions = [
            get_cluster_region(self.frame_shape, self.min_region_size, candidate, boxes)
            for candidate in cluster_candidates
        ]

        # save_clusters_image("redundant", boxes, cluster_candidates, regions)

        assert len(cluster_candidates) == 2
        assert all([len(c) == 1 for c in cluster_candidates])
        assert len(regions) == 2

    def test_combine_boxes(self):
        boxes = [
            (480, 0, 540, 128),
            (536, 0, 558, 99),
        ]

        # boundary_boxes = [get_cluster_boundary(box) for box in boxes]
        # save_cluster_boundary_image("combine_bound", boxes, boundary_boxes)

        cluster_candidates = get_cluster_candidates(
            self.frame_shape, self.min_region_size, boxes
        )

        regions = [
            get_cluster_region(self.frame_shape, self.min_region_size, candidate, boxes)
            for candidate in cluster_candidates
        ]

        # save_clusters_image("combine", boxes, cluster_candidates, regions)
        assert len(regions) == 1

    def test_dont_combine_smaller_boxes(self):
        boxes = [
            (460, 0, 561, 144),
            (565, 0, 586, 71),
        ]

        # boundary_boxes = [get_cluster_boundary(box) for box in boxes]
        # save_cluster_boundary_image("combine_bound", boxes, boundary_boxes)

        cluster_candidates = get_cluster_candidates(
            self.frame_shape, self.min_region_size, boxes
        )

        regions = [
            get_cluster_region(self.frame_shape, self.min_region_size, candidate, boxes)
            for candidate in cluster_candidates
        ]

        # save_clusters_image("combine", boxes, cluster_candidates, regions)
        assert len(regions) == 2

    def test_dont_combine_boxes(self):
        boxes = [
            (460, 0, 532, 129),
            (586, 0, 606, 46),
        ]

        # boundary_boxes = [get_cluster_boundary(box) for box in boxes]
        # save_cluster_boundary_image("dont_combine_bound", boxes, boundary_boxes)

        cluster_candidates = get_cluster_candidates(
            self.frame_shape, self.min_region_size, boxes
        )

        regions = [
            get_cluster_region(self.frame_shape, self.min_region_size, candidate, boxes)
            for candidate in cluster_candidates
        ]

        # save_clusters_image("dont_combine", boxes, cluster_candidates, regions)
        assert len(regions) == 2


class TestObjectBoundingBoxes(unittest.TestCase):
    def setUp(self) -> None:
        pass

    def test_box_intersection(self):
        box_a = [2012, 191, 2031, 205]
        box_b = [887, 92, 985, 151]
        box_c = [899, 128, 1080, 175]

        assert intersection(box_a, box_b) == None
        assert intersection(box_b, box_c) == (899, 128, 985, 151)

    def test_overlapping_objects_reduced(self):
        """Test that object not on edge of region is used when a higher scoring object at the edge of region is provided."""
        detections = [
            (
                "car",
                0.81,
                (1209, 73, 1437, 163),
                20520,
                2.53333333,
                (1150, 0, 1500, 200),
            ),
            (
                "car",
                0.88,
                (1238, 73, 1401, 171),
                15974,
                1.663265306122449,
                (1242, 0, 1602, 360),
            ),
        ]
        frame_shape = (720, 2560)
        consolidated_detections = reduce_detections(frame_shape, detections)
        assert consolidated_detections == [
            (
                "car",
                0.81,
                (1209, 73, 1437, 163),
                20520,
                2.53333333,
                (1150, 0, 1500, 200),
            )
        ]

    def test_non_overlapping_objects_not_reduced(self):
        """Test that non overlapping objects are not reduced."""
        detections = [
            (
                "car",
                0.81,
                (1209, 73, 1437, 163),
                20520,
                2.53333333,
                (1150, 0, 1500, 200),
            ),
            (
                "car",
                0.83203125,
                (1121, 55, 1214, 100),
                4185,
                2.066666666666667,
                (922, 0, 1242, 320),
            ),
            (
                "car",
                0.85546875,
                (1414, 97, 1571, 186),
                13973,
                1.7640449438202248,
                (1248, 0, 1568, 320),
            ),
        ]
        frame_shape = (720, 2560)
        consolidated_detections = reduce_detections(frame_shape, detections)
        assert len(consolidated_detections) == len(detections)

    def test_overlapping_different_size_objects_not_reduced(self):
        """Test that overlapping objects that are significantly different in size are not reduced."""
        detections = [
            (
                "car",
                0.81,
                (164, 279, 816, 719),
                286880,
                1.48,
                (90, 0, 910, 820),
            ),
            (
                "car",
                0.83203125,
                (248, 340, 328, 385),
                3600,
                1.777,
                (0, 0, 460, 460),
            ),
        ]
        frame_shape = (720, 2560)
        consolidated_detections = reduce_detections(frame_shape, detections)
        assert len(consolidated_detections) == len(detections)

    def test_vert_stacked_cars_not_reduced(self):
        detections = [
            ("car", 0.8, (954, 312, 1247, 475), 498512, 1.48, (800, 200, 1400, 600)),
            ("car", 0.85, (970, 380, 1273, 610), 698752, 1.56, (800, 200, 1400, 700)),
        ]
        frame_shape = (720, 1280)
        consolidated_detections = reduce_detections(frame_shape, detections)
        assert len(consolidated_detections) == len(detections)


class TestRegionGrid(unittest.TestCase):
    def setUp(self) -> None:
        pass

    def test_region_in_range(self):
        """Test that region is kept at minimal size when within std dev."""
        frame_shape = (720, 1280)
        box = [450, 450, 550, 550]
        region_grid = [
            [],
            [],
            [],
            [{}, {}, {}, {}, {}, {"sizes": [0.25], "mean": 0.26, "std_dev": 0.01}],
        ]

        region = get_region_from_grid(frame_shape, box, 320, region_grid)
        assert region[2] - region[0] == 320

    def test_region_out_of_range(self):
        """Test that region is upsized when outside of std dev."""
        frame_shape = (720, 1280)
        box = [450, 450, 550, 550]
        region_grid = [
            [],
            [],
            [],
            [{}, {}, {}, {}, {}, {"sizes": [0.5], "mean": 0.5, "std_dev": 0.1}],
        ]

        region = get_region_from_grid(frame_shape, box, 320, region_grid)
        assert region[2] - region[0] > 320


TIMEOUT = 120


class RecorderWatchBase(unittest.TestCase):
    CAMERA = "front"

    def setUp(self):
        self.cache_dir = tempfile.mkdtemp()
        self.now = time.time()
        # watching (and recording) since well before the checks below
        self.enabled_at = self.now - 1000
        self.watch = RecorderWatch(self.CAMERA, self.enabled_at, self.cache_dir)

    def tearDown(self):
        shutil.rmtree(self.cache_dir, ignore_errors=True)

    def write(self, variant: str, age: float, camera: str = CAMERA) -> str:
        """A cache segment that started ``age`` seconds before now."""
        start = datetime.datetime.fromtimestamp(
            int(self.now - age), datetime.timezone.utc
        )
        path = os.path.join(
            self.cache_dir,
            f"{camera}@{variant}@{start.strftime(CACHE_SEGMENT_FORMAT)}.mp4",
        )
        open(path, "w").close()
        os.utime(path, (self.now - age, self.now - age))
        return path

    def healthy(self, variant: str, age: float = 5) -> str:
        """A fresh segment, plus the mover's valid verdict on the one before."""
        self.watch.on_valid(self.write(variant, age + 10), self.now - age - 10)
        return self.write(variant, age)

    def heartbeat(self, *ages: float) -> None:
        for age in sorted(ages, reverse=True):
            self.watch.on_latest(self.now - age, TIMEOUT)

    def stale(self, variant: str) -> str | None:
        return self.watch.stale_reason(variant, self.now, TIMEOUT, self.enabled_at)


class TestRecorderWatch(RecorderWatchBase):
    def test_paused_mover_with_fresh_files_restarts_nothing(self):
        # the mover last ran 3 minutes ago; both recorders keep writing
        self.heartbeat(*range(1000, 180, -5))
        self.write("main", 5)
        self.write("sub", 5)
        self.watch.scan()

        assert self.stale("main") is None
        assert self.stale("sub") is None
        assert self.watch.mover_stalled_for(self.now, TIMEOUT) >= 180

    def test_recorder_without_new_files_is_stale_after_the_timeout(self):
        self.heartbeat(*range(1000, -1, -5))
        self.write("main", 125)
        self.watch.scan()

        assert self.stale("main") == "No new recording segments were created"

    def test_freshness_is_remembered_after_the_mover_takes_the_file(self):
        self.heartbeat(*range(1000, -1, -5))
        path = self.healthy("main", 30)
        self.watch.scan()

        for name in os.listdir(self.cache_dir):
            os.remove(os.path.join(self.cache_dir, name))

        self.watch.scan()

        assert self.stale("main") is None

        # nothing new for longer than the timeout: now it is stale
        self.now += TIMEOUT
        self.heartbeat(0)
        assert self.stale("main") == "No new recording segments were created", path

    def test_growing_file_counts_by_its_mtime(self):
        self.heartbeat(*range(1000, -1, -5))
        self.watch.on_valid(None, self.now - 40)
        path = self.write("main", 30)
        # a recorder writing one long segment: old start time, fresh mtime
        os.utime(path, (self.now - 2, self.now - 2))
        os.rename(path, path.replace(path.rsplit("@", 1)[1], "20200101000000+0000.mp4"))
        self.watch.scan()

        assert self.stale("main") is None

    def test_hung_sub_with_healthy_main(self):
        self.heartbeat(*range(1000, -1, -5))
        self.healthy("main")
        self.write("sub", 300)
        self.watch.scan()

        assert self.stale("main") is None
        assert self.stale("sub") == "No new recording segments were created"

    def test_invalid_only_segments_stale_that_variant_while_mover_runs(self):
        self.heartbeat(*range(1000, -1, -5))
        self.write("main", 5)
        self.write("sub", 5)
        self.watch.scan()

        for age in range(300, 0, -10):
            self.watch.on_valid(self.write("main", age), self.now - age)
            self.watch.on_invalid(self.write("sub", age), self.now - age)

        assert self.stale("main") is None
        assert self.stale("sub") is not None

    def test_invalid_segments_are_not_judged_while_mover_is_stalled(self):
        self.heartbeat(*range(1000, 300, -5))
        self.write("sub", 5)
        self.watch.scan()
        self.watch.on_invalid(self.write("sub", 400), self.now - 400)

        assert self.stale("sub") is None

    def test_no_restart_while_the_backlog_clears_after_a_resume(self):
        # running, then stalled for 3 minutes; recorders kept writing
        self.heartbeat(*range(1000, 190, -5))

        for age in range(1000, 190, -10):
            self.watch.on_valid(self.write("main", age), self.now - age)

        self.write("main", 5)
        self.watch.scan()

        # the mover resumes; its first passes report the backlog's old segments
        self.heartbeat(0)
        for later in (0, 10, 20, 30):
            check = self.now + later
            self.watch.on_latest(check, TIMEOUT)
            self.watch.on_valid(None, self.now - 180 + later)
            assert (
                self.watch.stale_reason("main", check, TIMEOUT, self.enabled_at) is None
            )

    def test_startup_grace_is_the_smaller_of_90s_and_the_timeout(self):
        self.enabled_at = self.now - 50

        # 50s after start with no files: within the 90s grace at 120s timeout
        assert self.stale("main") is None
        # with a 40s timeout the grace is 40s, so it is already being judged
        assert (
            self.watch.stale_reason("main", self.now, 40, self.enabled_at)
            == "No new recording segments were created"
        )

    def test_restart_starts_a_new_grace_for_that_recorder_only(self):
        self.heartbeat(*range(1000, -1, -5))
        self.watch.on_restart("sub", self.now - 10)

        assert self.stale("sub") is None
        assert self.stale("main") is not None

    def test_other_cameras_files_are_ignored(self):
        self.heartbeat(*range(1000, -1, -5))
        self.write("main", 5, camera="front_yard")
        self.watch.scan()

        assert self.stale("main") is not None


class TestCameraWatchdogRecorders(RecorderWatchBase):
    def watchdog(self, mode: str = "cache", timeout: int = TIMEOUT):
        from frigate.video import CameraWatchdog

        config = FrigateConfig(
            **{
                "mqtt": {"host": "mqtt"},
                "record": {
                    "enabled": True,
                    "pipeline": {
                        "recorder_watchdog": mode,
                        "recorder_stale_timeout": timeout,
                    },
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

        with (
            patch("frigate.video.LogPipe"),
            patch("frigate.video.InterProcessRequestor"),
            patch("frigate.video.RecordingsDataSubscriber"),
            patch("frigate.video.CameraConfigUpdateSubscriber"),
        ):
            watchdog = CameraWatchdog(
                config.cameras[self.CAMERA], 2, None, None, None, None, MagicMock()
            )

        watchdog.requestor = MagicMock()
        watchdog.record_enable_time = datetime.datetime.fromtimestamp(
            self.enabled_at, datetime.timezone.utc
        )
        return watchdog

    def test_upstream_mode_has_no_recorder_watch(self):
        assert self.watchdog("mover").recorder_watch is None
        assert self.watchdog("cache").recorder_watch is not None

    def test_only_the_stale_recorder_is_restarted(self):
        watchdog = self.watchdog()
        watchdog.recorder_watch = self.watch
        self.heartbeat(*range(1000, -1, -5))
        self.healthy("main")
        self.write("sub", 300)
        self.watch.scan()

        processes = {
            variant: {
                "cmd": [variant],
                "roles": [CameraRoleEnum.record],
                "record_variant": variant,
                "logpipe": MagicMock(),
                "process": MagicMock(),
            }
            for variant in ("main", "sub")
        }

        with (
            patch("frigate.video.start_or_restart_ffmpeg") as restart,
            patch("frigate.video.time.time", return_value=self.now),
            self.assertLogs("watchdog.front", logging.ERROR) as logs,
        ):
            assert not watchdog.restart_if_stale(processes["main"], TIMEOUT)
            assert watchdog.restart_if_stale(processes["sub"], TIMEOUT)

        assert restart.call_count == 1
        assert restart.call_args.args[0] == ["sub"]
        assert "(sub) in the last 120s" in logs.output[0]

    def test_stalled_mover_is_reported_at_most_every_5_minutes(self):
        watchdog = self.watchdog()
        watchdog.recorder_watch = self.watch
        self.heartbeat(400)

        with self.assertLogs("watchdog.front", logging.WARNING) as logs:
            with patch("frigate.video.time.time", return_value=self.now):
                watchdog.warn_if_mover_stalled(TIMEOUT)
                watchdog.warn_if_mover_stalled(TIMEOUT)
            with patch("frigate.video.time.time", return_value=self.now + 301):
                watchdog.warn_if_mover_stalled(TIMEOUT)

        assert len(logs.output) == 2
        assert "recording mover" in logs.output[0]
