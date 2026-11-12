"""Tests for the main process's request thread and timed requestors
(frigate.comms.inter_process), over a real REP/REQ pair."""

import importlib
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch


def real_inter_process():
    """The real comms module, even if another test module mocked it."""
    name = "frigate.comms.inter_process"

    if isinstance(sys.modules.get(name), MagicMock):
        del sys.modules[name]

    return importlib.import_module(name)


def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class InterProcessTestBase(unittest.TestCase):
    def setUp(self):
        self.ipc = real_inter_process()
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        patcher = patch.object(
            self.ipc, "SOCKET_REP_REQ", f"ipc://{os.path.join(tmp, 'comms')}"
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        self.release = threading.Event()
        self.addCleanup(self.release.set)

    def dispatch(self, topic, payload):
        if topic == "raise":
            raise KeyError(payload)
        if topic == "set":
            return {1, 2}
        if topic == "slow":
            self.release.wait(10)
            return {"slow": payload}
        return {"pong": payload}

    def communicator(self):
        communicator = self.ipc.InterProcessCommunicator()
        communicator.subscribe(self.dispatch)
        self.addCleanup(communicator.stop)
        return communicator

    def requestor(self, timeout=5.0):
        # timed by default, so a request left unanswered fails the test
        # instead of hanging it
        requestor = self.ipc.InterProcessRequestor(timeout=timeout)
        self.addCleanup(requestor.stop)
        return requestor


class TestRequestThread(InterProcessTestBase):
    def test_raising_handler_is_answered_and_the_thread_keeps_serving(self):
        communicator = self.communicator()
        first, second = self.requestor(), self.requestor()

        with self.assertLogs(self.ipc.logger, "ERROR") as logs:
            assert first.send_data("raise", "deleted-event") == []

        assert "raise" in logs.output[0] and "KeyError" in logs.output[0]
        # e.g. a camera activity update from another process
        assert second.send_data("camera_activity", "on") == {"pong": "on"}
        assert first.send_data("camera_activity", "off") == {"pong": "off"}
        assert communicator.reader_thread.is_alive()

    def test_result_that_cannot_be_sent_gets_an_empty_result(self):
        communicator = self.communicator()
        requestor = self.requestor()

        with self.assertLogs(self.ipc.logger, "ERROR") as logs:
            assert requestor.send_data("set", None) == []

        assert "set" in logs.output[0]
        assert requestor.send_data("ping", 1) == {"pong": 1}
        assert communicator.reader_thread.is_alive()

    def test_failures_of_one_topic_log_one_traceback_per_window(self):
        communicator = self.communicator()
        requestor = self.requestor()

        with self.assertLogs(self.ipc.logger, "ERROR") as logs:
            for n in range(50):
                assert requestor.send_data("raise", n) == []

        assert len(logs.records) == 1
        assert logs.records[0].exc_info is not None

        # the next window: one traceback, with the count it did not log
        with (
            patch.object(self.ipc, "ERROR_LOG_INTERVAL", 0),
            self.assertLogs(self.ipc.logger, "ERROR") as logs,
        ):
            assert requestor.send_data("raise", 50) == []

        assert len(logs.records) == 1
        assert "49 more failures" in logs.output[0], logs.output
        assert communicator.reader_thread.is_alive()

    def test_busy_names_the_request_in_progress(self):
        communicator = self.communicator()
        requestor = self.requestor()
        assert communicator.busy is None

        replies = []
        sender = threading.Thread(
            target=lambda: replies.append(requestor.send_data("slow", 7)), daemon=True
        )
        sender.start()

        assert wait_until(lambda: communicator.busy is not None)
        topic, started = communicator.busy
        assert topic == "slow"
        assert started <= time.monotonic()

        self.release.set()
        sender.join(5)
        assert replies == [{"slow": 7}]
        assert wait_until(lambda: communicator.busy is None)
        assert communicator.reader_thread.is_alive()

    def test_thread_survives_every_failure_kind(self):
        communicator = self.communicator()
        requestor = self.requestor()

        with self.assertLogs(self.ipc.logger, "ERROR"):
            requestor.send_data("raise", 1)
            requestor.send_data("set", 2)
            # a request that is not a (topic, payload) pair
            requestor.socket.send_json(["only-a-topic"])
            assert requestor.socket.recv_json() == []

        assert requestor.send_data("ping", 3) == {"pong": 3}
        assert communicator.reader_thread.is_alive()


class TestTimedRequestor(InterProcessTestBase):
    def test_no_reply_returns_within_the_timeout_and_the_next_request_works(self):
        communicator = self.communicator()
        requestor = self.requestor(timeout=0.5)

        start = time.monotonic()
        with self.assertLogs(self.ipc.logger, "WARNING") as logs:
            assert requestor.send_data("slow", 1) == ""

        assert time.monotonic() - start < 1.5
        assert "slow" in logs.output[0]

        # the main process answers again: the next request on the same
        # requestor goes over a new connection and gets its own reply
        self.release.set()
        assert requestor.send_data("ping", 2) == {"pong": 2}
        assert requestor.send_data("ping", 3) == {"pong": 3}
        assert communicator.reader_thread.is_alive()

    def test_untimed_requestor_waits_for_its_reply(self):
        self.communicator()
        requestor = self.requestor(timeout=None)
        threading.Timer(1.0, self.release.set).start()

        assert requestor.send_data("slow", 1) == {"slow": 1}


if __name__ == "__main__":
    unittest.main()
