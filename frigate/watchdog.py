import datetime
import logging
import threading
import time
from multiprocessing.synchronize import Event as MpEvent
from typing import TYPE_CHECKING, Optional

from frigate.object_detection.base import ObjectDetectProcess
from frigate.util.services import restart_frigate

if TYPE_CHECKING:
    from frigate.comms.inter_process import InterProcessCommunicator

logger = logging.getLogger(__name__)

# A single inter-process request may run this long, or twice the database busy
# timeout if that is longer, before Frigate restarts (fork).
STUCK_REQUEST_FLOOR = 600.0


class FrigateWatchdog(threading.Thread):
    def __init__(
        self,
        detectors: dict[str, ObjectDetectProcess],
        stop_event: MpEvent,
        inter_process: Optional["InterProcessCommunicator"] = None,
        busy_timeout: Optional[float] = None,
    ):
        super().__init__(name="frigate_watchdog")
        self.detectors = detectors
        self.stop_event = stop_event
        # fork: the thread that answers every process's requests. Nothing
        # recovers it in place, so a dead or stuck one restarts Frigate.
        self.inter_process = inter_process
        self.stuck_after = max(STUCK_REQUEST_FLOOR, 2 * (busy_timeout or 0))
        self.restarting = False

    def run(self) -> None:
        time.sleep(10)
        while not self.stop_event.wait(10):
            self.check()

        logger.info("Exiting watchdog...")

    def check(self) -> None:
        now = datetime.datetime.now().timestamp()

        # check the detection processes
        for detector in self.detectors.values():
            detection_start = detector.detection_start.value  # type: ignore[attr-defined]
            # issue https://github.com/python/typeshed/issues/8799
            # from mypy 0.981 onwards
            if detection_start > 0.0 and now - detection_start > 10:
                logger.info(
                    "Detection appears to be stuck. Restarting detection process..."
                )
                detector.start_or_restart()
            elif (
                detector.detect_process is not None
                and not detector.detect_process.is_alive()
            ):
                logger.info("Detection appears to have stopped. Exiting Frigate...")
                restart_frigate()

        self.check_request_thread()

    def check_request_thread(self) -> None:
        """Restart Frigate if the inter-process request thread died or is stuck."""
        communicator = self.inter_process

        if (
            communicator is None
            or self.restarting
            or self.stop_event.is_set()
            or communicator.stop_event.is_set()
        ):
            return

        reader = getattr(communicator, "reader_thread", None)

        if reader is None:
            return

        if not reader.is_alive():
            logger.error(
                "The inter-process request thread has stopped, so no other "
                "process's requests are answered. Restarting Frigate..."
            )
            self._restart()
            return

        busy = communicator.busy

        if busy is None:
            return

        topic, started = busy
        running = time.monotonic() - started

        if running > self.stuck_after:
            logger.error(
                "Inter-process request %s has been running for %.0fs (limit "
                "%.0fs), and every other process's requests wait behind it. "
                "Restarting Frigate...",
                topic,
                running,
                self.stuck_after,
            )
            self._restart()

    def _restart(self) -> None:
        self.restarting = True
        restart_frigate()
