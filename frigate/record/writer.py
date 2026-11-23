"""Segment rows written off the mover's path (fork-only).

Upstream ends every mover pass with a REQ/REP request that blocks until the
row insert commits in the main process, so a busy or locked database stops
segments leaving the cache. Here the mover hands its rows to a bounded writer
thread in the recording process and starts its next pass straight away.

- The main-process handler (``insert_recording_rows``) never raises: a
  database error comes back as ``{"ok": false}`` instead of ending the
  inter-process request thread that every Frigate process depends on. Inserts
  ignore rows already stored, so a retried batch is harmless.
- The writer retries failed batches with backoff. When its queue is full, the
  mover holds segments in the cache (``has_room``) instead of moving more; it
  never blocks on the writer, so it keeps applying the cache limits and stops
  promptly.
- A row whose values the database rejects (a constraint, a trigger, a
  datatype) would fail its batch the same way forever and stop every camera.
  The handler then inserts that batch row by row in the same request, stores
  the rest, and reports the rejected rows; they are logged and dropped, and
  their files stay in recordings. Every other failure is retried whole.
- Its requests time out after the main process's busy timeout plus a margin;
  the requestor then reconnects, so a main process that stops answering does
  not hang the writer.
- ``recordings/saved`` is published from here, after the rows are written, so
  it never advertises footage that has no row yet.
- On a clean stop the writer drains its queue, and the recording process then
  leaves a clean-shutdown marker. Without the marker, the next start looks for
  segment files that never got a row (``frigate.record.reconcile``).
"""

import itertools
import logging
import os
import threading
import time
from collections import deque
from typing import Any, Callable, Optional

from peewee import DataError, IntegrityError

from frigate.comms.inter_process import InterProcessRequestor
from frigate.comms.recordings_updater import (
    RecordingsDataPublisher,
    RecordingsDataTypeEnum,
)
from frigate.config import FrigateConfig
from frigate.const import INSERT_MANY_RECORDINGS
from frigate.db.maintenance import busy_timeout
from frigate.models import Recordings
from frigate.record.ranges import note_late_rows

logger = logging.getLogger(__name__)

# Rows per insert request; 17 columns each stays far below SQLite's
# bound-variable limit.
BATCH_ROWS = 500

RETRY_INITIAL = 1.0
RETRY_MAX = 30.0

# How long writes may fail before the warning becomes an error.
FAILING_ERROR_AFTER = 60.0

CLEAN_SHUTDOWN_MARKER = ".recordings_clean_shutdown"

# A segment-row request waits this long past the main process's database busy
# timeout for its reply, then reconnects; the insert itself may legitimately
# wait the whole busy timeout.
REPLY_MARGIN = 30.0

# Rejected rows log one error per this many seconds, with the count since.
REJECTED_LOG_INTERVAL = 300.0


def row_request_timeout(config: FrigateConfig) -> float:
    """Seconds a segment-row request waits for the main process's reply."""
    return busy_timeout(config) + REPLY_MARGIN


def timed_requestor_factory(config: FrigateConfig) -> Callable[[], Any]:
    timeout = row_request_timeout(config)
    return lambda: InterProcessRequestor(timeout=timeout)


def insert_recording_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Store a batch of segment rows. Runs on the main inter-process thread.

    Never raises: that thread answers every inter-process request in Frigate,
    so an exception escaping here would leave all of them waiting forever.

    ``{"ok": true}`` when every row is stored or left out as rejected; the
    rejected ones, if any, are listed under ``rejected``. ``{"ok": false}``
    when the database failed for a reason that is not about the rows' values
    (locked, busy, I/O, corruption): the caller retries the whole batch.
    """
    if not rows:
        return {"ok": True, "count": 0}

    rejected: list[dict[str, Any]] = []

    try:
        Recordings.insert_many(rows).on_conflict(action="nothing").execute()
    except (IntegrityError, DataError) as e:
        # the statement is aborted whole for one row's values, and would fail
        # the same way however often it is retried: find those rows here, in
        # this request, instead of failing the batch forever
        logger.debug("Inserting %d recording rows rejected: %s", len(rows), e)
        rows, rejected, error = _insert_each(rows)

        if error is not None:
            return {"ok": False, "error": error}
    except Exception as e:
        logger.debug("Inserting %d recording rows failed: %s", len(rows), e)
        return {"ok": False, "error": str(e)}

    try:
        note_late_rows(rows)
    except Exception:
        logger.exception("Failed to note late recording rows")

    result: dict[str, Any] = {"ok": True, "count": len(rows)}

    if rejected:
        result["rejected"] = rejected

    return result


def _insert_each(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], Optional[str]]:
    """Insert rows one at a time: (stored, rejected, error).

    ``error`` is set, and the rest left unsent, on a failure that is not about
    a row's values; rows already stored are ignored when the batch is resent.
    """
    stored: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    for row in rows:
        try:
            Recordings.insert(row).on_conflict(action="nothing").execute()
        except (IntegrityError, DataError) as e:
            rejected.append(
                {
                    "path": row.get(Recordings.path.name),
                    "camera": row.get(Recordings.camera.name),
                    "variant": row.get(Recordings.variant.name),
                    "error": str(e),
                }
            )
        except Exception as e:
            logger.debug("Inserting a recording row failed: %s", e)
            return stored, rejected, str(e)
        else:
            stored.append(row)

    return stored, rejected, None


class RejectedRowsLog:
    """Logs the rows the database rejected, at most once per interval.

    Each error names the first rejected row and how many more were rejected
    since the previous one, so a constraint that refuses every row does not
    log a line per segment.
    """

    def __init__(self) -> None:
        self.logged_at: Optional[float] = None
        self.unlogged = 0

    def report(self, rejected: Optional[list[dict[str, Any]]]) -> None:
        if not rejected:
            return

        for row in rejected:
            logger.debug("Rejected recording row %s: %s", row.get("path"), row)

        now = time.monotonic()

        if self.logged_at is not None and now - self.logged_at < REJECTED_LOG_INTERVAL:
            self.unlogged += len(rejected)
            return

        first = rejected[0]
        others = []

        if len(rejected) > 1:
            others.append(f"{len(rejected) - 1} more rows in the same batch")

        if self.unlogged:
            others.append(f"{self.unlogged} more since the last report")

        logger.error(
            "The database rejected the recording row for %s (camera %s, variant "
            "%s): %s. Rejected rows are dropped; their segment files stay in "
            "recordings%s",
            first.get("path"),
            first.get("camera"),
            first.get("variant"),
            first.get("error"),
            f" ({' and '.join(others)} were rejected too)" if others else "",
        )
        self.logged_at = now
        self.unlogged = 0


def clean_shutdown_marker(database_path: str) -> str:
    return os.path.join(os.path.dirname(database_path), CLEAN_SHUTDOWN_MARKER)


def write_clean_shutdown_marker(database_path: str) -> None:
    try:
        with open(clean_shutdown_marker(database_path), "w") as f:
            f.write(str(time.time()))
    except OSError as e:
        logger.error("Unable to write the clean-shutdown marker: %s", e)


def take_clean_shutdown_marker(database_path: str) -> bool:
    """Whether the last run shut down cleanly. Removes the marker."""
    try:
        os.remove(clean_shutdown_marker(database_path))
    except FileNotFoundError:
        return False
    except OSError as e:
        logger.error("Unable to remove the clean-shutdown marker: %s", e)

    return True


class RecordingsWriter(threading.Thread):
    """Writes the mover's segment rows to the database, off the mover's path."""

    def __init__(
        self,
        config: FrigateConfig,
        requestor_factory: Optional[Callable[[], Any]] = None,
        publisher_factory: Callable[[], Any] = RecordingsDataPublisher,
    ) -> None:
        # daemon: a send stuck on a locked database must not keep the
        # process alive past its flush timeout
        super().__init__(name="recordings_writer", daemon=True)
        self.capacity = config.record.pipeline.db_write_queue
        # timed by default: a main process that stops answering must not hang
        # the writer; the requestor reconnects, so the retry goes over a new
        # connection
        self.requestor_factory = requestor_factory or timed_requestor_factory(config)
        self.publisher_factory = publisher_factory

        self.rows: deque[dict[str, Any]] = deque()
        self.saved_candidates: dict[str, Optional[float]] = {}
        self.cond = threading.Condition()
        self.stopping = False
        self.stop_requested = threading.Event()

        self.full_logged = False
        self.failing_since: Optional[float] = None
        self.failing_error_logged = False
        self.rejected_rows = RejectedRowsLog()

    def queue_depth(self) -> int:
        with self.cond:
            return len(self.rows)

    def has_room(self) -> bool:
        """Whether the mover may move more segments into the queue.

        False while the queue holds ``db_write_queue`` rows or more; the first
        time in an outage it logs an error. The mover then holds segments in
        the cache rather than blocking on the writer, so it keeps applying the
        cache limits and still sees a stop.
        """
        with self.cond:
            if len(self.rows) < self.capacity:
                return True

            if not self.full_logged:
                logger.error(
                    "The recording row queue is full (%d rows) because the "
                    "database is not taking writes. The mover holds segments in "
                    "the cache until there is room; past the cache limits the "
                    "oldest are discarded",
                    len(self.rows),
                )
                self.full_logged = True

            return False

    def submit(
        self, rows: list[dict[str, Any]], saved: dict[str, Optional[float]]
    ) -> None:
        """Queue a pass's rows and its saved times. Never waits.

        The capacity is enforced by the mover through ``has_room`` before a
        pass, so one pass's rows may take the queue past it.

        ``saved`` holds, per camera, the start of the oldest segment still in
        the cache (None if recording is off); it is published once every row
        before it is written.
        """
        with self.cond:
            # rows first: saved must never be published without them queued
            self.rows.extend(rows)
            self.saved_candidates.update(saved)
            self.cond.notify_all()

    def take_rows(self) -> list[dict[str, Any]]:
        """Remove and return every queued row, for a writer that has stopped."""
        with self.cond:
            rows = list(self.rows)
            self.rows.clear()
            self.cond.notify_all()

        return rows

    def stop(self, timeout: float) -> bool:
        """Drain for up to ``timeout`` seconds. True if every row was written."""
        with self.cond:
            self.stopping = True
            self.cond.notify_all()

        self.stop_requested.set()
        self.join(timeout)

        with self.cond:
            unsent = list(self.rows)

        if unsent:
            logger.error(
                "%d recording rows were not written before shutdown (oldest %s, "
                "newest %s). The next start recovers them when "
                "record.pipeline.reconcile_on_unclean_shutdown is on",
                len(unsent),
                unsent[0].get(Recordings.path.name),
                unsent[-1].get(Recordings.path.name),
            )
            return False

        return not self.is_alive()

    def run(self) -> None:
        requestor = self.requestor_factory()
        publisher = self.publisher_factory()
        retry_delay = RETRY_INITIAL

        try:
            while True:
                with self.cond:
                    while (
                        not self.rows
                        and not self.saved_candidates
                        and not self.stopping
                    ):
                        self.cond.wait(timeout=1)

                    if self.stopping and not self.rows:
                        break

                    batch = list(itertools.islice(self.rows, BATCH_ROWS))

                # a send with no reply needs no new connection here: the timed
                # requestor reconnects itself
                if batch and not self._send(requestor, batch):
                    # a stop cuts the backoff short; the drain then retries
                    # every half second until stop() stops waiting for it
                    if self.stop_requested.wait(retry_delay):
                        time.sleep(0.5)

                    retry_delay = min(retry_delay * 2, RETRY_MAX)
                    continue

                retry_delay = RETRY_INITIAL

                if batch:
                    self._pop(len(batch))

                try:
                    self._publish_saved(publisher)
                except Exception:
                    # the rows are written; only this saved update is lost
                    logger.exception("Failed to publish recordings saved times")
        finally:
            requestor.stop()
            publisher.stop()

    def _pop(self, count: int) -> None:
        with self.cond:
            for _ in range(count):
                self.rows.popleft()

            if not self.rows:
                self.full_logged = False

            self.cond.notify_all()

    def _send(self, requestor: Any, batch: list[dict[str, Any]]) -> bool:
        """Whether the batch is written; rows the database rejected count."""
        try:
            result = requestor.send_data(INSERT_MANY_RECORDINGS, batch)
        except Exception as e:
            # retried like a refused write: if it ended this thread, rows would
            # queue unwritten until the full queue stopped the mover
            if self.failing_since is None:
                logger.exception("Sending recording rows raised")

            result = {"ok": False, "error": f"{type(e).__name__}: {e}"}

        if isinstance(result, dict) and result.get("ok"):
            # the main process left these out; the rest of the batch is stored
            self.rejected_rows.report(result.get("rejected"))

            if self.failing_since is not None:
                logger.info(
                    "Recording rows are being written again after %.0fs of "
                    "failures, %d rows queued",
                    time.monotonic() - self.failing_since,
                    self.queue_depth(),
                )
                self.failing_since = None
                self.failing_error_logged = False

            return True

        # not a dict: the requestor hit a ZMQ error or a timeout and returned ""
        error = result.get("error") if isinstance(result, dict) else "no reply"

        if self.failing_since is None:
            self.failing_since = time.monotonic()
            logger.warning(
                "Writing recording rows failed (%s), retrying; %d rows queued",
                error,
                self.queue_depth(),
            )
        elif (
            not self.failing_error_logged
            and time.monotonic() - self.failing_since >= FAILING_ERROR_AFTER
        ):
            self.failing_error_logged = True
            logger.error(
                "Recording rows have not been written for %.0fs (%s); %d rows "
                "queued. Footage keeps moving to disk and is listed once the "
                "database takes writes again",
                time.monotonic() - self.failing_since,
                error,
                self.queue_depth(),
            )

        return False

    def _publish_saved(self, publisher: Any) -> None:
        with self.cond:
            candidates, self.saved_candidates = self.saved_candidates, {}
            oldest_queued: dict[str, float] = {}

            for row in self.rows:
                camera = row[Recordings.camera.name]
                start = row[Recordings.start_time.name]

                if camera not in oldest_queued or start < oldest_queued[camera]:
                    oldest_queued[camera] = start

        for camera, saved_time in candidates.items():
            if saved_time is not None and camera in oldest_queued:
                saved_time = min(saved_time, oldest_queued[camera])

            publisher.publish(
                (camera, saved_time, None), RecordingsDataTypeEnum.saved.value
            )
