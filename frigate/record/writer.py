"""Segment rows written off the mover's path (fork-only).

Upstream ends every mover pass with a REQ/REP request that blocks until the
row insert commits in the main process, so a busy or locked database stops
segments leaving the cache. Here the mover hands its rows to a bounded writer
thread in the recording process and starts its next pass straight away.

- The main-process handler (``insert_recording_rows``) never raises: a
  database error comes back as ``{"ok": false}`` instead of ending the
  inter-process request thread that every Frigate process depends on. Inserts
  ignore rows already stored, so a retried batch is harmless.
- The writer retries failed batches with backoff and never drops a row. When
  its queue is full, the mover waits.
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

from frigate.comms.inter_process import InterProcessRequestor
from frigate.comms.recordings_updater import (
    RecordingsDataPublisher,
    RecordingsDataTypeEnum,
)
from frigate.config import FrigateConfig
from frigate.const import INSERT_MANY_RECORDINGS
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


def insert_recording_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Store a batch of segment rows. Runs on the main inter-process thread.

    Never raises: that thread answers every inter-process request in Frigate,
    so an exception escaping here would leave all of them waiting forever.
    """
    if not rows:
        return {"ok": True, "count": 0}

    try:
        Recordings.insert_many(rows).on_conflict(action="nothing").execute()
    except Exception as e:
        logger.debug("Inserting %d recording rows failed: %s", len(rows), e)
        return {"ok": False, "error": str(e)}

    try:
        note_late_rows(rows)
    except Exception:
        logger.exception("Failed to note late recording rows")

    return {"ok": True, "count": len(rows)}


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
        requestor_factory: Callable[[], Any] = InterProcessRequestor,
        publisher_factory: Callable[[], Any] = RecordingsDataPublisher,
    ) -> None:
        # daemon: a send stuck on a locked database must not keep the
        # process alive past its flush timeout
        super().__init__(name="recordings_writer", daemon=True)
        self.capacity = config.record.pipeline.db_write_queue
        self.requestor_factory = requestor_factory
        self.publisher_factory = publisher_factory

        self.rows: deque[dict[str, Any]] = deque()
        self.saved_candidates: dict[str, Optional[float]] = {}
        self.cond = threading.Condition()
        self.stopping = False
        self.stop_requested = threading.Event()

        self.full_logged = False
        self.failing_since: Optional[float] = None
        self.failing_error_logged = False

    def queue_depth(self) -> int:
        with self.cond:
            return len(self.rows)

    def submit(
        self, rows: list[dict[str, Any]], saved: dict[str, Optional[float]]
    ) -> None:
        """Queue a pass's rows and its saved times. Waits while the queue is full.

        ``saved`` holds, per camera, the start of the oldest segment still in
        the cache (None if recording is off); it is published once every row
        before it is written.
        """
        with self.cond:
            self.saved_candidates.update(saved)

            for row in rows:
                while len(self.rows) >= self.capacity and not self.stopping:
                    if not self.full_logged:
                        logger.error(
                            "The recording row queue is full (%d rows) because the "
                            "database is not taking writes. The mover is waiting for "
                            "room; segments may back up in the cache",
                            len(self.rows),
                        )
                        self.full_logged = True

                    self.cond.wait(timeout=1)

                # while stopping the row is queued anyway, so it is counted
                # and logged with the unsent rows rather than lost
                self.rows.append(row)

            self.cond.notify_all()

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

                if batch and not self._send(requestor, batch):
                    # a stop cuts the backoff short; the drain then retries
                    # every half second until stop() stops waiting for it
                    if self.stop_requested.wait(retry_delay):
                        time.sleep(0.5)

                    retry_delay = min(retry_delay * 2, RETRY_MAX)
                    continue

                retry_delay = RETRY_INITIAL

                with self.cond:
                    for _ in batch:
                        self.rows.popleft()

                    if not self.rows:
                        self.full_logged = False

                    self.cond.notify_all()

                self._publish_saved(publisher)
        finally:
            requestor.stop()
            publisher.stop()

    def _send(self, requestor: Any, batch: list[dict[str, Any]]) -> bool:
        result = requestor.send_data(INSERT_MANY_RECORDINGS, batch)

        if isinstance(result, dict) and result.get("ok"):
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
