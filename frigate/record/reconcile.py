"""Recovery of segment rows after an unclean shutdown (fork-only).

The writer (``frigate.record.writer``) moves a segment's file into
``recordings/`` before its row reaches the database, so a crash, an OOM kill
or a stop that outlasts the flush timeout can leave files without rows.
Cleanup is driven by rows and ``sync_recordings`` is off by default, so such a
file would never be listed, played or expired.

When the previous run did not leave its clean-shutdown marker, this scans the
per-variant hour directories of the last ``record.pipeline.reconcile_window``
hours, finds files that have no row, probes them one at a time and inserts
rows with unknown (NULL) activity stats, in batches. Inserts ignore rows that
exist, so running it twice changes nothing.
"""

import asyncio
import datetime
import logging
import os
from typing import Any, Optional, get_args

import psutil

from frigate.config import FrigateConfig
from frigate.config.camera.ffmpeg import RecordVariant
from frigate.const import MAX_SEGMENT_DURATION, RECORD_DIR
from frigate.models import Recordings
from frigate.record.util import recording_row
from frigate.record.variants import apply_variant_filter
from frigate.record.writer import BATCH_ROWS, insert_recording_rows
from frigate.util.services import get_video_properties

logger = logging.getLogger(__name__)

VARIANTS = set(get_args(RecordVariant))


def _segment_start(date: str, hour: str, file_name: str) -> Optional[float]:
    """UTC start time from recordings/{date}/{hour}/{camera}/{variant}/MM.SS.mp4."""
    try:
        return (
            datetime.datetime.strptime(
                f"{date} {hour} {file_name}", "%Y-%m-%d %H %M.%S.mp4"
            )
            .replace(tzinfo=datetime.timezone.utc)
            .timestamp()
        )
    except ValueError:
        return None


def segment_files(
    since: float, until: float
) -> dict[tuple[str, str], dict[str, float]]:
    """{(camera, variant): {path: start_time}} for segments started in the window."""
    found: dict[tuple[str, str], dict[str, float]] = {}
    hour = datetime.datetime.fromtimestamp(since, datetime.timezone.utc).replace(
        minute=0, second=0, microsecond=0
    )
    last = datetime.datetime.fromtimestamp(until, datetime.timezone.utc)

    while hour <= last:
        date_part, hour_part = hour.strftime("%Y-%m-%d"), hour.strftime("%H")
        hour_dir = os.path.join(RECORD_DIR, date_part, hour_part)
        hour += datetime.timedelta(hours=1)

        try:
            cameras = os.listdir(hour_dir)
        except OSError:
            continue

        for camera in cameras:
            for variant in VARIANTS:
                variant_dir = os.path.join(hour_dir, camera, variant)

                try:
                    names = os.listdir(variant_dir)
                except OSError:
                    continue

                for name in names:
                    start = _segment_start(date_part, hour_part, name)

                    if start is None or not since <= start <= until:
                        continue

                    found.setdefault((camera, variant), {})[
                        os.path.join(variant_dir, name)
                    ] = start

    return found


def _row(
    camera: str, variant: str, path: str, start: float, media_info: dict[str, Any]
) -> dict[str, Any]:
    duration = float(media_info["duration"])

    try:
        size_bytes = os.path.getsize(path)
    except OSError:
        size_bytes = 0

    # the frames were never matched to this segment: stats unknown, not zero
    return recording_row(
        camera,
        variant,
        path,
        start,
        start + duration,
        duration,
        size_bytes,
        media_info,
    )


def _insert(rows: list[dict[str, Any]]) -> int:
    """Insert one batch of recovered rows. Returns how many were stored."""
    result = insert_recording_rows(rows)

    if not result["ok"]:
        logger.error(
            f"Recovery could not add rows for {len(rows)} segments "
            f"({rows[0][Recordings.path.name]} to {rows[-1][Recordings.path.name]}): "
            f"{result.get('error')}"
        )
        return 0

    return len(rows)


async def _recover(
    config: FrigateConfig, missing: list[tuple[str, str, str, float]]
) -> int:
    """Probe each file in turn and insert the rows in batches."""
    recovered = 0
    rows: list[dict[str, Any]] = []

    for camera, variant, path, start in missing:
        # one at a time: recovery runs while 32 cameras record
        media_info = await get_video_properties(config.ffmpeg, path, get_duration=True)
        duration = float(media_info.get("duration", -1))

        if (
            not media_info.get("has_valid_video", False)
            or not 0 < duration < MAX_SEGMENT_DURATION
        ):
            logger.warning(
                f"Recovery could not probe {path}; leaving it in place without a row"
            )
            continue

        rows.append(_row(camera, variant, path, start, media_info))

        if len(rows) >= BATCH_ROWS:
            recovered += _insert(rows)
            rows = []

    if rows:
        recovered += _insert(rows)

    return recovered


def reconcile_recordings(
    config: FrigateConfig,
    now: Optional[float] = None,
    written_before: Optional[float] = None,
) -> int:
    """Insert rows for recent segment files that have none. Returns how many.

    Only files written before ``written_before`` (default: when this Frigate
    process started) are considered, so a segment the new run has just moved
    and queued is left to the writer, which stores its real stats.
    """
    now = datetime.datetime.now().timestamp() if now is None else now
    written_before = (
        psutil.Process().create_time() if written_before is None else written_before
    )
    since = now - config.record.pipeline.reconcile_window * 3600
    files = segment_files(since, now)
    missing: list[tuple[str, str, str, float]] = []

    for (camera, variant), segments in sorted(files.items()):
        known = {
            path
            for (path,) in apply_variant_filter(
                Recordings.select(Recordings.path).where(
                    Recordings.camera == camera,
                    Recordings.start_time >= since - MAX_SEGMENT_DURATION,
                    Recordings.start_time <= now,
                ),
                variant,
            ).tuples()
        }

        for path, start in sorted(segments.items(), key=lambda s: s[1]):
            if path in known:
                continue

            try:
                if os.path.getmtime(path) >= written_before:
                    continue
            except OSError:
                continue

            missing.append((camera, variant, path, start))

    if missing:
        logger.info(
            f"Recovery is probing {len(missing)} recording segments that have no "
            "database row"
        )

    recovered = asyncio.run(_recover(config, missing)) if missing else 0

    if recovered:
        logger.warning(
            f"Recovered {recovered} recording segments that had no database row "
            "after an unclean shutdown"
        )
    else:
        logger.info("Recovery found no recording segments without a database row")

    return recovered
