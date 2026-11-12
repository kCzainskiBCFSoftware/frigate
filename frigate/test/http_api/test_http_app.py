import threading
import time
from unittest.mock import Mock, patch

from frigate.models import Event, Recordings, ReviewSegment
from frigate.stats.emitter import StatsEmitter
from frigate.test.http_api.base_http_test import AuthTestClient, BaseTestHttp


class TestHttpApp(BaseTestHttp):
    def setUp(self):
        super().setUp([Event, Recordings, ReviewSegment])
        self.app = super().create_app()

    ####################################################################################################################
    ###################################  GET /stats Endpoint   #########################################################
    ####################################################################################################################
    def test_stats_endpoint(self):
        stats = Mock(spec=StatsEmitter)
        stats.get_latest_stats.return_value = self.test_stats
        app = super().create_app(stats)

        with AuthTestClient(app) as client:
            response = client.get("/stats")
            response_json = response.json()
            assert response_json == self.test_stats

    ####################################################################################################################
    ###################################  GET /config fork block   ######################################################
    ####################################################################################################################
    def test_config_advertises_fork_features(self):
        with AuthTestClient(self.app) as client:
            response = client.get("/config")

        assert response.status_code == 200
        fork = response.json()["fork"]
        assert set(fork) == {"version", "features"}
        # append-only: every shipped flag stays, in sorted order
        assert fork["features"] == sorted(fork["features"])
        assert {
            "recording_variants",
            "recordings_ranges",
            "recordings_hours",
            "recordings_unknown_stats",
        } <= set(fork["features"])

    ####################################################################################################################
    ###################################  POST /restart Endpoint   ######################################################
    ####################################################################################################################
    def test_restart_replies_before_it_restarts(self):
        called = threading.Event()
        called_at: list[float] = []

        def record_call():
            called_at.append(time.monotonic())
            called.set()

        with patch("frigate.api.app.restart_frigate", side_effect=record_call) as m:
            with AuthTestClient(self.app) as client:
                sent_at = time.monotonic()
                response = client.post("/restart")
                replied_at = time.monotonic()

                assert response.status_code == 200
                assert response.json() == {
                    "success": True,
                    "message": "Restarting (this can take up to one minute)...",
                }
                # the reply is out before the shutdown starts
                m.assert_not_called()

                assert called.wait(timeout=5), "restart_frigate was never called"

        m.assert_called_once_with()
        # about 1 s after the request, never before the reply
        assert called_at[0] >= replied_at
        assert 0.9 <= called_at[0] - sent_at < 4

    def test_restart_is_admin_only(self):
        with patch("frigate.api.app.restart_frigate") as m:
            with AuthTestClient(self.app) as client:
                response = client.post("/restart", headers={"remote-role": "viewer"})
            # a timer started by mistake would fire within this window
            time.sleep(1.5)

        assert response.status_code == 403
        m.assert_not_called()

    def test_restart_schedule_failure_is_a_500(self):
        with (
            patch("frigate.api.app.restart_frigate") as m,
            patch(
                "frigate.api.app.threading.Timer.start",
                side_effect=RuntimeError("can't start new thread"),
            ),
        ):
            with AuthTestClient(self.app) as client:
                response = client.post("/restart")

        assert response.status_code == 500
        assert response.json() == {
            "success": False,
            "message": "Unable to restart Frigate.",
        }
        m.assert_not_called()

    def test_restart_error_in_timer_is_logged_not_raised(self):
        done = threading.Event()

        def fail():
            done.set()
            raise RuntimeError("boom")

        with patch("frigate.api.app.restart_frigate", side_effect=fail):
            with self.assertLogs("frigate.api.app", level="ERROR") as logs:
                with AuthTestClient(self.app) as client:
                    response = client.post("/restart")
                assert response.status_code == 200
                assert done.wait(timeout=5)
                # let the timer thread reach the log call
                time.sleep(0.2)

        assert any("Error restarting Frigate: boom" in line for line in logs.output)
