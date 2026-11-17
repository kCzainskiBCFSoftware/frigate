import datetime
import logging
import os
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

# While the request thread is dead or stuck, Frigate's stop can wait forever on
# a thread blocked in an untimed request to it (the stats emitter, the tracked
# object processor), and the reader's own join waits on a stuck one. If the
# process is still alive this long after the restart (or after a stopping
# Frigate found the thread dead or stuck), and the thread is still not
# answering, it exits at once, and s6 or the container's restart policy starts
# it again. Half of s6's 120 s timeout-kill for the frigate service.
RESTART_EXIT_DEADLINE = 60.0

# How often a stop that has begun looks at the request thread again.
STOP_CHECK_INTERVAL = 1.0

# Lets the log listener write the exit error before the process ends.
EXIT_LOG_GRACE = 1.0


def _hard_exit() -> None:
    os._exit(1)


class FrigateWatchdog(threading.Thread):
    def __init__(
        self,
        detectors: dict[str, ObjectDetectProcess],
        stop_event: MpEvent,
        inter_process: Optional["InterProcessCommunicator"] = None,
        busy_timeout: Optional[float] = None,
        restart_deadline: float = RESTART_EXIT_DEADLINE,
    ):
        super().__init__(name="frigate_watchdog")
        self.detectors = detectors
        self.stop_event = stop_event
        # fork: the thread that answers every process's requests. Nothing
        # recovers it in place, so a dead or stuck one restarts Frigate.
        self.inter_process = inter_process
        self.stuck_after = max(STUCK_REQUEST_FLOOR, 2 * (busy_timeout or 0))
        # a request that waits on the database gives up after the busy timeout,
        # so one running longer at stop time is not going to answer soon
        self.busy_timeout = busy_timeout or 0.0
        self.restart_deadline = restart_deadline
        self.exit_timer: Optional[threading.Timer] = None
        self.exit_timer_lock = threading.Lock()
        self.restarting = False
        # watches the request thread while Frigate stops (arm_stop_deadline)
        self.stop_monitor: Optional[threading.Thread] = None
        self.stop_monitor_done = threading.Event()

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

        problem = self.request_thread_problem(self.stuck_after)

        if problem is None:
            return

        logger.error(
            "Restarting Frigate: %s, so no other process's requests are "
            "answered (a request may run %.0fs)",
            problem,
            self.stuck_after,
        )
        self._restart()

    def request_thread_problem(self, limit: float) -> Optional[str]:
        """Why the request thread is not answering, or None if it is.

        It has died (unless its communicator was stopped on purpose), or one
        request has been running longer than ``limit`` seconds.
        """
        communicator = self.inter_process

        if communicator is None:
            return None

        reader = getattr(communicator, "reader_thread", None)

        if reader is None:
            return None

        if not reader.is_alive():
            if communicator.stop_event.is_set():
                return None

            return "the inter-process request thread has stopped"

        busy = communicator.busy

        if busy is None:
            return None

        topic, started = busy
        running = time.monotonic() - started

        if running <= limit:
            return None

        return f"inter-process request {topic} has been running for {running:.0f}s"

    def arm_stop_deadline(self) -> None:
        """Bound a stop for as long as the request thread is dead or stuck.

        Called first in FrigateApp.stop(). Every restart (the UI, the API, MQTT,
        a dead detector) and a plain stop end there, and the rest of stop() can
        wait forever on threads blocked behind that thread. The thread is
        checked at once and then every STOP_CHECK_INTERVAL until stop() has
        stopped it: once it is dead, or one request has run longer than the
        busy timeout, the exit deadline is armed. A stop that never sees that
        is left alone, however slow, and the deadline does not end a stop whose
        thread answers again before it runs out.
        """
        if self.inter_process is None:
            return

        self._check_stop()

        if self.stop_monitor is None:
            self.stop_monitor = threading.Thread(
                target=self._watch_stop, name="stop_deadline_monitor", daemon=True
            )
            self.stop_monitor.start()

    def _watch_stop(self) -> None:
        communicator = self.inter_process

        if communicator is None:
            return

        reader = getattr(communicator, "reader_thread", None)

        while not self.stop_monitor_done.wait(STOP_CHECK_INTERVAL):
            if (
                communicator.stop_event.is_set()
                and reader is not None
                and not reader.is_alive()
            ):
                # stop() has stopped the thread: nothing waits on it any more
                return

            self._check_stop()

    def _check_stop(self) -> None:
        problem = self.request_thread_problem(self.busy_timeout)

        if problem is None or self.exit_timer is not None:
            return

        logger.warning(
            "Frigate is stopping while %s (busy timeout %.0fs); it exits if that "
            "still holds in %.0fs",
            problem,
            self.busy_timeout,
            self.restart_deadline,
        )
        self._arm_exit_deadline()

    def _restart(self) -> None:
        self.restarting = True
        self._arm_exit_deadline()
        restart_frigate()

    def _arm_exit_deadline(self) -> None:
        # once: the watchdog's restart and the stop that follows may both arm it
        with self.exit_timer_lock:
            if self.exit_timer is not None:
                return

            # daemon: it never keeps a process that did stop in time alive
            self.exit_timer = threading.Timer(self.restart_deadline, self._exit_now)
            self.exit_timer.name = "restart_exit_deadline"
            self.exit_timer.daemon = True
            self.exit_timer.start()

    def _exit_now(self) -> None:
        problem = self.request_thread_problem(self.busy_timeout)

        if problem is None:
            # it answers again, or stop() has stopped it: nothing is blocked
            # behind it, so the stop goes on, and a new wedge can arm again
            logger.info(
                "The inter-process request thread is answering again; Frigate "
                "keeps stopping normally"
            )
            with self.exit_timer_lock:
                self.exit_timer = None

            return

        logger.error(
            "Frigate has not stopped within %.0fs: %s, and a thread is still "
            "waiting on it. Exiting now",
            self.restart_deadline,
            problem,
        )
        time.sleep(EXIT_LOG_GRACE)
        _hard_exit()
