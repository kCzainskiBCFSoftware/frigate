"""Recordings Utilities."""

import datetime
import logging
import os
import random
import string
from typing import Any, Iterable, Optional

from peewee import DatabaseError, chunked

from frigate.const import RECORD_DIR
from frigate.models import Recordings, RecordingsToDelete

logger = logging.getLogger(__name__)

# Recordings sync leaves a file without a row alone until it was moved this long
# ago. A segment's file is moved before its row is written, and the row can wait
# in the writer's queue through a database stall. At most 12 h, so the daily
# limited sync (files of the last 36 h) still sees every file once.
SYNC_FILE_GRACE = 2 * 3600


def recording_row(
    camera: str,
    variant: str,
    path: str,
    start_time: float,
    end_time: float,
    duration: float,
    size_bytes: int,
    media_info: Optional[dict[str, Any]],
    motion: Optional[int] = None,
    objects: Optional[int] = None,
    regions: Optional[int] = None,
    dBFS: Optional[int] = None,
) -> dict[str, Any]:
    """The Recordings row for a segment file in recordings/.

    Shared by the mover and unclean-shutdown recovery. Activity stats left out
    are stored as unknown (NULL), never as 0.
    """
    media_info = media_info or {}
    rand_id = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))

    return {
        Recordings.id.name: f"{start_time}-{rand_id}",
        Recordings.camera.name: camera,
        Recordings.path.name: path,
        Recordings.start_time.name: start_time,
        Recordings.end_time.name: end_time,
        Recordings.duration.name: duration,
        Recordings.motion.name: motion,
        # TODO: update this to store list of active objects at some point
        Recordings.objects.name: objects,
        Recordings.regions.name: regions,
        Recordings.dBFS.name: dBFS,
        # stored as MB
        Recordings.segment_size.name: round(float(size_bytes) / pow(2, 20), 2),
        Recordings.variant.name: variant,
        Recordings.codec_name.name: media_info.get("fourcc"),
        Recordings.width.name: media_info.get("width") or None,
        Recordings.height.name: media_info.get("height") or None,
        Recordings.bitrate.name: int(size_bytes * 8 / duration)
        if duration > 0 and size_bytes > 0
        else None,
        Recordings.transcoded_from_main.name: False,
    }


def remove_empty_directories(directory: str) -> None:
    # list all directories recursively and sort them by path,
    # longest first
    paths = sorted(
        [x[0] for x in os.walk(directory)],
        key=lambda p: len(str(p)),
        reverse=True,
    )
    for path in paths:
        # don't delete the parent
        if path == directory:
            continue
        if len(os.listdir(path)) == 0:
            os.rmdir(path)


def remove_empty_parent_directories(directories: Iterable[str], root: str) -> None:
    """Remove each directory and then its parents up to ``root``, while empty.

    Only these branches are touched: nothing else under ``root`` is listed,
    and ``root`` itself is never removed.
    """
    root = os.path.normpath(root)

    # deepest first, so a shared parent is tried after all of its children
    for directory in sorted(
        {os.path.normpath(d) for d in directories},
        key=lambda d: d.count(os.sep),
        reverse=True,
    ):
        path = directory

        while path != root and path.startswith(root + os.sep):
            try:
                os.rmdir(path)
            except FileNotFoundError:
                # removed already through another branch; its parent may not be
                pass
            except OSError:
                # not empty
                break

            path = os.path.dirname(path)


def sync_recordings(limited: bool) -> None:
    """Check the db for stale recordings entries that don't exist in the filesystem."""

    def delete_db_entries_without_file(check_timestamp: float) -> bool:
        """Delete db entries where file was deleted outside of frigate."""

        if limited:
            recordings = Recordings.select(Recordings.id, Recordings.path).where(
                Recordings.start_time >= check_timestamp
            )
        else:
            # get all recordings in the db
            recordings = Recordings.select(Recordings.id, Recordings.path)

        # Use pagination to process records in chunks
        page_size = 1000
        num_pages = (recordings.count() + page_size - 1) // page_size
        recordings_to_delete = set()

        for page in range(num_pages):
            for recording in recordings.paginate(page, page_size):
                if not os.path.exists(recording.path):
                    recordings_to_delete.add(recording.id)

        if len(recordings_to_delete) == 0:
            return True

        logger.info(
            f"Deleting {len(recordings_to_delete)} recording DB entries with missing files"
        )

        # convert back to list of dictionaries for insertion
        recordings_to_delete = [
            {"id": recording_id} for recording_id in recordings_to_delete
        ]

        if float(len(recordings_to_delete)) / max(1, recordings.count()) > 0.5:
            logger.warning(
                f"Deleting {(len(recordings_to_delete) / max(1, recordings.count()) * 100):.2f}% of recordings DB entries, could be due to configuration error. Aborting..."
            )
            return False

        # create a temporary table for deletion
        RecordingsToDelete.create_table(temporary=True)

        # insert ids to the temporary table
        max_inserts = 1000
        for batch in chunked(recordings_to_delete, max_inserts):
            RecordingsToDelete.insert_many(batch).execute()

        try:
            # delete records in the main table that exist in the temporary table
            query = Recordings.delete().where(
                Recordings.id.in_(RecordingsToDelete.select(RecordingsToDelete.id))
            )
            query.execute()
        except DatabaseError as e:
            logger.error(f"Database error during recordings db cleanup: {e}")

        return True

    def delete_files_without_db_entry(files_on_disk: list[str]):
        """Delete files where file is not inside frigate db."""
        files_to_delete = []
        # the row of a recently moved segment can still be queued in the
        # recording process's writer (record.pipeline.async_db_writes) or be
        # due from unclean-shutdown recovery; such a file is not an orphan yet
        moved_before = datetime.datetime.now().timestamp() - SYNC_FILE_GRACE

        for file in files_on_disk:
            if not Recordings.select().where(Recordings.path == file).exists():
                try:
                    if os.path.getmtime(file) >= moved_before:
                        continue
                except OSError:
                    continue

                files_to_delete.append(file)

        if len(files_to_delete) == 0:
            return True

        logger.info(
            f"Deleting {len(files_to_delete)} recordings files with missing DB entries"
        )

        if float(len(files_to_delete)) / max(1, len(files_on_disk)) > 0.5:
            logger.debug(
                f"Deleting {(len(files_to_delete) / max(1, len(files_on_disk)) * 100):.2f}% of recordings DB entries, could be due to configuration error. Aborting..."
            )
            return False

        for file in files_to_delete:
            os.unlink(file)

        # the cleanup run removes only directories it emptied itself
        remove_empty_parent_directories(
            {os.path.dirname(file) for file in files_to_delete}, RECORD_DIR
        )

        return True

    logger.debug("Start sync recordings.")

    # start checking on the hour 36 hours ago
    check_point = datetime.datetime.now().replace(
        minute=0, second=0, microsecond=0
    ).astimezone(datetime.timezone.utc) - datetime.timedelta(hours=36)
    db_success = delete_db_entries_without_file(check_point.timestamp())

    # only try to cleanup files if db cleanup was successful
    if db_success:
        if limited:
            # get recording files from last 36 hours
            hour_check = f"{RECORD_DIR}/{check_point.strftime('%Y-%m-%d/%H')}"
            files_on_disk = {
                os.path.join(root, file)
                for root, _, files in os.walk(RECORD_DIR)
                for file in files
                if root > hour_check
            }
        else:
            # get all recordings files on disk and put them in a set
            files_on_disk = {
                os.path.join(root, file)
                for root, _, files in os.walk(RECORD_DIR)
                for file in files
            }

        delete_files_without_db_entry(files_on_disk)

    logger.debug("End sync recordings.")
