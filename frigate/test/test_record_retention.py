import datetime
import multiprocessing as mp
import unittest

from frigate.config import FrigateConfig, RetainModeEnum
from frigate.models import Previews, Recordings, ReviewSegment, UserReviewStatus
from frigate.record.cleanup import RecordingCleanup
from frigate.record.maintainer import SegmentInfo
from frigate.test.http_api.base_http_test import BaseTestHttp

DAY = 24 * 60 * 60


class TestRecordRetention(unittest.TestCase):
    def test_motion_should_keep_motion_not_object(self):
        segment_info = SegmentInfo(
            motion_count=1, active_object_count=0, region_count=0, average_dBFS=0
        )
        assert not segment_info.should_discard_segment(RetainModeEnum.motion)
        assert segment_info.should_discard_segment(RetainModeEnum.active_objects)

    def test_object_should_keep_object_when_motion(self):
        segment_info = SegmentInfo(
            motion_count=0, active_object_count=1, region_count=0, average_dBFS=0
        )
        assert not segment_info.should_discard_segment(RetainModeEnum.motion)
        assert not segment_info.should_discard_segment(RetainModeEnum.active_objects)

    def test_all_should_keep_all(self):
        segment_info = SegmentInfo(
            motion_count=0, active_object_count=0, region_count=0, average_dBFS=0
        )
        assert not segment_info.should_discard_segment(RetainModeEnum.all)

    def test_should_keep_audio_in_motion_mode(self):
        segment_info = SegmentInfo(
            motion_count=0, active_object_count=0, region_count=0, average_dBFS=1
        )
        assert not segment_info.should_discard_segment(RetainModeEnum.motion)
        assert segment_info.should_discard_segment(RetainModeEnum.active_objects)


class TestUnknownStatsReviewRetention(BaseTestHttp):
    """Review retain modes keep a segment whose activity stats are unknown."""

    def setUp(self):
        super().setUp([Recordings, ReviewSegment, UserReviewStatus, Previews])
        self.config = FrigateConfig(
            **{
                "mqtt": {"host": "mqtt"},
                "record": {
                    "enabled": True,
                    "continuous": {"days": 1},
                    "motion": {"days": 3},
                    "alerts": {"retain": {"days": 30, "mode": "motion"}},
                    "detections": {"retain": {"days": 30, "mode": "active_objects"}},
                },
                "cameras": {
                    "back": {
                        "ffmpeg": {
                            "inputs": [
                                {
                                    "path": "rtsp://10.0.0.1:554/video",
                                    "roles": ["detect", "record"],
                                }
                            ]
                        },
                        "detect": {"height": 1080, "width": 1920, "fps": 5},
                    }
                },
            }
        )

    def _recording(self, id: str, start: float, stats) -> None:
        motion, objects, dbfs = stats
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
            variant="main",
        ).execute()

    def _review(self, id: str, start: float, severity: str) -> None:
        ReviewSegment.insert(
            id=id,
            camera="back",
            start_time=start - 5,
            end_time=start + 20,
            severity=severity,
            thumb_path=f"/thumbs/{id}.webp",
            data={},
        ).execute()

    def test_unknown_stats_survive_motion_and_active_objects_modes(self):
        now = datetime.datetime.now().timestamp()
        alert_at = now - 10 * DAY
        detection_at = now - 12 * DAY
        self._review("alert", alert_at, "alert")
        self._review("detection", detection_at, "detection")

        for name, start in (("alert", alert_at), ("detection", detection_at)):
            self._recording(f"{name}-unknown", start, (None, None, None))
            self._recording(f"{name}-still", start + 10, (0, 0, 0))

        RecordingCleanup(self.config, mp.Event()).expire_recordings()
        remaining = {r.id for r in Recordings.select(Recordings.id)}

        # known-still segments go under both modes; unknown ones are kept
        assert remaining == {"alert-unknown", "detection-unknown"}
