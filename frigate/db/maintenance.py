"""Database maintenance that must not block the recording path (fork-only).

Upstream ends every recordings cleanup run with ``wal_checkpoint(TRUNCATE)``,
which blocks writers while it waits for readers and while it copies. On a busy
device that stalls the segment-row inserts behind it. Here the closing
checkpoint is PASSIVE, the WAL file is bounded with ``journal_size_limit`` on
every connection, and a blocking TRUNCATE remains only as a fallback for an
oversized WAL, with a short busy timeout so it gives up instead of waiting.
"""

import logging
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator, Optional

from peewee import OperationalError
from playhouse.sqlite_ext import SqliteExtDatabase

from frigate.config.database import DatabaseConfig, WalCheckpointEnum
from frigate.const import MAX_WAL_SIZE

logger = logging.getLogger(__name__)

MB = 1024 * 1024

# How long the TRUNCATE fallback waits for other connections before giving up.
TRUNCATE_BUSY_TIMEOUT = 2.0

# The TRUNCATE fallback runs only when the WAL is still larger than this many
# times wal_size_limit after the passive checkpoint.
TRUNCATE_FALLBACK_FACTOR = 4

# (busy, log, checkpointed) as returned by PRAGMA wal_checkpoint
CheckpointRow = tuple[int, int, int]


def size_pragmas(database: DatabaseConfig) -> dict[str, int]:
    """Per-connection memory and WAL-size pragmas from the database config."""
    return {
        # a negative cache_size is in KiB; the fork has always written MB x 1000
        "cache_size": -database.cache_size * 1000,
        # SQLite cuts the WAL file back to this size each time it restarts it
        "journal_size_limit": database.wal_size_limit * MB,
    }


def wal_size(db_path: str) -> int:
    """Size of the database's WAL file in bytes, 0 if there is none."""
    try:
        return os.stat(f"{db_path}-wal").st_size
    except OSError:
        return 0


def _checkpoint(
    database: DatabaseConfig, mode: str, timeout: Optional[float] = None
) -> CheckpointRow:
    kwargs = {} if timeout is None else {"timeout": timeout}
    db = SqliteExtDatabase(database.path, pragmas=size_pragmas(database), **kwargs)

    try:
        row = db.execute_sql(f"PRAGMA wal_checkpoint({mode});").fetchone()
    finally:
        db.close()

    return (int(row[0]), int(row[1]), int(row[2]))


def passive_checkpoint(database: DatabaseConfig) -> CheckpointRow:
    """Checkpoint what can be copied without waiting for any connection."""
    return _checkpoint(database, "PASSIVE")


def bounded_truncate_checkpoint(
    database: DatabaseConfig, timeout: float = TRUNCATE_BUSY_TIMEOUT
) -> Optional[CheckpointRow]:
    """TRUNCATE checkpoint that waits at most ``timeout`` for other connections.

    Returns None when the database stayed locked for the whole timeout. A
    returned row with busy=1 also means it gave up.
    """
    try:
        return _checkpoint(database, "TRUNCATE", timeout=timeout)
    except OperationalError as e:
        logger.debug("TRUNCATE checkpoint could not get the database: %s", e)
        return None


@dataclass
class CheckpointResult:
    mode: str
    wal_before: int
    wal_after: int
    passive: Optional[CheckpointRow] = None
    truncate: Optional[CheckpointRow] = None
    truncate_attempted: bool = False

    def describe(self) -> str:
        parts = [f"mode {self.mode}"]

        if self.passive is not None:
            parts.append(
                "passive (busy={}, log={}, checkpointed={})".format(*self.passive)
            )

        if self.truncate_attempted:
            if self.truncate is None:
                parts.append("truncate gave up (locked)")
            else:
                parts.append(
                    "truncate (busy={}, log={}, checkpointed={})".format(*self.truncate)
                )

        return ", ".join(parts)


def maintain_wal(database: DatabaseConfig) -> CheckpointResult:
    """Run the checkpoint that closes a recordings cleanup run."""
    wal_before = wal_size(database.path)
    result = CheckpointResult(
        mode=database.wal_checkpoint.value, wal_before=wal_before, wal_after=wal_before
    )

    if database.wal_checkpoint == WalCheckpointEnum.truncate:
        # upstream: a blocking TRUNCATE whenever the WAL is over 10 MB
        if wal_before / MB > MAX_WAL_SIZE:
            result.truncate_attempted = True

            try:
                result.truncate = _checkpoint(database, "TRUNCATE")
            except OperationalError as e:
                logger.warning("WAL checkpoint (TRUNCATE) failed: %s", e)
    else:
        try:
            result.passive = passive_checkpoint(database)
        except OperationalError as e:
            logger.warning("WAL checkpoint (PASSIVE) failed: %s", e)

        # a long-lived reader can starve PASSIVE; only then is a short,
        # bounded TRUNCATE worth holding writers for
        oversized = TRUNCATE_FALLBACK_FACTOR * database.wal_size_limit * MB

        if wal_size(database.path) > oversized:
            result.truncate_attempted = True
            result.truncate = bounded_truncate_checkpoint(database)

            if result.truncate is None or result.truncate[0] != 0:
                logger.warning(
                    "WAL is %.1f MB after a passive checkpoint and a TRUNCATE "
                    "checkpoint gave up after %ss waiting for other connections",
                    wal_size(database.path) / MB,
                    TRUNCATE_BUSY_TIMEOUT,
                )

    result.wal_after = wal_size(database.path)
    return result


@dataclass
class CleanupRunSummary:
    """Phase timings of one recordings cleanup run, logged as one line."""

    phases: dict[str, float] = field(default_factory=dict)
    checkpoint: Optional[CheckpointResult] = None
    started: float = field(default_factory=time.monotonic)

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        start = time.monotonic()

        try:
            yield
        finally:
            self.phases[name] = self.phases.get(name, 0.0) + (time.monotonic() - start)

    def describe(self) -> str:
        phases = ", ".join(f"{name} {secs:.1f}s" for name, secs in self.phases.items())
        line = (
            f"Recordings cleanup run took {time.monotonic() - self.started:.1f}s "
            f"({phases})"
        )

        if self.checkpoint is not None:
            line += (
                f"; WAL {self.checkpoint.wal_before / MB:.1f} MB before and "
                f"{self.checkpoint.wal_after / MB:.1f} MB after the checkpoint, "
                f"{self.checkpoint.describe()}"
            )

        return line

    def log(self) -> None:
        logger.info(self.describe())
