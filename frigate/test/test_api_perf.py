"""Unit tests for the media-endpoint performance logging."""

import json
import logging
import os
import tempfile
import unittest

from frigate.api import perf


class TestApiPerfLogging(unittest.TestCase):
    def setUp(self):
        # detach any handler a previous test/app setup attached so each test
        # exercises setup from scratch
        for handler in list(perf._perf_logger.handlers):
            perf._perf_logger.removeHandler(handler)
            handler.close()
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "frigate.db")

    def tearDown(self):
        for handler in list(perf._perf_logger.handlers):
            perf._perf_logger.removeHandler(handler)
            handler.close()
        self.tmp.cleanup()

    def _log_path(self) -> str:
        return os.path.join(
            self.tmp.name, perf.PERF_LOG_DIR_NAME, perf.PERF_LOG_FILE_NAME
        )

    def test_writes_json_line_next_to_db(self):
        perf.setup_api_perf_logging(self.db_path)
        perf.log_api_perf(
            {
                "endpoint": "clip.mp4",
                "camera": "front_door",
                "sql_ms": 1.23456,
                "segments": 3,
            }
        )

        with open(self._log_path()) as f:
            lines = f.read().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["endpoint"] == "clip.mp4"
        assert record["camera"] == "front_door"
        assert record["sql_ms"] == 1.2  # ms fields rounded
        assert record["segments"] == 3
        assert "ts" in record

    def test_setup_is_idempotent(self):
        perf.setup_api_perf_logging(self.db_path)
        perf.setup_api_perf_logging(self.db_path)
        assert len(perf._perf_logger.handlers) == 1

    def test_unwritable_location_degrades_to_noop(self):
        # a path whose parent is a file cannot host the log dir
        blocker = os.path.join(self.tmp.name, "blocker")
        with open(blocker, "w") as f:
            f.write("x")
        perf.setup_api_perf_logging(os.path.join(blocker, "frigate.db"))
        assert isinstance(perf._perf_logger.handlers[0], logging.NullHandler)
        # must not raise
        perf.log_api_perf({"endpoint": "vod"})

    def test_does_not_propagate_to_root_logger(self):
        assert perf._perf_logger.propagate is False
