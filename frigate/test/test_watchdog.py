"""Tests for the main-process watchdog's request-thread check (frigate.watchdog)."""

import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from frigate.watchdog import FrigateWatchdog


def communicator(alive: bool = True, busy_for: float | None = None, topic="t"):
    reader = MagicMock()
    reader.is_alive.return_value = alive
    return SimpleNamespace(
        reader_thread=reader,
        busy=None if busy_for is None else (topic, time.monotonic() - busy_for),
        stop_event=threading.Event(),
    )


def detector(alive: bool = True):
    return SimpleNamespace(
        detection_start=SimpleNamespace(value=0.0),
        detect_process=SimpleNamespace(is_alive=lambda: alive),
        start_or_restart=MagicMock(),
    )


class TestRequestThreadWatchdog(unittest.TestCase):
    def setUp(self):
        patcher = patch("frigate.watchdog.restart_frigate")
        self.restart = patcher.start()
        self.addCleanup(patcher.stop)
        self.stop_event = threading.Event()

    def watchdog(self, inter_process, busy_timeout=320, detectors=None):
        return FrigateWatchdog(
            detectors or {},
            self.stop_event,
            inter_process=inter_process,
            busy_timeout=busy_timeout,
        )

    def test_dead_request_thread_restarts_once(self):
        watchdog = self.watchdog(communicator(alive=False))

        with self.assertLogs("frigate.watchdog", "ERROR") as logs:
            watchdog.check()
            watchdog.check()

        assert self.restart.call_count == 1
        assert "inter-process request thread" in logs.output[0]

    def test_stuck_request_restarts_with_its_topic(self):
        # 32 cameras: busy timeout 320 s, so the limit is 640 s
        watchdog = self.watchdog(
            communicator(busy_for=641, topic="insert_many_recordings")
        )

        with self.assertLogs("frigate.watchdog", "ERROR") as logs:
            watchdog.check()

        assert self.restart.call_count == 1
        assert "insert_many_recordings" in logs.output[0]
        assert "641s" in logs.output[0], logs.output

    def test_long_but_finite_request_does_not_restart(self):
        self.watchdog(communicator(busy_for=300)).check()
        # the floor: a short busy timeout still allows 600 s
        self.watchdog(communicator(busy_for=599), busy_timeout=60).check()
        self.watchdog(communicator()).check()

        self.restart.assert_not_called()

    def test_no_restart_while_frigate_stops(self):
        self.stop_event.set()
        self.watchdog(communicator(alive=False)).check()

        self.stop_event.clear()
        stopping = communicator(alive=False)
        stopping.stop_event.set()
        self.watchdog(stopping).check()

        self.restart.assert_not_called()

    def test_without_inter_process_only_detectors_are_checked(self):
        # the upstream signature still works
        watchdog = FrigateWatchdog({"cpu": detector(alive=True)}, self.stop_event)
        watchdog.check()
        self.restart.assert_not_called()

        watchdog = FrigateWatchdog({"cpu": detector(alive=False)}, self.stop_event)
        watchdog.check()
        assert self.restart.call_count == 1


if __name__ == "__main__":
    unittest.main()
