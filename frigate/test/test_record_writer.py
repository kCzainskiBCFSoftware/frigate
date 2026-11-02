"""Tests for segment rows written off the mover's path (frigate.record.writer)."""

import importlib
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

# sqlite3: the library peewee uses (pysqlite3 in the Frigate image), so the
# lock the test holds is seen by peewee's connections in this process
from peewee import SqliteDatabase, sqlite3
from playhouse.sqliteq import SqliteQueueDatabase

from frigate.config import FrigateConfig
from frigate.const import INSERT_MANY_RECORDINGS
from frigate.models import RecordingRangeCoverage, RecordingRanges, Recordings
from frigate.record import writer as writer_module
from frigate.record.ranges import reset_late_rows
from frigate.record.writer import (
    RecordingsWriter,
    insert_recording_rows,
    take_clean_shutdown_marker,
    write_clean_shutdown_marker,
)
from frigate.test.http_api.base_http_test import BaseTestHttp


def row(n: int, camera: str = "back", start: float = 1700000000.0) -> dict:
    return {
        "id": f"{start + n * 10}-r{n}",
        "camera": camera,
        "path": f"/rec/{camera}/{n}.mp4",
        "start_time": start + n * 10,
        "end_time": start + n * 10 + 10,
        "duration": 10,
        "motion": None,
        "objects": None,
        "regions": None,
        "dBFS": None,
        "segment_size": 1.0,
        "variant": "main",
    }


def config(**pipeline) -> FrigateConfig:
    return FrigateConfig(
        **{
            "mqtt": {"host": "mqtt"},
            "record": {"enabled": True, "pipeline": pipeline},
            "cameras": {
                "back": {
                    "ffmpeg": {
                        "inputs": [
                            {"path": "rtsp://10.0.0.1:554/video", "roles": ["detect"]}
                        ]
                    },
                    "detect": {"height": 1080, "width": 1920, "fps": 5},
                }
            },
        }
    )


class FakeRequestor:
    """Stands in for the REQ socket to the main process."""

    def __init__(self) -> None:
        self.open = threading.Event()
        self.open.set()
        self.fail_times = 0
        self.raise_times = 0
        self.batches: list[list[dict]] = []
        self.calls = 0

    def send_data(self, topic, data):
        assert topic == INSERT_MANY_RECORDINGS
        self.calls += 1
        self.open.wait()

        if self.raise_times > 0:
            self.raise_times -= 1
            raise TypeError("Object of type int64 is not JSON serializable")

        if self.fail_times > 0:
            self.fail_times -= 1
            return {"ok": False, "error": "database is locked"}

        self.batches.append(list(data))
        return {"ok": True, "count": len(data)}

    def stop(self) -> None:
        pass

    def written(self) -> list[str]:
        return [r["path"] for batch in self.batches for r in batch]


class FakePublisher:
    def __init__(self) -> None:
        self.saved: list[tuple] = []

    def publish(self, payload, topic) -> None:
        self.saved.append(tuple(payload))

    def stop(self) -> None:
        pass


def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class TestRecordingsWriter(unittest.TestCase):
    def make_writer(self, **pipeline) -> RecordingsWriter:
        self.requestor = FakeRequestor()
        self.publisher = FakePublisher()
        writer = RecordingsWriter(
            config(**pipeline),
            requestor_factory=lambda: self.requestor,
            publisher_factory=lambda: self.publisher,
        )
        writer.start()
        self.addCleanup(self.requestor.open.set)
        return writer

    def test_mover_keeps_moving_while_the_database_blocks(self):
        writer = self.make_writer()
        self.requestor.open.clear()

        start = time.monotonic()
        for n in range(20):
            writer.submit([row(n)], {"back": None})
        assert time.monotonic() - start < 0.5

        self.requestor.open.set()
        assert wait_until(lambda: len(self.requestor.written()) == 20)
        assert writer.stop(5)

    def test_full_queue_holds_the_mover_and_drops_nothing(self):
        writer = self.make_writer(db_write_queue=3)
        self.requestor.open.clear()
        assert writer.has_room()
        writer.submit([row(n) for n in range(3)], {})

        with self.assertLogs("frigate.record.writer", "ERROR") as logs:
            assert not writer.has_room()
            assert not writer.has_room()

        # one error per outage, however many passes are held
        assert len([line for line in logs.output if "queue is full" in line]) == 1

        # a pass already under way may still hand over its rows: submit never
        # waits, so it cannot hold up the mover or its stop
        start = time.monotonic()
        writer.submit([row(3)], {})
        assert time.monotonic() - start < 0.5

        self.requestor.open.set()
        assert wait_until(lambda: len(self.requestor.written()) == 4)
        assert sorted(self.requestor.written()) == sorted(
            row(n)["path"] for n in range(4)
        )
        assert writer.has_room()
        assert writer.stop(5)

    def test_missing_reply_reconnects(self):
        # a REQ socket that missed its reply refuses every later send
        with patch.object(writer_module, "RETRY_INITIAL", 0.02):
            requestor = FakeRequestor()
            factory = MagicMock(return_value=requestor)
            sent_once = []

            def send(topic, data):
                if not sent_once:
                    sent_once.append(True)
                    return ""

                return FakeRequestor.send_data(requestor, topic, data)

            requestor.send_data = send
            writer = RecordingsWriter(
                config(), requestor_factory=factory, publisher_factory=FakePublisher
            )
            writer.start()

            with self.assertLogs("frigate.record.writer", "WARNING"):
                writer.submit([row(0)], {})
                assert wait_until(lambda: len(requestor.written()) == 1)

        # the first connection, then a fresh one after the missing reply
        assert factory.call_count == 2
        assert writer.stop(5)

    def test_failed_batch_is_retried_and_outage_is_logged(self):
        with (
            patch.object(writer_module, "RETRY_INITIAL", 0.02),
            patch.object(writer_module, "FAILING_ERROR_AFTER", 0.05),
        ):
            writer = self.make_writer()
            self.requestor.fail_times = 4

            with self.assertLogs("frigate.record.writer", "INFO") as logs:
                writer.submit([row(0), row(1)], {})
                assert wait_until(lambda: len(self.requestor.written()) == 2)

        assert self.requestor.calls == 5
        levels = [line.split(":")[0] for line in logs.output]
        assert levels == ["WARNING", "ERROR", "INFO"], logs.output
        # every message carries the queue depth
        assert all("queued" in line for line in logs.output), logs.output
        assert writer.stop(5)

    def test_exception_from_send_is_retried_not_fatal(self):
        # an exception used to end the thread; rows then queued unwritten until
        # the full queue stopped the mover for good
        with patch.object(writer_module, "RETRY_INITIAL", 0.02):
            writer = self.make_writer()
            self.requestor.raise_times = 2

            with self.assertLogs("frigate.record.writer", "WARNING") as logs:
                writer.submit([row(0)], {})
                assert wait_until(lambda: len(self.requestor.written()) == 1)

        assert writer.is_alive()
        assert self.requestor.calls == 3
        assert any("TypeError" in line for line in logs.output)
        assert writer.stop(5)

    def test_exception_from_saved_publish_is_not_fatal(self):
        writer = self.make_writer()
        self.publisher.publish = MagicMock(side_effect=RuntimeError("socket closed"))

        with self.assertLogs("frigate.record.writer", "ERROR") as logs:
            writer.submit([row(0)], {"back": 1700000000.0})
            # the log line comes after the publish call, on the writer thread
            assert wait_until(lambda: logs.output)

        assert self.publisher.publish.called

        writer.submit([row(1)], {})
        assert wait_until(lambda: len(self.requestor.written()) == 2)
        assert writer.is_alive()
        assert writer.stop(5)

    def test_submit_does_not_wait_on_a_stopped_writer(self):
        def gone():
            raise SystemExit

        writer = RecordingsWriter(
            config(db_write_queue=1),
            requestor_factory=gone,
            publisher_factory=FakePublisher,
        )
        writer.start()
        writer.join(5)
        assert not writer.is_alive()

        # in a thread, so a regression fails the test instead of hanging it
        submitter = threading.Thread(
            target=writer.submit,
            args=([row(n) for n in range(3)], {}),
            daemon=True,
        )
        submitter.start()
        submitter.join(2)

        assert not submitter.is_alive(), "submit waited on a stopped writer"
        assert [r["path"] for r in writer.take_rows()] == [
            row(n)["path"] for n in range(3)
        ]
        # nothing left to write and the thread is gone: a clean stop
        assert writer.stop(1)

    def test_saved_waits_for_queued_rows(self):
        writer = self.make_writer()
        self.requestor.fail_times = 1000
        start = 1700000000.0

        # rows for segments starting at `start` are queued but not written; the
        # mover reports the cache's oldest segment 60s later
        writer.submit([row(0, start=start)], {"back": start + 60})
        time.sleep(0.3)
        assert all(
            saved is None or saved <= start for _, saved, _ in self.publisher.saved
        )

        self.requestor.fail_times = 0
        assert wait_until(lambda: self.publisher.saved)
        assert self.publisher.saved[-1] == ("back", start + 60, None)
        assert writer.stop(5)

    def test_saved_published_straight_away_with_nothing_queued(self):
        writer = self.make_writer()

        writer.submit([], {"back": 1700000000.0})

        assert wait_until(lambda: self.publisher.saved)
        assert self.publisher.saved[-1] == ("back", 1700000000.0, None)
        assert writer.stop(5)

    def test_stop_flushes_queued_rows(self):
        writer = self.make_writer()
        self.requestor.open.clear()
        writer.submit([row(n) for n in range(200)], {})

        threading.Timer(0.2, self.requestor.open.set).start()

        assert writer.stop(5)
        assert len(self.requestor.written()) == 200

    def test_stop_reports_rows_it_could_not_write(self):
        writer = self.make_writer()
        self.requestor.fail_times = 10**6
        writer.submit([row(n) for n in range(3)], {})

        with self.assertLogs("frigate.record.writer", "ERROR") as logs:
            assert not writer.stop(0.5)

        line = logs.output[-1]
        assert "3 recording rows were not written" in line
        assert row(0)["path"] in line and row(2)["path"] in line

        # let the abandoned thread finish instead of retrying for later tests
        self.requestor.fail_times = 0
        writer.join(5)


class TestCleanShutdownMarker(unittest.TestCase):
    def test_marker_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "frigate.db")

            assert not take_clean_shutdown_marker(db)
            write_clean_shutdown_marker(db)
            assert take_clean_shutdown_marker(db)
            # taken: the next start without a clean stop is unclean
            assert not take_clean_shutdown_marker(db)


class TestInsertHandler(BaseTestHttp):
    def setUp(self):
        super().setUp([Recordings, RecordingRanges, RecordingRangeCoverage])
        reset_late_rows()

    def tearDown(self):
        # close this test's extra connections before the database files go
        self.doCleanups()
        super().tearDown()

    def test_resent_batch_stores_each_segment_once(self):
        batch = [row(n) for n in range(3)]

        assert insert_recording_rows(batch) == {"ok": True, "count": 3}
        assert insert_recording_rows(batch) == {"ok": True, "count": 3}
        assert Recordings.select().count() == 3

    def test_unknown_stats_are_stored_as_null(self):
        insert_recording_rows([row(0)])

        stored = Recordings.get(Recordings.path == row(0)["path"])
        assert stored.motion is None and stored.objects is None
        assert stored.dBFS is None and stored.regions is None

    def _lock_database(self) -> sqlite3.Connection:
        holder = sqlite3.connect(self.lock_db_path, isolation_level=None)
        holder.execute("BEGIN IMMEDIATE")
        self.addCleanup(holder.close)
        return holder

    def _short_timeout_db(self) -> SqliteQueueDatabase:
        # a database of its own on the container filesystem: a second writer
        # on the shared test.db is unreliable over a Windows bind mount
        models = [Recordings, RecordingRanges, RecordingRangeCoverage]
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.lock_db_path = os.path.join(tmp, "locked.db")
        schema = SqliteDatabase(self.lock_db_path, pragmas={"journal_mode": "wal"})
        schema.bind(models)
        schema.create_tables(models)
        schema.close()

        db = SqliteQueueDatabase(self.lock_db_path, timeout=0.5)
        db.bind(models)
        self.addCleanup(db.stop)
        # wait for one queued write, so its writer thread has connected (and
        # set its pragmas) before the test takes the lock
        db.execute_sql("DELETE FROM recordings WHERE 0", timeout=5).rowcount
        return db

    def test_locked_database_is_a_failed_outcome(self):
        self._short_timeout_db()
        holder = self._lock_database()

        result = insert_recording_rows([row(0)])

        assert result["ok"] is False
        assert "locked" in result["error"]

        holder.execute("ROLLBACK")
        assert insert_recording_rows([row(0)]) == {"ok": True, "count": 1}

    def test_request_thread_keeps_serving_after_a_failed_insert(self):
        inter_process = real_inter_process()
        os.makedirs("/tmp/cache", exist_ok=True)

        def dispatch(topic, payload):
            if topic == INSERT_MANY_RECORDINGS:
                return insert_recording_rows(payload)
            return {"pong": payload}

        communicator = inter_process.InterProcessCommunicator()
        communicator.subscribe(dispatch)
        self.addCleanup(communicator.stop)
        requestor = inter_process.InterProcessRequestor()
        self.addCleanup(requestor.stop)

        self._short_timeout_db()
        holder = self._lock_database()

        failed = requestor.send_data(INSERT_MANY_RECORDINGS, [row(0)])
        assert failed["ok"] is False

        # another process's request, e.g. a camera status update
        assert requestor.send_data("back/status/detect", "online") == {"pong": "online"}

        holder.execute("ROLLBACK")
        assert requestor.send_data(INSERT_MANY_RECORDINGS, [row(0)])["ok"]


def real_inter_process():
    """The real comms module, even if another test module mocked it."""
    name = "frigate.comms.inter_process"

    if isinstance(sys.modules.get(name), MagicMock):
        del sys.modules[name]

    return importlib.import_module(name)


if __name__ == "__main__":
    unittest.main()
