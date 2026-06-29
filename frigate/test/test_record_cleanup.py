"""Tests for RecordingCleanup per-variant retention expiry.

These exercise the actual expire path (RecordingCleanup.expire_recordings), which
was previously untested. The regression guarded here: a `sub` variant configured
with a longer per-variant `retain_days` override must NOT be deleted at the base
`continuous.days` window.
"""

import datetime
import multiprocessing as mp

from frigate.config import FrigateConfig
from frigate.models import Previews, Recordings, ReviewSegment, UserReviewStatus
from frigate.record.cleanup import RecordingCleanup
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
