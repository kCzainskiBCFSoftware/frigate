"""Per-request performance logging for media playback endpoints.

Writes one JSON line per request to a rotating file in a `perf-logs/`
directory next to the SQLite database (so on setups where the DB lives on
fast storage, these logs land there too). The logger does not propagate, so
these lines stay out of the normal container log stream.
"""

import json
import logging
import os
from datetime import datetime
from logging.handlers import RotatingFileHandler

PERF_LOG_DIR_NAME = "perf-logs"
PERF_LOG_FILE_NAME = "api_perf.log"
PERF_LOG_MAX_BYTES = 10 * 1024 * 1024
PERF_LOG_BACKUP_COUNT = 3

# dedicated logger; propagate=False keeps it out of the docker/stdout logs
_perf_logger = logging.getLogger("frigate.api_perf")
_perf_logger.propagate = False

logger = logging.getLogger(__name__)


def setup_api_perf_logging(database_path: str) -> None:
    """Attach the rotating file handler once; safe to call repeatedly.

    On failure (e.g. read-only config dir) perf logging degrades to a no-op
    instead of breaking media serving.
    """
    if _perf_logger.handlers:
        return

    log_dir = os.path.join(os.path.dirname(database_path), PERF_LOG_DIR_NAME)

    try:
        os.makedirs(log_dir, exist_ok=True)
        handler = RotatingFileHandler(
            os.path.join(log_dir, PERF_LOG_FILE_NAME),
            maxBytes=PERF_LOG_MAX_BYTES,
            backupCount=PERF_LOG_BACKUP_COUNT,
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        _perf_logger.addHandler(handler)
        _perf_logger.setLevel(logging.INFO)
        logger.info(f"API perf logging enabled at {log_dir}/{PERF_LOG_FILE_NAME}")
    except OSError as e:
        logger.warning(f"Unable to set up API perf logging at {log_dir}: {e}")
        _perf_logger.addHandler(logging.NullHandler())


def log_api_perf(payload: dict) -> None:
    """Write one JSON line; ms fields are rounded for readability."""
    if not _perf_logger.handlers or isinstance(
        _perf_logger.handlers[0], logging.NullHandler
    ):
        return

    record: dict = {"ts": datetime.now().isoformat(timespec="milliseconds")}
    for key, value in payload.items():
        if isinstance(value, float) and key.endswith("_ms"):
            record[key] = round(value, 1)
        else:
            record[key] = value

    try:
        _perf_logger.info(json.dumps(record, default=str))
    except Exception:
        # metrics must never break request serving
        pass
