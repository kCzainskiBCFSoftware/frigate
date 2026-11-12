"""Tests for the main-process watchdog's request-thread check (frigate.watchdog)."""

import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from frigate.watchdog import FrigateWatchdog


def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


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
        # never the real os._exit: it would end the test run
        exit_patcher = patch("frigate.watchdog._hard_exit")
        self.hard_exit = exit_patcher.start()
        self.addCleanup(exit_patcher.stop)
        grace_patcher = patch("frigate.watchdog.EXIT_LOG_GRACE", 0)
        grace_patcher.start()
        self.addCleanup(grace_patcher.stop)
        self.stop_event = threading.Event()

    def watchdog(self, inter_process, busy_timeout=320, detectors=None, **kwargs):
        watchdog = FrigateWatchdog(
            detectors or {},
            self.stop_event,
            inter_process=inter_process,
            busy_timeout=busy_timeout,
            **kwargs,
        )
        # an armed exit deadline must not outlive the test (cleanups run in
        # reverse, so this runs while _hard_exit is still patched)
        self.addCleanup(
            lambda: watchdog.exit_timer is not None and watchdog.exit_timer.cancel()
        )
        return watchdog

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

    def test_restart_exits_if_frigate_has_not_stopped_by_the_deadline(self):
        # e.g. the stats emitter blocked in a request to the dead thread keeps
        # FrigateApp.stop() from finishing
        watchdog = self.watchdog(communicator(alive=False), restart_deadline=0.05)

        with self.assertLogs("frigate.watchdog", "ERROR") as logs:
            watchdog.check()
            assert wait_until(lambda: self.hard_exit.called, 2)

        assert self.restart.call_count == 1
        assert self.hard_exit.call_count == 1
        assert "has not stopped" in logs.output[-1], logs.output
        # a process that does stop in time is not kept alive by the deadline
        assert watchdog.exit_timer.daemon

    def test_deadline_is_armed_only_by_a_request_thread_restart(self):
        watchdog = self.watchdog(
            communicator(busy_for=300), detectors={"cpu": detector(alive=False)}
        )
        watchdog.restart_deadline = 0.01
        watchdog.check()
        time.sleep(0.2)

        # the upstream dead-detector restart is unchanged
        assert self.restart.call_count == 1
        assert watchdog.exit_timer is None
        self.hard_exit.assert_not_called()

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
