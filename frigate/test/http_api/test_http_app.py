from unittest.mock import Mock

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
