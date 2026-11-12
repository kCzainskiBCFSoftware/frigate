"""Facilitates communication between processes."""

import logging
import multiprocessing as mp
import threading
import time
from multiprocessing.synchronize import Event as MpEvent
from typing import Any, Callable, Optional

import zmq

from frigate.comms.base_communicator import Communicator

logger = logging.getLogger(__name__)

SOCKET_REP_REQ = "ipc:///tmp/cache/comms"

# A topic whose requests keep failing logs one traceback per this many seconds.
ERROR_LOG_INTERVAL = 300.0

# The topic logged for a request that could not be decoded.
UNREADABLE_TOPIC = "<unreadable request>"


class InterProcessCommunicator(Communicator):
    """Answers every other process's requests, on one thread for all of Frigate.

    Fork: a handler that raises, or a reply that cannot be encoded, is answered
    with ``[]`` and logged, instead of ending the thread and leaving every later
    request waiting forever. ``busy`` lets the watchdog see a request that
    never finishes.
    """

    def __init__(self) -> None:
        self.context = zmq.Context()
        self.socket = self.context.socket(zmq.REP)
        self.socket.bind(SOCKET_REP_REQ)
        self.stop_event: MpEvent = mp.Event()
        # (topic, time.monotonic() at its start) while a request is handled,
        # None between requests. Always assigned as a whole tuple, so another
        # thread reads a consistent pair without a lock.
        self.busy: Optional[tuple[str, float]] = None
        # topic -> (monotonic time of its last logged traceback, failures not
        # logged since). Used only by the reader thread.
        self._failures: dict[str, tuple[float, int]] = {}

    def publish(self, topic: str, payload: Any, retain: bool = False) -> None:
        """There is no communication back to the processes."""
        pass

    def subscribe(self, receiver: Callable) -> None:
        self._dispatcher = receiver
        self.reader_thread = threading.Thread(
            target=self.read, name="inter_process_requests"
        )
        self.reader_thread.start()

    def read(self) -> None:
        while not self.stop_event.is_set():
            try:
                while True:  # load all messages that are queued
                    has_message, _, _ = zmq.select([self.socket], [], [], 1)

                    if not has_message:
                        break

                    if not self._serve_one():
                        break
            except Exception:
                # never fatal: this thread answers every process in Frigate
                self._log_failure("<inter-process transport>", "failed")
                self.stop_event.wait(0.1)

    def _serve_one(self) -> bool:
        """Receive one request and answer it. False on a ZMQ error."""
        try:
            raw = self.socket.recv_json(flags=zmq.NOBLOCK)
        except zmq.ZMQError:
            return False
        except Exception:
            # received but not decodable: the socket still owes it a reply
            raw = None
            self._log_failure(UNREADABLE_TOPIC, "could not be decoded")

        topic = (
            raw[0]
            if isinstance(raw, list) and raw and isinstance(raw[0], str)
            else UNREADABLE_TOPIC
        )
        self.busy = (topic, time.monotonic())

        try:
            response = self._dispatch(topic, raw)
            return self._reply(topic, response)
        finally:
            self.busy = None

    def _dispatch(self, topic: str, raw: Any) -> Any:
        if raw is None:
            return None

        if not isinstance(raw, list):
            logger.warning(
                f"Received unexpected data type in ZMQ recv_json: {type(raw)}"
            )
            return None

        try:
            (topic, value) = raw
            return self._dispatcher(topic, value)
        except Exception:
            self._log_failure(topic, "raised")
            return None

    def _reply(self, topic: str, response: Any) -> bool:
        """Send exactly one reply, ``[]`` if the response cannot be sent."""
        try:
            self.socket.send_json(response if response is not None else [])
            return True
        except zmq.ZMQError:
            return False
        except Exception:
            # e.g. not JSON serializable; nothing was sent yet
            self._log_failure(topic, "returned a result that could not be sent")

        try:
            self.socket.send_json([])
        except zmq.ZMQError:
            return False

        return True

    def _log_failure(self, topic: str, what: str) -> None:
        """Log a failed request with its traceback, at most every 5 min per topic."""
        now = time.monotonic()
        last, suppressed = self._failures.get(topic, (None, 0))

        if last is not None and now - last < ERROR_LOG_INTERVAL:
            self._failures[topic] = (last, suppressed + 1)
            return

        self._failures[topic] = (now, 0)
        logger.error(
            "Inter-process request %s %s; answered with an empty result%s",
            topic,
            what,
            (
                f" ({suppressed} more failures of this topic were not logged)"
                if suppressed
                else ""
            ),
            exc_info=True,
        )

    def stop(self) -> None:
        self.stop_event.set()
        self.reader_thread.join()
        self.socket.close()
        self.context.destroy()


class InterProcessRequestor:
    """Simplifies sending data to InterProcessCommunicator and getting a reply.

    With ``timeout`` (seconds, fork), a request that gets no reply in time
    returns ``""`` and the requestor reconnects, so its next request is sent
    and answered normally. Without one, a request waits for its reply as
    upstream does.
    """

    def __init__(self, timeout: Optional[float] = None) -> None:
        self.timeout = timeout
        self.context = zmq.Context()
        self.socket = self._connect()

    def _connect(self) -> zmq.Socket:
        socket = self.context.socket(zmq.REQ)

        if self.timeout is not None:
            timeout_ms = max(1, int(self.timeout * 1000))
            socket.setsockopt(zmq.RCVTIMEO, timeout_ms)
            socket.setsockopt(zmq.SNDTIMEO, timeout_ms)

        socket.connect(SOCKET_REP_REQ)
        return socket

    def send_data(self, topic: str, data: Any) -> Any:
        """Sends data and then waits for reply."""
        try:
            self.socket.send_json((topic, data))
            return self.socket.recv_json()
        except zmq.Again:
            if self.timeout is not None:
                logger.warning(
                    "No reply to inter-process request %s within %ss; reconnecting",
                    topic,
                    self.timeout,
                )
                self._reconnect()

            return ""
        except zmq.ZMQError:
            return ""

    def _reconnect(self) -> None:
        """A new REQ socket: one that missed its reply refuses every later send."""
        try:
            self.socket.close(linger=0)
        except zmq.ZMQError:
            pass

        try:
            self.socket = self._connect()
        except zmq.ZMQError:
            # the closed socket fails each send with "", so callers that
            # replace their requestor on "no reply" still recover
            logger.exception("Could not reconnect an inter-process requestor")

    def stop(self) -> None:
        if self.timeout is not None:
            # a request that timed out must not hold up the context's teardown
            self.socket.close(linger=0)
        else:
            self.socket.close()

        self.context.destroy()
