"""Recovery of segment rows after an unclean shutdown (fork-only).

The writer (``frigate.record.writer``) moves a segment's file into
``recordings/`` before its row reaches the database, so a crash, an OOM kill
or a stop that outlasts the flush timeout can leave files without rows.
Cleanup is driven by rows and ``sync_recordings`` is off by default, so such a
file would never be listed, played or expired.

When the previous run did not leave its clean-shutdown marker, this scans the
per-variant hour directories of the last ``record.pipeline.reconcile_window``
hours, finds files that have no row, probes them one at a time and inserts a
row with unknown (NULL) activity stats. Inserts ignore rows that exist, so
running it twice changes nothing.
"""

import asyncio
import datetime
import logging
import os
import random
import string
from typing import Any, Optional, get_args

import psutil

from frigate.config import FrigateConfig
from frigate.config.camera.ffmpeg import RecordVariant
from frigate.const import MAX_SEGMENT_DURATION, RECORD_DIR
from frigate.models import Recordings
from frigate.record.writer import insert_recording_rows
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

    rand_id = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))

    return {
        Recordings.id.name: f"{start}-{rand_id}",
        Recordings.camera.name: camera,
        Recordings.path.name: path,
        Recordings.start_time.name: start,
        Recordings.end_time.name: start + duration,
        Recordings.duration.name: duration,
        # the frames were never matched to this segment: unknown, not zero
        Recordings.motion.name: None,
        Recordings.objects.name: None,
        Recordings.regions.name: None,
        Recordings.dBFS.name: None,
        Recordings.segment_size.name: round(float(size_bytes) / pow(2, 20), 2),
        Recordings.variant.name: variant,
        Recordings.codec_name.name: media_info.get("fourcc"),
        Recordings.width.name: media_info.get("width") or None,
        Recordings.height.name: media_info.get("height") or None,
        Recordings.bitrate.name: int(size_bytes * 8 / duration) if size_bytes else None,
        Recordings.transcoded_from_main.name: False,
    }


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
    recovered = 0

    for (camera, variant), segments in sorted(files.items()):
        known = {
            path
            for (path,) in Recordings.select(Recordings.path)
            .where(
                Recordings.camera == camera,
                Recordings.variant == variant,
                Recordings.start_time >= since - MAX_SEGMENT_DURATION,
                Recordings.start_time <= now,
            )
            .tuples()
        }

        for path, start in sorted(segments.items(), key=lambda s: s[1]):
            if path in known:
                continue

            try:
                if os.path.getmtime(path) >= written_before:
                    continue
            except OSError:
                continue

            media_info = asyncio.run(
                get_video_properties(config.ffmpeg, path, get_duration=True)
            )
            duration = float(media_info.get("duration", -1))

            if (
                not media_info.get("has_valid_video", False)
                or not 0 < duration < MAX_SEGMENT_DURATION
            ):
                logger.warning(
                    f"Recovery could not probe {path}; leaving it in place without a row"
                )
                continue

            result = insert_recording_rows(
                [_row(camera, variant, path, start, media_info)]
            )

            if not result["ok"]:
                logger.error(f"Recovery could not add a row for {path}: {result}")
                continue

            recovered += 1

    if recovered:
        logger.warning(
            f"Recovered {recovered} recording segments that had no database row "
            "after an unclean shutdown"
        )
    else:
        logger.info("Recovery found no recording segments without a database row")

    return recovered
