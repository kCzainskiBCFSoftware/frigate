"""Unit tests for recordings/media API endpoints."""

from datetime import datetime, timezone

import pytz
from fastapi import Request

from frigate.api.auth import get_allowed_cameras_for_filter, get_current_user
from frigate.const import MAX_SEGMENT_DURATION
from frigate.models import Event, Recordings
from frigate.record.variants import apply_variant_filter, recordings_overlap_clause
from frigate.test.http_api.base_http_test import AuthTestClient, BaseTestHttp


class TestHttpMedia(BaseTestHttp):
    """Test media API endpoints, particularly recordings with DST handling."""

    def setUp(self):
        """Set up test fixtures."""
        super().setUp([Recordings])
        self.app = super().create_app()

        # Mock get_current_user for all tests
        async def mock_get_current_user(request: Request):
            username = request.headers.get("remote-user")
            role = request.headers.get("remote-role")
            if not username or not role:
                from fastapi.responses import JSONResponse

                return JSONResponse(
                    content={"message": "No authorization headers."}, status_code=401
                )
            return {"username": username, "role": role}

        self.app.dependency_overrides[get_current_user] = mock_get_current_user

        async def mock_get_allowed_cameras_for_filter(request: Request):
            return ["front_door"]

        self.app.dependency_overrides[get_allowed_cameras_for_filter] = (
            mock_get_allowed_cameras_for_filter
        )

    def tearDown(self):
        """Clean up after tests."""
        self.app.dependency_overrides.clear()
        super().tearDown()

    def test_recordings_summary_across_dst_spring_forward(self):
        """
        Test recordings summary across spring DST transition (spring forward).

        In 2024, DST in America/New_York transitions on March 10, 2024 at 2:00 AM
        Clocks spring forward from 2:00 AM to 3:00 AM (EST to EDT)
        """
        tz = pytz.timezone("America/New_York")

        # March 9, 2024 at 12:00 PM EST (before DST)
        march_9_noon = tz.localize(datetime(2024, 3, 9, 12, 0, 0)).timestamp()

        # March 10, 2024 at 12:00 PM EDT (after DST transition)
        march_10_noon = tz.localize(datetime(2024, 3, 10, 12, 0, 0)).timestamp()

        # March 11, 2024 at 12:00 PM EDT (after DST)
        march_11_noon = tz.localize(datetime(2024, 3, 11, 12, 0, 0)).timestamp()

        with AuthTestClient(self.app) as client:
            # Insert recordings for each day
            Recordings.insert(
                id="recording_march_9",
                path="/media/recordings/march_9.mp4",
                camera="front_door",
                start_time=march_9_noon,
                end_time=march_9_noon + 3600,  # 1 hour recording
                duration=3600,
                motion=100,
                objects=5,
            ).execute()

            Recordings.insert(
                id="recording_march_10",
                path="/media/recordings/march_10.mp4",
                camera="front_door",
                start_time=march_10_noon,
                end_time=march_10_noon + 3600,
                duration=3600,
                motion=150,
                objects=8,
            ).execute()

            Recordings.insert(
                id="recording_march_11",
                path="/media/recordings/march_11.mp4",
                camera="front_door",
                start_time=march_11_noon,
                end_time=march_11_noon + 3600,
                duration=3600,
                motion=200,
                objects=10,
            ).execute()

            # Test recordings summary with America/New_York timezone
            response = client.get(
                "/recordings/summary",
                params={"timezone": "America/New_York", "cameras": "all"},
            )

            assert response.status_code == 200
            summary = response.json()

            # Verify we get exactly 3 days
            assert len(summary) == 3, f"Expected 3 days, got {len(summary)}"

            # Verify the correct dates are returned (API returns dict with True values)
            assert "2024-03-09" in summary, f"Expected 2024-03-09 in {summary}"
            assert "2024-03-10" in summary, f"Expected 2024-03-10 in {summary}"
            assert "2024-03-11" in summary, f"Expected 2024-03-11 in {summary}"
            assert summary["2024-03-09"] is True
            assert summary["2024-03-10"] is True
            assert summary["2024-03-11"] is True

    def test_recordings_summary_across_dst_fall_back(self):
        """
        Test recordings summary across fall DST transition (fall back).

        In 2024, DST in America/New_York transitions on November 3, 2024 at 2:00 AM
        Clocks fall back from 2:00 AM to 1:00 AM (EDT to EST)
        """
        tz = pytz.timezone("America/New_York")

        # November 2, 2024 at 12:00 PM EDT (before DST transition)
        nov_2_noon = tz.localize(datetime(2024, 11, 2, 12, 0, 0)).timestamp()

        # November 3, 2024 at 12:00 PM EST (after DST transition)
        # Need to specify is_dst=False to get the time after fall back
        nov_3_noon = tz.localize(
            datetime(2024, 11, 3, 12, 0, 0), is_dst=False
        ).timestamp()

        # November 4, 2024 at 12:00 PM EST (after DST)
        nov_4_noon = tz.localize(datetime(2024, 11, 4, 12, 0, 0)).timestamp()

        with AuthTestClient(self.app) as client:
            # Insert recordings for each day
            Recordings.insert(
                id="recording_nov_2",
                path="/media/recordings/nov_2.mp4",
                camera="front_door",
                start_time=nov_2_noon,
                end_time=nov_2_noon + 3600,
                duration=3600,
                motion=100,
                objects=5,
            ).execute()

            Recordings.insert(
                id="recording_nov_3",
                path="/media/recordings/nov_3.mp4",
                camera="front_door",
                start_time=nov_3_noon,
                end_time=nov_3_noon + 3600,
                duration=3600,
                motion=150,
                objects=8,
            ).execute()

            Recordings.insert(
                id="recording_nov_4",
                path="/media/recordings/nov_4.mp4",
                camera="front_door",
                start_time=nov_4_noon,
                end_time=nov_4_noon + 3600,
                duration=3600,
                motion=200,
                objects=10,
            ).execute()

            # Test recordings summary with America/New_York timezone
            response = client.get(
                "/recordings/summary",
                params={"timezone": "America/New_York", "cameras": "all"},
            )

            assert response.status_code == 200
            summary = response.json()

            # Verify we get exactly 3 days
            assert len(summary) == 3, f"Expected 3 days, got {len(summary)}"

            # Verify the correct dates are returned (API returns dict with True values)
            assert "2024-11-02" in summary, f"Expected 2024-11-02 in {summary}"
            assert "2024-11-03" in summary, f"Expected 2024-11-03 in {summary}"
            assert "2024-11-04" in summary, f"Expected 2024-11-04 in {summary}"
            assert summary["2024-11-02"] is True
            assert summary["2024-11-03"] is True
            assert summary["2024-11-04"] is True

    def test_recordings_summary_multiple_cameras_across_dst(self):
        """
        Test recordings summary with multiple cameras across DST boundary.
        """
        tz = pytz.timezone("America/New_York")

        # March 9, 2024 at 10:00 AM EST (before DST)
        march_9_morning = tz.localize(datetime(2024, 3, 9, 10, 0, 0)).timestamp()

        # March 10, 2024 at 3:00 PM EDT (after DST transition)
        march_10_afternoon = tz.localize(datetime(2024, 3, 10, 15, 0, 0)).timestamp()

        with AuthTestClient(self.app) as client:
            # Override allowed cameras for this test to include both
            async def mock_get_allowed_cameras_for_filter(_request: Request):
                return ["front_door", "back_door"]

            self.app.dependency_overrides[get_allowed_cameras_for_filter] = (
                mock_get_allowed_cameras_for_filter
            )

            # Insert recordings for front_door on March 9
            Recordings.insert(
                id="front_march_9",
                path="/media/recordings/front_march_9.mp4",
                camera="front_door",
                start_time=march_9_morning,
                end_time=march_9_morning + 3600,
                duration=3600,
                motion=100,
                objects=5,
            ).execute()

            # Insert recordings for back_door on March 10
            Recordings.insert(
                id="back_march_10",
                path="/media/recordings/back_march_10.mp4",
                camera="back_door",
                start_time=march_10_afternoon,
                end_time=march_10_afternoon + 3600,
                duration=3600,
                motion=150,
                objects=8,
            ).execute()

            # Test with all cameras
            response = client.get(
                "/recordings/summary",
                params={"timezone": "America/New_York", "cameras": "all"},
            )

            assert response.status_code == 200
            summary = response.json()

            # Verify we get both days
            assert len(summary) == 2, f"Expected 2 days, got {len(summary)}"
            assert "2024-03-09" in summary
            assert "2024-03-10" in summary
            assert summary["2024-03-09"] is True
            assert summary["2024-03-10"] is True

            # Reset dependency override back to default single camera for other tests
            async def reset_allowed_cameras(_request: Request):
                return ["front_door"]

            self.app.dependency_overrides[get_allowed_cameras_for_filter] = (
                reset_allowed_cameras
            )

    def test_recordings_summary_at_dst_transition_time(self):
        """
        Test recordings that span the exact DST transition time.
        """
        tz = pytz.timezone("America/New_York")

        # March 10, 2024 at 1:00 AM EST (1 hour before DST transition)
        # At 2:00 AM, clocks jump to 3:00 AM
        before_transition = tz.localize(datetime(2024, 3, 10, 1, 0, 0)).timestamp()

        # Recording that spans the transition (1:00 AM to 3:30 AM EDT)
        # This is 1.5 hours of actual time but spans the "missing" hour
        after_transition = tz.localize(datetime(2024, 3, 10, 3, 30, 0)).timestamp()

        with AuthTestClient(self.app) as client:
            Recordings.insert(
                id="recording_during_transition",
                path="/media/recordings/transition.mp4",
                camera="front_door",
                start_time=before_transition,
                end_time=after_transition,
                duration=after_transition - before_transition,
                motion=100,
                objects=5,
            ).execute()

            response = client.get(
                "/recordings/summary",
                params={"timezone": "America/New_York", "cameras": "all"},
            )

            assert response.status_code == 200
            summary = response.json()

            # The recording should appear on March 10
            assert len(summary) == 1
            assert "2024-03-10" in summary
            assert summary["2024-03-10"] is True

    def test_recordings_summary_utc_timezone(self):
        """
        Test recordings summary with UTC timezone (no DST).
        """
        # Use UTC timestamps directly
        march_9_utc = datetime(2024, 3, 9, 17, 0, 0, tzinfo=timezone.utc).timestamp()
        march_10_utc = datetime(2024, 3, 10, 17, 0, 0, tzinfo=timezone.utc).timestamp()

        with AuthTestClient(self.app) as client:
            Recordings.insert(
                id="recording_march_9_utc",
                path="/media/recordings/march_9_utc.mp4",
                camera="front_door",
                start_time=march_9_utc,
                end_time=march_9_utc + 3600,
                duration=3600,
                motion=100,
                objects=5,
            ).execute()

            Recordings.insert(
                id="recording_march_10_utc",
                path="/media/recordings/march_10_utc.mp4",
                camera="front_door",
                start_time=march_10_utc,
                end_time=march_10_utc + 3600,
                duration=3600,
                motion=150,
                objects=8,
            ).execute()

            # Test with UTC timezone
            response = client.get(
                "/recordings/summary", params={"timezone": "utc", "cameras": "all"}
            )

            assert response.status_code == 200
            summary = response.json()

            # Verify we get both days
            assert len(summary) == 2
            assert "2024-03-09" in summary
            assert "2024-03-10" in summary
            assert summary["2024-03-09"] is True
            assert summary["2024-03-10"] is True

    def test_recordings_summary_no_recordings(self):
        """
        Test recordings summary when no recordings exist.
        """
        with AuthTestClient(self.app) as client:
            response = client.get(
                "/recordings/summary",
                params={"timezone": "America/New_York", "cameras": "all"},
            )

            assert response.status_code == 200
            summary = response.json()
            assert len(summary) == 0

    def test_recordings_summary_single_camera_filter(self):
        """
        Test recordings summary filtered to a single camera.
        """
        tz = pytz.timezone("America/New_York")
        march_10_noon = tz.localize(datetime(2024, 3, 10, 12, 0, 0)).timestamp()

        with AuthTestClient(self.app) as client:
            # Insert recordings for both cameras
            Recordings.insert(
                id="front_recording",
                path="/media/recordings/front.mp4",
                camera="front_door",
                start_time=march_10_noon,
                end_time=march_10_noon + 3600,
                duration=3600,
                motion=100,
                objects=5,
            ).execute()

            Recordings.insert(
                id="back_recording",
                path="/media/recordings/back.mp4",
                camera="back_door",
                start_time=march_10_noon,
                end_time=march_10_noon + 3600,
                duration=3600,
                motion=150,
                objects=8,
            ).execute()

            # Test with only front_door camera
            response = client.get(
                "/recordings/summary",
                params={"timezone": "America/New_York", "cameras": "front_door"},
            )

            assert response.status_code == 200
            summary = response.json()
            assert len(summary) == 1
            assert "2024-03-10" in summary
            assert summary["2024-03-10"] is True

    def _get_summary(self, client, path, **params):
        response = client.get(path, params=params)
        assert response.status_code == 200
        return response.json()

    def test_recordings_summary_skips_gap_days(self):
        """The loose index scan must return exactly the days that have
        recordings, jumping over empty days without inventing any."""
        base = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc).timestamp()
        day = 86400

        # recordings on Jun 1, Jun 5, Jun 6 (Jun 2-4 are empty)
        for idx, offset_days in enumerate((0, 4, 5)):
            start = base + offset_days * day
            Recordings.insert(
                id=f"gap_rec_{idx}",
                path=f"/media/recordings/gap_{idx}.mp4",
                camera="front_door",
                start_time=start,
                end_time=start + 30,
                duration=30,
                motion=1,
                objects=0,
            ).execute()

        with AuthTestClient(self.app) as client:
            summary = self._get_summary(
                client, "/recordings/summary", timezone="utc", cameras="all"
            )
            assert sorted(summary.keys()) == [
                "2024-06-01",
                "2024-06-05",
                "2024-06-06",
            ]
            assert all(v is True for v in summary.values())

    def test_recordings_summary_matches_legacy(self):
        """The optimized endpoint must return identical output to the legacy
        full-scan implementation across cameras, gap days and a DST boundary."""
        tz = pytz.timezone("America/New_York")
        timestamps = {
            "front_door": [
                tz.localize(datetime(2024, 3, 8, 9, 0, 0)).timestamp(),
                tz.localize(datetime(2024, 3, 10, 5, 0, 0)).timestamp(),  # after DST
                tz.localize(datetime(2024, 3, 14, 23, 30, 0)).timestamp(),
            ],
            "back_door": [
                tz.localize(datetime(2024, 3, 9, 0, 15, 0)).timestamp(),
                tz.localize(datetime(2024, 3, 10, 12, 0, 0)).timestamp(),
            ],
        }

        idx = 0
        for camera, starts in timestamps.items():
            for start in starts:
                Recordings.insert(
                    id=f"parity_{idx}",
                    path=f"/media/recordings/parity_{idx}.mp4",
                    camera=camera,
                    start_time=start,
                    end_time=start + 600,
                    duration=600,
                    motion=10,
                    objects=1,
                ).execute()
                idx += 1

        with AuthTestClient(self.app) as client:

            async def both_cameras(_request: Request):
                return ["front_door", "back_door"]

            self.app.dependency_overrides[get_allowed_cameras_for_filter] = both_cameras

            for cameras in ("all", "front_door", "front_door,back_door"):
                new = self._get_summary(
                    client,
                    "/recordings/summary",
                    timezone="America/New_York",
                    cameras=cameras,
                )
                legacy = self._get_summary(
                    client,
                    "/recordings/summary/legacy",
                    timezone="America/New_York",
                    cameras=cameras,
                )
                assert new == legacy, (
                    f"mismatch for cameras={cameras}: {new} != {legacy}"
                )

            async def reset_allowed_cameras(_request: Request):
                return ["front_door"]

            self.app.dependency_overrides[get_allowed_cameras_for_filter] = (
                reset_allowed_cameras
            )

    def test_recordings_summary_sub_hour_offset_zone(self):
        """Day boundaries must be correct for a zone with a :45 UTC offset."""
        tz = pytz.timezone("Asia/Kathmandu")  # +05:45, no DST

        # 23:50 local on Jun 1 and 00:10 local on Jun 2 -> two distinct local days
        late = tz.localize(datetime(2024, 6, 1, 23, 50, 0)).timestamp()
        early = tz.localize(datetime(2024, 6, 2, 0, 10, 0)).timestamp()
        for idx, start in enumerate((late, early)):
            Recordings.insert(
                id=f"ktm_{idx}",
                path=f"/media/recordings/ktm_{idx}.mp4",
                camera="front_door",
                start_time=start,
                end_time=start + 60,
                duration=60,
                motion=1,
                objects=0,
            ).execute()

        with AuthTestClient(self.app) as client:
            new = self._get_summary(
                client, "/recordings/summary", timezone="Asia/Kathmandu", cameras="all"
            )
            legacy = self._get_summary(
                client,
                "/recordings/summary/legacy",
                timezone="Asia/Kathmandu",
                cameras="all",
            )
            assert new == legacy
            assert sorted(new.keys()) == ["2024-06-01", "2024-06-02"]


class TestHttpVodVariants(BaseTestHttp):
    """Variant selection on vod and recordings endpoints (dual-stream)."""

    # fixed, far-past window so cache/now logic is deterministic
    T = 1700000000.0

    def setUp(self):
        super().setUp([Event, Recordings])
        self.app = super().create_app()

        async def mock_get_current_user(request: Request):
            username = request.headers.get("remote-user")
            role = request.headers.get("remote-role")
            if not username or not role:
                from fastapi.responses import JSONResponse

                return JSONResponse(
                    content={"message": "No authorization headers."}, status_code=401
                )
            return {"username": username, "role": role}

        self.app.dependency_overrides[get_current_user] = mock_get_current_user

        async def mock_get_allowed_cameras_for_filter(request: Request):
            return ["front_door"]

        self.app.dependency_overrides[get_allowed_cameras_for_filter] = (
            mock_get_allowed_cameras_for_filter
        )

    def tearDown(self):
        self.app.dependency_overrides.clear()
        super().tearDown()

    def _insert_dual(self):
        """One 20s segment per variant over the same wall-clock window."""
        self.insert_mock_recording(
            "rec-main", self.T, self.T + 20, variant="main", path="/rec/main/seg.mp4"
        )
        self.insert_mock_recording(
            "rec-sub", self.T, self.T + 20, variant="sub", path="/rec/sub/seg.mp4"
        )

    @staticmethod
    def _clip_paths(payload):
        return [c["path"] for seq in payload["sequences"] for c in seq["clips"]]

    def test_vod_path_form_selects_variant(self):
        self._insert_dual()
        with AuthTestClient(self.app) as client:
            for variant, path in (
                ("main", "/rec/main/seg.mp4"),
                ("sub", "/rec/sub/seg.mp4"),
            ):
                response = client.get(
                    f"/vod/front_door/start/{self.T}/end/{self.T + 20}/{variant}"
                )
                assert response.status_code == 200
                assert self._clip_paths(response.json()) == [path]

    def test_vod_query_form_equivalent_to_path_form(self):
        self._insert_dual()
        with AuthTestClient(self.app) as client:
            path_resp = client.get(
                f"/vod/front_door/start/{self.T}/end/{self.T + 20}/main"
            )
            query_resp = client.get(
                f"/vod/front_door/start/{self.T}/end/{self.T + 20}",
                params={"variant": "main"},
            )
            assert path_resp.status_code == query_resp.status_code == 200
            assert self._clip_paths(path_resp.json()) == self._clip_paths(
                query_resp.json()
            )

    def test_vod_falls_back_to_other_variant(self):
        # only main rows exist (single-stream camera / legacy history)
        self.insert_mock_recording(
            "rec-main", self.T, self.T + 20, variant="main", path="/rec/main/seg.mp4"
        )
        with AuthTestClient(self.app) as client:
            response = client.get(
                f"/vod/front_door/start/{self.T}/end/{self.T + 20}/sub"
            )
            assert response.status_code == 200
            assert self._clip_paths(response.json()) == ["/rec/main/seg.mp4"]

    def test_vod_clip_path_form(self):
        self._insert_dual()
        with AuthTestClient(self.app) as client:
            response = client.get(
                f"/vod/clip/front_door/start/{self.T}/end/{self.T + 20}/main"
            )
            assert response.status_code == 200
            payload = response.json()
            assert payload["discontinuity"] is True
            assert self._clip_paths(payload) == ["/rec/main/seg.mp4"]

    def test_vod_event_path_form(self):
        self._insert_dual()
        self.insert_mock_event("ev1", start_time=self.T, end_time=self.T + 20)
        with AuthTestClient(self.app) as client:
            response = client.get("/vod/event/ev1/main")
            assert response.status_code == 200
            assert self._clip_paths(response.json()) == ["/rec/main/seg.mp4"]

    def test_vod_hour_trailing_segment_dispatch(self):
        # "main"/"sub" in the tz slot must be treated as a variant (and must not
        # hit pytz with an invalid timezone); a real tz name must keep working.
        with AuthTestClient(self.app) as client:
            for trailing in ("main", "sub", "America,New_York"):
                response = client.get(f"/vod/2024-03/10/13/front_door/{trailing}")
                # no recordings inserted for that hour -> 404 from vod_ts, never
                # a 500 (which an UnknownTimeZoneError would produce)
                assert response.status_code == 404, (
                    f"{trailing}: expected 404, got {response.status_code}"
                )

    def test_vod_invalid_variant_rejected(self):
        self._insert_dual()
        with AuthTestClient(self.app) as client:
            for bad in ("hd", "all"):
                path_resp = client.get(
                    f"/vod/front_door/start/{self.T}/end/{self.T + 20}/{bad}"
                )
                assert path_resp.status_code == 422, f"path variant {bad}"
                query_resp = client.get(
                    f"/vod/front_door/start/{self.T}/end/{self.T + 20}",
                    params={"variant": bad},
                )
                assert query_resp.status_code == 422, f"query variant {bad}"

    def test_recordings_list_filters_by_variant(self):
        self._insert_dual()
        with AuthTestClient(self.app) as client:
            response = client.get(
                "/front_door/recordings",
                params={"after": self.T - 5, "before": self.T + 25, "variant": "main"},
            )
            assert response.status_code == 200
            rows = response.json()
            assert [r["variant"] for r in rows] == ["main"]

    def test_recordings_list_falls_back_for_single_variant_camera(self):
        # only main rows; the default (sub) request must return them anyway
        self.insert_mock_recording(
            "rec-main", self.T, self.T + 20, variant="main", path="/rec/main/seg.mp4"
        )
        with AuthTestClient(self.app) as client:
            response = client.get(
                "/front_door/recordings",
                params={"after": self.T - 5, "before": self.T + 25, "variant": "sub"},
            )
            assert response.status_code == 200
            rows = response.json()
            assert len(rows) == 1
            assert rows[0]["variant"] == "main"

    def test_recordings_list_window_boundaries(self):
        # inclusive edge semantics of the seekable overlap predicate must match
        # the legacy OR-of-BETWEENs it replaced
        T = self.T
        specs = {
            "b-spans-window": (T - 30, T + 130),
            "b-ends-at-start": (T - 20, T),
            "b-inside": (T + 40, T + 60),
            "b-starts-at-end": (T + 100, T + 120),
            "b-before": (T - 50, T - 1),
            "b-after": (T + 101, T + 120),
        }
        for rec_id, (start, end) in specs.items():
            self.insert_mock_recording(
                rec_id, start, end, variant="main", path=f"/{rec_id}"
            )
        with AuthTestClient(self.app) as client:
            response = client.get(
                "/front_door/recordings",
                params={"after": T, "before": T + 100, "variant": "main"},
            )
            assert response.status_code == 200
            assert [r["id"] for r in response.json()] == [
                "b-spans-window",
                "b-ends-at-start",
                "b-inside",
                "b-starts-at-end",
            ]

            # a window in a recording gap returns empty, not an error
            gap = client.get(
                "/front_door/recordings",
                params={"after": T + 10000, "before": T + 10100, "variant": "main"},
            )
            assert gap.status_code == 200
            assert gap.json() == []


class TestRecordingsOverlapQuery(BaseTestHttp):
    """DB-level guarantees for the seekable recordings overlap predicate."""

    T = 1700000000.0

    def setUp(self):
        super().setUp([Event, Recordings])

    def _legacy_overlap(self, start_ts: float, end_ts: float):
        # the pre-034 predicate shape, kept here as the semantic reference
        return (
            Recordings.start_time.between(start_ts, end_ts)
            | Recordings.end_time.between(start_ts, end_ts)
            | ((start_ts > Recordings.start_time) & (end_ts < Recordings.end_time))
        )

    def _ids(self, clause) -> set[str]:
        return {
            r.id
            for r in Recordings.select(Recordings.id).where(
                clause, Recordings.camera == "front_door"
            )
        }

    def test_matches_legacy_overlap_predicate(self):
        T = self.T
        n = 0
        for start_off in (-650, -600, -30, -20, 0, 40, 99, 100, 101, 150):
            for duration in (10, 20, 100, MAX_SEGMENT_DURATION):
                n += 1
                self.insert_mock_recording(
                    f"eq-{n}",
                    T + start_off,
                    T + start_off + duration,
                    variant="main",
                    path=f"/eq/{n}",
                )
        windows = [
            (T, T + 100),
            (T - MAX_SEGMENT_DURATION, T),
            (T + 100, T + 100),
            (T - 1000, T - 700),
            (T + 500, T + 600),
        ]
        for start_ts, end_ts in windows:
            legacy = self._ids(self._legacy_overlap(start_ts, end_ts))
            seekable = self._ids(recordings_overlap_clause(start_ts, end_ts))
            assert seekable == legacy, (
                f"window ({start_ts - T}, {end_ts - T}): difference {seekable ^ legacy}"
            )

    def test_overlong_segment_outside_seek_bound_is_excluded(self):
        # segments are duration-capped at MAX_SEGMENT_DURATION by the recorder;
        # a pathological longer row that started more than MAX_SEGMENT_DURATION
        # before the window is intentionally no longer matched — the price of
        # the two-sided start_time bound that makes the query an index seek
        T = self.T
        self.insert_mock_recording(
            "overlong",
            T - MAX_SEGMENT_DURATION - 50,
            T + 50,
            variant="main",
            path="/overlong",
        )
        assert self._ids(recordings_overlap_clause(T, T + 100)) == set()

    def test_query_plan_seeks_composite_index(self):
        # regression guard: the playback query shape must range-seek the
        # (camera, variant, start_time, end_time) index, not walk the whole
        # camera+variant partition
        self.insert_mock_recording(
            "qp-1", self.T, self.T + 20, variant="sub", path="/qp/1"
        )
        query = apply_variant_filter(
            Recordings.select(Recordings.id).where(
                Recordings.camera == "front_door",
                recordings_overlap_clause(self.T, self.T + 100),
            ),
            "sub",
        ).order_by(Recordings.start_time)
        sql, params = query.sql()
        plan = " ".join(
            str(row)
            for row in self.db.execute_sql(
                "EXPLAIN QUERY PLAN " + sql, params
            ).fetchall()
        )
        assert "recordings_camera_variant_start_time_end_time" in plan, plan
        # a two-sided start_time range in the plan detail proves the seek
        assert "start_time<" in plan and "start_time>" in plan, plan
