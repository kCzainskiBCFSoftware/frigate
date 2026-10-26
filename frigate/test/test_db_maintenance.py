"""Tests for the non-blocking WAL maintenance in frigate.db.maintenance."""

import logging
import os
import shutil
import tempfile
import threading
import time
import unittest

# the SQLite library peewee uses (pysqlite3 in the Frigate image). A second
# library in the same process would not see this one's locks: POSIX locks
# never conflict within a process, and each library tracks its own.
from peewee import sqlite3
from playhouse.sqlite_ext import SqliteExtDatabase

from frigate.config.database import DatabaseConfig
from frigate.db.maintenance import (
    MB,
    CleanupRunSummary,
    bounded_truncate_checkpoint,
    maintain_wal,
    passive_checkpoint,
    size_pragmas,
    wal_size,
)

BLOB = b"x" * 4096


class WalTestBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "frigate.db")
        self.database = DatabaseConfig(path=self.path, wal_size_limit=1)

        conn = self._connect()
        conn.execute("PRAGMA journal_mode=wal")
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, data BLOB)")

        # the last connection to close checkpoints and deletes the WAL, so one
        # idle connection stays open for the whole test, as Frigate's do
        self.open: list[sqlite3.Connection] = [conn]

    def tearDown(self):
        for conn in self.open:
            conn.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _connect(self, timeout: float = 5.0) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=timeout, isolation_level=None)
        # keep frames in the WAL so the tests control when they are copied
        conn.execute("PRAGMA wal_autocheckpoint=0")
        return conn

    def _write(self, megabytes: float) -> None:
        conn = self._connect()
        conn.execute("BEGIN")
        for _ in range(int(megabytes * MB / len(BLOB))):
            conn.execute("INSERT INTO t (data) VALUES (?)", (BLOB,))
        conn.execute("COMMIT")
        conn.close()

    def _pin_snapshot(self) -> sqlite3.Connection:
        """A reader that holds an open read transaction on the current snapshot."""
        reader = self._connect()
        reader.execute("BEGIN")
        reader.execute("SELECT count(*) FROM t").fetchone()
        self.open.append(reader)
        return reader


class TestSizePragmas(WalTestBase):
    def test_new_connections_get_size_pragmas(self):
        database = DatabaseConfig(path=self.path, wal_size_limit=32, cache_size=96)
        db = SqliteExtDatabase(self.path, pragmas=size_pragmas(database))

        assert db.execute_sql("PRAGMA journal_size_limit").fetchone()[0] == 32 * MB
        assert db.execute_sql("PRAGMA cache_size").fetchone()[0] == -96 * 1000
        db.close()

    def test_defaults_match_fork_values(self):
        pragmas = size_pragmas(DatabaseConfig(path=self.path))

        assert pragmas["cache_size"] == -256 * 1000
        assert pragmas["journal_size_limit"] == 64 * MB

    def test_wal_is_cut_back_to_limit_when_it_restarts(self):
        self._write(5)
        assert wal_size(self.path) > 4 * MB

        # nothing pins the old frames, so the passive checkpoint completes
        busy, log, checkpointed = passive_checkpoint(self.database)
        assert busy == 0 and log == checkpointed

        # the next writer restarts the WAL and truncates it to the limit
        db = SqliteExtDatabase(self.path, pragmas=size_pragmas(self.database))
        db.execute_sql("INSERT INTO t (data) VALUES (?)", (BLOB,))
        db.close()

        assert wal_size(self.path) <= self.database.wal_size_limit * MB


class TestCheckpoints(WalTestBase):
    def test_passive_checkpoint_does_not_block_writers(self):
        self._write(1)
        self._pin_snapshot()
        self._write(1)

        start = time.monotonic()
        busy, log, checkpointed = passive_checkpoint(self.database)
        assert time.monotonic() - start < 1.0
        # the pinned snapshot keeps the newer frames in the WAL
        assert checkpointed < log

        # a writer that will not wait at all still gets in
        writer = self._connect(timeout=0)
        self.open.append(writer)
        writer.execute("INSERT INTO t (data) VALUES (?)", (BLOB,))

    def test_truncate_fallback_gives_up_within_its_timeout(self):
        self._write(1)
        self._pin_snapshot()
        self._write(1)

        start = time.monotonic()
        result = bounded_truncate_checkpoint(self.database, timeout=2.0)
        elapsed = time.monotonic() - start

        assert result is None or result[0] != 0, result
        assert elapsed < 3.5, elapsed

    def test_truncate_fallback_holds_writers_only_while_it_waits(self):
        # the reason PASSIVE is the default: a TRUNCATE waiting on a reader
        # holds the writer lock for as long as it waits
        self._write(1)
        self._pin_snapshot()
        self._write(1)

        checkpoint = threading.Thread(
            target=bounded_truncate_checkpoint, args=(self.database, 2.0)
        )
        checkpoint.start()
        time.sleep(0.5)

        writer = self._connect(timeout=0.1)
        self.open.append(writer)
        with self.assertRaises(sqlite3.OperationalError):
            writer.execute("INSERT INTO t (data) VALUES (?)", (BLOB,))

        checkpoint.join()
        writer.execute("INSERT INTO t (data) VALUES (?)", (BLOB,))


class TestMaintainWal(WalTestBase):
    def test_passive_mode_skips_fallback_for_small_wal(self):
        self._write(2)

        result = maintain_wal(self.database)

        assert result.mode == "passive"
        assert result.passive is not None
        assert not result.truncate_attempted
        assert result.wal_before >= 2 * MB

    def test_passive_mode_falls_back_and_gives_up_on_oversized_wal(self):
        self._write(1)
        self._pin_snapshot()
        self._write(5)

        with self.assertLogs("frigate.db.maintenance", logging.WARNING) as logs:
            start = time.monotonic()
            result = maintain_wal(self.database)
            elapsed = time.monotonic() - start

        assert result.truncate_attempted
        assert result.truncate is None or result.truncate[0] != 0
        assert elapsed < 3.5, elapsed
        assert any("MB" in line for line in logs.output)

    def test_truncate_mode_restores_upstream_threshold(self):
        database = DatabaseConfig(path=self.path, wal_checkpoint="truncate")

        self._write(2)
        result = maintain_wal(database)
        assert not result.truncate_attempted
        assert result.passive is None

        self._write(10)
        result = maintain_wal(database)
        assert result.truncate_attempted
        assert result.truncate is not None and result.truncate[0] == 0
        assert result.wal_after == 0


class TestCleanupRunSummary(unittest.TestCase):
    def test_summary_reports_phases_and_checkpoint(self):
        summary = CleanupRunSummary()

        for name in ("expire", "directories", "checkpoint", "stats", "ranges"):
            with summary.phase(name):
                pass

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "frigate.db")
            conn = sqlite3.connect(path)
            conn.execute("PRAGMA journal_mode=wal")
            conn.execute("CREATE TABLE t (id INTEGER)")
            conn.close()
            summary.checkpoint = maintain_wal(DatabaseConfig(path=path))

        with self.assertLogs("frigate.db.maintenance", logging.INFO) as logs:
            summary.log()

        line = logs.output[0]
        for name in ("expire", "directories", "checkpoint", "stats", "ranges"):
            assert f"{name} " in line, line
        assert "MB before" in line and "MB after" in line, line
        assert "passive (busy=" in line, line


if __name__ == "__main__":
    unittest.main()
