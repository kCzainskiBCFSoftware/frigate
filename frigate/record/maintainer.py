"""Maintain recording segments in cache."""

import asyncio
import datetime
import logging
import os
import random
import string
import threading
import time
from collections import Counter, defaultdict
from contextlib import AbstractAsyncContextManager, nullcontext
from multiprocessing.synchronize import Event as MpEvent
from pathlib import Path
from typing import Any, Optional, Tuple

import numpy as np
import psutil

from frigate.comms.detections_updater import DetectionSubscriber, DetectionTypeEnum
from frigate.comms.inter_process import InterProcessRequestor
from frigate.comms.recordings_updater import (
    RecordingsDataPublisher,
    RecordingsDataTypeEnum,
)
from frigate.config import FrigateConfig, RetainModeEnum
from frigate.config.camera.updater import (
    CameraConfigUpdateEnum,
    CameraConfigUpdateSubscriber,
)
from frigate.const import (
    CACHE_DIR,
    CACHE_SEGMENT_FORMAT,
    FAST_QUEUE_TIMEOUT,
    INSERT_MANY_RECORDINGS,
    MAX_SEGMENT_DURATION,
    RECORD_DIR,
)
from frigate.models import Recordings, ReviewSegment
from frigate.record.cache_watch import CachedSegment, cache_space, reserve_discards
from frigate.record.writer import RecordingsWriter
from frigate.review.types import SeverityEnum
from frigate.util.services import get_video_properties

logger = logging.getLogger(__name__)


DEFAULT_RECORD_VARIANT = "main"

# at most one "frame processing is behind" warning per camera in this window
PROCESSING_LAG_LOG_INTERVAL = 300


def _parse_cache_segment(basename: str) -> Optional[Tuple[str, str, str]]:
    """Parse a cache filename and return (camera, variant, date_str).

    Supports both legacy `camera@date` and new `camera@variant@date` formats.
    Returns None if the format is not recognized.
    """
    parts = basename.rsplit("@", maxsplit=2)
    if len(parts) == 3:
        camera, variant, date = parts
        return camera, variant, date
    if len(parts) == 2:
        camera, date = parts
        return camera, DEFAULT_RECORD_VARIANT, date
    return None


class SegmentInfo:
    def __init__(
        self,
        motion_count: int,
        active_object_count: int,
        region_count: int,
        average_dBFS: int,
    ) -> None:
        self.motion_count = motion_count
        self.active_object_count = active_object_count
        self.region_count = region_count
        self.average_dBFS = average_dBFS

    def should_discard_segment(self, retain_mode: RetainModeEnum) -> bool:
        keep = False

        # all mode should never discard
        if retain_mode == RetainModeEnum.all:
            keep = True

        # motion mode should keep if motion or audio is detected
        if (
            not keep
            and retain_mode == RetainModeEnum.motion
            and (self.motion_count > 0 or self.average_dBFS != 0)
        ):
            keep = True

        # active objects mode should keep if any active objects are detected
        if not keep and self.active_object_count > 0:
            keep = True

        return not keep


class RecordingMaintainer(threading.Thread):
    def __init__(
        self,
        config: FrigateConfig,
        stop_event: MpEvent,
        writer: Optional[RecordingsWriter] = None,
    ):
        super().__init__(name="recording_maintainer")
        self.config = config
        # record.pipeline.async_db_writes: rows go to the writer thread instead
        # of a request that blocks the pass until they commit
        self.writer = writer

        # create communication for retained recordings
        self.requestor = InterProcessRequestor()
        self.config_subscriber = CameraConfigUpdateSubscriber(
            self.config,
            self.config.cameras,
            [CameraConfigUpdateEnum.add, CameraConfigUpdateEnum.record],
        )
        self.detection_subscriber = DetectionSubscriber(DetectionTypeEnum.all.value)
        self.recordings_publisher = RecordingsDataPublisher()

        self.stop_event = stop_event
        self.object_recordings_info: dict[str, list] = defaultdict(list)
        self.audio_recordings_info: dict[str, list] = defaultdict(list)
        self.end_time_cache: dict[
            str, Tuple[datetime.datetime, float, dict[str, Any]]
        ] = {}
        self.unexpected_cache_files_logged: bool = False
        self.processing_lag_logged: dict[str, float] = {}
        # created per pass: each pass runs in its own event loop
        self.move_slots: Optional[asyncio.Semaphore] = None

    async def move_files(self) -> None:
        cache_files = [
            d
            for d in os.listdir(CACHE_DIR)
            if os.path.isfile(os.path.join(CACHE_DIR, d))
            and d.endswith(".mp4")
            and not d.startswith("preview_")
        ]

        # publish newest cached segment per camera (across all variants)
        newest_cache_segments: dict[str, dict[str, Any]] = {}
        for cache in cache_files:
            cache_path = os.path.join(CACHE_DIR, cache)
            basename = os.path.splitext(cache)[0]
            parsed = _parse_cache_segment(basename)
            if parsed is None:
                if not self.unexpected_cache_files_logged:
                    logger.warning("Skipping unexpected files in cache")
                    self.unexpected_cache_files_logged = True
                continue
            camera, _variant, date = parsed

            try:
                start_time = datetime.datetime.strptime(
                    date, CACHE_SEGMENT_FORMAT
                ).astimezone(datetime.timezone.utc)
            except ValueError:
                if not self.unexpected_cache_files_logged:
                    logger.warning("Skipping unexpected files in cache")
                    self.unexpected_cache_files_logged = True
                continue
            if (
                camera not in newest_cache_segments
                or start_time > newest_cache_segments[camera]["start_time"]
            ):
                newest_cache_segments[camera] = {
                    "start_time": start_time,
                    "cache_path": cache_path,
                }

        for camera, newest in newest_cache_segments.items():
            self.recordings_publisher.publish(
                (
                    camera,
                    newest["start_time"].timestamp(),
                    newest["cache_path"],
                ),
                RecordingsDataTypeEnum.latest.value,
            )
        # publish None for cameras with no cache files (but only if we know the camera exists)
        for camera_name in self.config.cameras:
            if camera_name not in newest_cache_segments:
                self.recordings_publisher.publish(
                    (camera_name, None, None),
                    RecordingsDataTypeEnum.latest.value,
                )

        files_in_use = []
        for process in psutil.process_iter():
            try:
                if process.name() != "ffmpeg":
                    continue
                file_list = process.open_files()
                if file_list:
                    for nt in file_list:
                        if nt.path.startswith(CACHE_DIR):
                            files_in_use.append(nt.path.split("/")[-1])
            except psutil.Error:
                continue

        # group recordings by camera (skip in-use for validation/moving)
        grouped_recordings: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
        for cache in cache_files:
            # Skip files currently in use
            if cache in files_in_use:
                continue

            cache_path = os.path.join(CACHE_DIR, cache)
            basename = os.path.splitext(cache)[0]
            parsed = _parse_cache_segment(basename)
            if parsed is None:
                if not self.unexpected_cache_files_logged:
                    logger.warning("Skipping unexpected files in cache")
                    self.unexpected_cache_files_logged = True
                continue
            camera, variant, date = parsed

            # important that start_time is utc because recordings are stored and compared in utc
            try:
                start_time = datetime.datetime.strptime(
                    date, CACHE_SEGMENT_FORMAT
                ).astimezone(datetime.timezone.utc)
            except ValueError:
                if not self.unexpected_cache_files_logged:
                    logger.warning("Skipping unexpected files in cache")
                    self.unexpected_cache_files_logged = True
                continue

            grouped_recordings[camera].append(
                {
                    "cache_path": cache_path,
                    "start_time": start_time,
                    "variant": variant,
                }
            )

        for camera in grouped_recordings.keys():
            # sort based on start time
            grouped_recordings[camera] = sorted(
                grouped_recordings[camera], key=lambda s: s["start_time"]
            )

        # the backlog limit is applied after the moves (discard_backlog), so a
        # backlog this pass can move is never discarded; the free-space
        # reserve keeps the tmpfs from filling meanwhile
        self.enforce_cache_reserve(grouped_recordings)
        self.move_slots = (
            asyncio.Semaphore(self.config.record.pipeline.move_concurrency)
            if self.config.record.pipeline.move_concurrency > 0
            else None
        )

        tasks = []
        saved: dict[str, Optional[float]] = {}
        for camera, recordings in grouped_recordings.items():
            if not recordings:
                continue

            # clear out all the object recording info for old frames
            while (
                len(self.object_recordings_info[camera]) > 0
                and self.object_recordings_info[camera][0][0]
                < recordings[0]["start_time"].timestamp()
            ):
                self.object_recordings_info[camera].pop(0)

            # clear out all the audio recording info for old frames
            while (
                len(self.audio_recordings_info[camera]) > 0
                and self.audio_recordings_info[camera][0][0]
                < recordings[0]["start_time"].timestamp()
            ):
                self.audio_recordings_info[camera].pop(0)

            # get all reviews with the end time after the start of the oldest cache file
            # or with end_time None
            reviews: ReviewSegment = (
                ReviewSegment.select(
                    ReviewSegment.start_time,
                    ReviewSegment.end_time,
                    ReviewSegment.severity,
                    ReviewSegment.data,
                )
                .where(
                    ReviewSegment.camera == camera,
                    (ReviewSegment.end_time == None)
                    | (
                        ReviewSegment.end_time
                        >= recordings[0]["start_time"].timestamp()
                    ),
                )
                .order_by(ReviewSegment.start_time)
            )

            tasks.extend(
                [self.validate_and_move_segment(camera, reviews, r) for r in recordings]
            )

            # publish most recently available recording time and None if disabled
            saved[camera] = (
                recordings[0]["start_time"].timestamp()
                if self.config.cameras[camera].record.enabled
                else None
            )

            # with the writer, saved is published once earlier rows are written
            if self.writer is None:
                self.recordings_publisher.publish(
                    (camera, saved[camera], None),
                    RecordingsDataTypeEnum.saved.value,
                )

        recordings_to_insert: list[Optional[Recordings]] = await asyncio.gather(*tasks)

        for camera, recordings in grouped_recordings.items():
            self.discard_backlog(camera, recordings)

        self.enforce_cache_reserve(grouped_recordings)

        rows = [r for r in recordings_to_insert if r is not None]

        if self.writer is not None:
            # waits only while the writer's queue is full
            self.writer.submit(rows, saved)
            return

        # fire and forget recordings entries
        result = self.requestor.send_data(INSERT_MANY_RECORDINGS, rows)

        if isinstance(result, dict) and not result.get("ok", True):
            logger.error(
                f"Failed to write {len(rows)} recording rows: {result.get('error')}"
            )

    def drop_segment(self, cache_path: str) -> None:
        Path(cache_path).unlink(missing_ok=True)
        self.end_time_cache.pop(cache_path, None)

    def discard_backlog(self, camera: str, recordings: list[dict[str, Any]]) -> None:
        """Apply the cache backlog limit to what is still cached, per variant."""
        keep_count = self.config.record.pipeline.max_cached_segments
        most_recently_processed_frame_time = self.processed_frame_time(camera)

        by_variant: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
        for rec in recordings:
            if os.path.exists(rec["cache_path"]):
                by_variant[rec["variant"]].append(rec)

        for remaining in by_variant.values():
            processed_segment_count = len(
                [
                    r
                    for r in remaining
                    if r["start_time"].timestamp() < most_recently_processed_frame_time
                ]
            )

            # see if the recording mover is too slow and segments need to be deleted
            if processed_segment_count > keep_count:
                logger.warning(
                    f"Unable to keep up with recording segments in cache for {camera}. Keeping the {keep_count} most recent segments out of {processed_segment_count} and discarding the rest..."
                )
                for rec in remaining[:-keep_count]:
                    self.drop_segment(rec["cache_path"])
                remaining = remaining[-keep_count:]

            # see if detection has failed and unprocessed segments need to be deleted
            unprocessed_segment_count = len(remaining) - processed_segment_count
            if unprocessed_segment_count > keep_count:
                logger.warning(
                    f"Too many unprocessed recording segments in cache for {camera}. This likely indicates an issue with the detect stream, keeping the {keep_count} most recent segments out of {unprocessed_segment_count} and discarding the rest..."
                )
                for rec in remaining[:-keep_count]:
                    self.drop_segment(rec["cache_path"])

    def enforce_cache_reserve(
        self, grouped_recordings: dict[str, list[dict[str, Any]]]
    ) -> None:
        """Discard the oldest closed segments while the cache is below its reserve."""
        reserve = self.config.record.pipeline.cache_free_reserve

        if reserve <= 0:
            return

        try:
            free_bytes, total_bytes = cache_space(CACHE_DIR)
        except OSError:
            return

        discards = reserve_discards(
            [
                CachedSegment(
                    camera, r["variant"], r["start_time"].timestamp(), r["cache_path"]
                )
                for camera, recordings in grouped_recordings.items()
                for r in recordings
            ],
            free_bytes,
            total_bytes,
            reserve,
        )

        if not discards:
            return

        for segment in discards:
            self.drop_segment(segment.path)

        dropped = {segment.path for segment in discards}
        for camera, recordings in grouped_recordings.items():
            grouped_recordings[camera] = [
                r for r in recordings if r["cache_path"] not in dropped
            ]

        per_camera = Counter(segment.camera for segment in discards)
        logger.error(
            "Recording cache is %.0f%% free, below the %g%% reserve. Discarded the "
            "%d oldest segments (%s)",
            free_bytes / total_bytes * 100,
            reserve,
            len(discards),
            ", ".join(f"{camera}: {count}" for camera, count in per_camera.items()),
        )

    def processed_frame_time(self, camera: str) -> float:
        """Time of the newest frame processed for a camera, 0 if none yet."""
        camera_info = self.object_recordings_info[camera]
        return camera_info[-1][0] if len(camera_info) > 0 else 0

    def frames_processed(self, camera: str, end_time: datetime.datetime) -> bool:
        """Whether frame processing has passed the end of a segment."""
        return (
            datetime.datetime.fromtimestamp(
                self.processed_frame_time(camera)
            ).astimezone(datetime.timezone.utc)
            >= end_time
        )

    def processing_timed_out(self, end_time: datetime.datetime) -> bool:
        """Whether a continuous segment has waited long enough for processing."""
        timeout = self.config.record.pipeline.processing_timeout
        return (
            timeout > 0
            and datetime.datetime.now().timestamp() - end_time.timestamp() >= timeout
        )

    def warn_processing_lag(self, camera: str) -> None:
        now = datetime.datetime.now().timestamp()

        if (
            now - self.processing_lag_logged.get(camera, 0)
            < PROCESSING_LAG_LOG_INTERVAL
        ):
            return

        self.processing_lag_logged[camera] = now
        processed_time = self.processed_frame_time(camera)
        behind = (
            f"is {now - processed_time:.0f}s behind"
            if processed_time > 0
            else "has not reported any frames"
        )
        logger.warning(
            f"Frame processing for {camera} {behind}. Moving its continuous recording segments without activity stats"
        )

    def move_slot(self) -> AbstractAsyncContextManager:
        """One of record.pipeline.move_concurrency slots for a probe or copy."""
        return self.move_slots if self.move_slots is not None else nullcontext()

    async def validate_and_move_segment(
        self, camera: str, reviews: list[ReviewSegment], recording: dict[str, Any]
    ) -> Optional[Recordings]:
        cache_path: str = recording["cache_path"]
        start_time: datetime.datetime = recording["start_time"]
        variant: str = recording.get("variant", DEFAULT_RECORD_VARIANT)
        record_config = self.config.cameras[camera].record

        # Just delete files if recordings are turned off
        if (
            camera not in self.config.cameras
            or not self.config.cameras[camera].record.enabled
        ):
            self.drop_segment(cache_path)
            return None

        media_info: dict[str, Any] = {}
        if cache_path in self.end_time_cache:
            end_time, duration, media_info = self.end_time_cache[cache_path]
        else:
            async with self.move_slot():
                segment_info = await get_video_properties(
                    self.config.ffmpeg, cache_path, get_duration=True
                )

            if not segment_info.get("has_valid_video", False):
                logger.warning(
                    f"Invalid or missing video stream in segment {cache_path}. Discarding."
                )
                self.recordings_publisher.publish(
                    (camera, start_time.timestamp(), cache_path),
                    RecordingsDataTypeEnum.invalid.value,
                )
                self.drop_segment(cache_path)
                return None

            duration = float(segment_info.get("duration", -1))
            media_info = segment_info

            # ensure duration is within expected length
            if 0 < duration < MAX_SEGMENT_DURATION:
                end_time = start_time + datetime.timedelta(seconds=duration)
                self.end_time_cache[cache_path] = (end_time, duration, media_info)
            else:
                if duration == -1:
                    logger.warning(f"Failed to probe corrupt segment {cache_path}")

                logger.warning(f"Discarding a corrupt recording segment: {cache_path}")
                self.recordings_publisher.publish(
                    (camera, start_time.timestamp(), cache_path),
                    RecordingsDataTypeEnum.invalid.value,
                )
                self.drop_segment(cache_path)
                return None

            # this segment has a valid duration and has video data, so publish an update
            self.recordings_publisher.publish(
                (camera, start_time.timestamp(), cache_path),
                RecordingsDataTypeEnum.valid.value,
            )

        record_config = self.config.cameras[camera].record
        highest = None

        if record_config.continuous.days > 0:
            highest = "continuous"
        elif record_config.motion.days > 0:
            highest = "motion"

        # if we have continuous or motion recording enabled
        # we should first just check if this segment matches that
        # and avoid any DB calls
        if highest is not None:
            # assume that empty means the relevant recording info has not been received yet
            # ensure delayed segment info does not lead to lost segments
            if self.frames_processed(camera, end_time):
                record_mode = (
                    RetainModeEnum.all
                    if highest == "continuous"
                    else RetainModeEnum.motion
                )
                return await self.move_segment(
                    camera,
                    start_time,
                    end_time,
                    duration,
                    cache_path,
                    record_mode,
                    variant,
                    media_info,
                )

            if highest == "continuous":
                # continuous retention keeps the segment whatever its stats, so
                # it need not wait for frame processing beyond the timeout
                if self.processing_timed_out(end_time):
                    self.warn_processing_lag(camera)
                    return await self.move_segment(
                        camera,
                        start_time,
                        end_time,
                        duration,
                        cache_path,
                        RetainModeEnum.all,
                        variant,
                        media_info,
                        stats_known=False,
                    )

                # wait in the cache: a review-based move now would decide on
                # incomplete stats and could discard the segment
                return None

        # we fell through the continuous / motion check, so we need to check the review items
        # if the cached segment overlaps with the review items:
        overlaps = False
        for review in reviews:
            severity = SeverityEnum[review.severity]

            # if the review item starts in the future, stop checking review items
            # and remove this segment
            if (
                review.start_time - record_config.get_review_pre_capture(severity)
            ) > end_time.timestamp():
                overlaps = False
                break

            # if the review item is in progress or ends after the recording starts, keep it
            # and stop looking at review items
            if (
                review.end_time is None
                or (review.end_time + record_config.get_review_post_capture(severity))
                >= start_time.timestamp()
            ):
                overlaps = True
                break

        if overlaps:
            # keep or discard only on complete stats; wait for frame processing
            if not self.frames_processed(camera, end_time):
                return None

            record_mode = (
                record_config.alerts.retain.mode
                if review.severity == "alert"
                else record_config.detections.retain.mode
            )
            # move from cache to recordings immediately
            return await self.move_segment(
                camera,
                start_time,
                end_time,
                duration,
                cache_path,
                record_mode,
                variant,
                media_info,
            )
        # if it doesn't overlap with an review item, go ahead and drop the segment
        # if it ends more than the configured pre_capture for the camera
        # BUT only if continuous/motion is NOT enabled (otherwise wait for processing)
        elif highest is None:
            camera_info = self.object_recordings_info[camera]
            most_recently_processed_frame_time = (
                camera_info[-1][0] if len(camera_info) > 0 else 0
            )
            retain_cutoff = datetime.datetime.fromtimestamp(
                most_recently_processed_frame_time - record_config.event_pre_capture
            ).astimezone(datetime.timezone.utc)
            if end_time < retain_cutoff:
                self.drop_segment(cache_path)

    def segment_stats(
        self, camera: str, start_time: datetime.datetime, end_time: datetime.datetime
    ) -> SegmentInfo:
        video_frame_count = 0
        active_count = 0
        region_count = 0
        motion_count = 0
        for frame in self.object_recordings_info[camera]:
            # frame is after end time of segment
            if frame[0] > end_time.timestamp():
                break
            # frame is before start time of segment
            if frame[0] < start_time.timestamp():
                continue

            video_frame_count += 1
            active_count += len(
                [
                    o
                    for o in frame[1]
                    if not o["false_positive"] and o["motionless_count"] == 0
                ]
            )
            motion_count += len(frame[2])
            region_count += len(frame[3])

        audio_values = []
        for frame in self.audio_recordings_info[camera]:
            # frame is after end time of segment
            if frame[0] > end_time.timestamp():
                break

            # frame is before start time of segment
            if frame[0] < start_time.timestamp():
                continue

            # add active audio label count to count of active objects
            active_count += len(frame[2])

            # add sound level to audio values
            audio_values.append(frame[1])

        average_dBFS = 0 if not audio_values else np.average(audio_values)

        return SegmentInfo(
            motion_count, active_count, region_count, round(average_dBFS)
        )

    async def move_segment(
        self,
        camera: str,
        start_time: datetime.datetime,
        end_time: datetime.datetime,
        duration: float,
        cache_path: str,
        store_mode: RetainModeEnum,
        variant: str = DEFAULT_RECORD_VARIANT,
        media_info: Optional[dict[str, Any]] = None,
        stats_known: bool = True,
    ) -> Optional[Recordings]:
        # stats_known=False: moved on the processing timeout; the row's activity
        # stats are stored as unknown (NULL), never as 0, so retention keeps it
        segment_info = (
            self.segment_stats(camera, start_time, end_time) if stats_known else None
        )

        # check if the segment shouldn't be stored
        if segment_info is not None and segment_info.should_discard_segment(store_mode):
            self.drop_segment(cache_path)
            return

        # directory will be in utc due to start_time being in utc; recordings are
        # written per-variant so that main and sub do not collide on disk
        directory = os.path.join(
            RECORD_DIR,
            start_time.strftime("%Y-%m-%d/%H"),
            camera,
            variant,
        )

        if not os.path.exists(directory):
            os.makedirs(directory)

        # file will be in utc due to start_time being in utc
        file_name = f"{start_time.strftime('%M.%S.mp4')}"
        file_path = os.path.join(directory, file_name)

        try:
            if not os.path.exists(file_path):
                start_frame = datetime.datetime.now().timestamp()

                # add faststart to kept segments to improve metadata reading
                async with self.move_slot():
                    p = await asyncio.create_subprocess_exec(
                        self.config.ffmpeg.ffmpeg_path,
                        "-hide_banner",
                        "-y",
                        "-i",
                        cache_path,
                        "-c",
                        "copy",
                        "-movflags",
                        "+faststart",
                        file_path,
                        stderr=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.DEVNULL,
                    )
                    await p.wait()

                if p.returncode != 0:
                    logger.error(f"Unable to convert {cache_path} to {file_path}")
                    logger.error((await p.stderr.read()).decode("ascii"))
                    return None
                else:
                    logger.debug(
                        f"Copied {file_path} in {datetime.datetime.now().timestamp() - start_frame} seconds."
                    )

                try:
                    # get the segment size of the cache file
                    # file without faststart is same size
                    segment_size_bytes = os.path.getsize(cache_path)
                    segment_size = round(float(segment_size_bytes) / pow(2, 20), 2)
                except OSError:
                    segment_size_bytes = 0
                    segment_size = 0

                os.remove(cache_path)

                rand_id = "".join(
                    random.choices(string.ascii_lowercase + string.digits, k=6)
                )

                bitrate: Optional[int] = None
                if duration > 0 and segment_size_bytes > 0:
                    bitrate = int((segment_size_bytes * 8) / duration)

                codec_name = None
                width = None
                height = None
                if media_info:
                    codec_name = media_info.get("fourcc")
                    width = media_info.get("width") or None
                    height = media_info.get("height") or None

                return {
                    Recordings.id.name: f"{start_time.timestamp()}-{rand_id}",
                    Recordings.camera.name: camera,
                    Recordings.path.name: file_path,
                    Recordings.start_time.name: start_time.timestamp(),
                    Recordings.end_time.name: end_time.timestamp(),
                    Recordings.duration.name: duration,
                    Recordings.motion.name: segment_info.motion_count
                    if segment_info
                    else None,
                    # TODO: update this to store list of active objects at some point
                    Recordings.objects.name: segment_info.active_object_count
                    if segment_info
                    else None,
                    Recordings.regions.name: segment_info.region_count
                    if segment_info
                    else None,
                    Recordings.dBFS.name: segment_info.average_dBFS
                    if segment_info
                    else None,
                    Recordings.segment_size.name: segment_size,
                    Recordings.variant.name: variant,
                    Recordings.codec_name.name: codec_name,
                    Recordings.width.name: width,
                    Recordings.height.name: height,
                    Recordings.bitrate.name: bitrate,
                    Recordings.transcoded_from_main.name: False,
                }
        except Exception as e:
            logger.error(f"Unable to store recording segment {cache_path}")
            Path(cache_path).unlink(missing_ok=True)
            logger.error(e)

        # clear end_time cache
        self.end_time_cache.pop(cache_path, None)
        return None

    def run(self) -> None:
        # Check for new files every 5 seconds
        wait_time = 0.0
        while not self.stop_event.is_set():
            time.sleep(wait_time)

            if self.stop_event.is_set():
                break

            run_start = datetime.datetime.now().timestamp()

            # check if there is an updated config
            self.config_subscriber.check_for_updates()

            stale_frame_count = 0
            stale_frame_count_threshold = 10
            # empty the object recordings info queue
            while True:
                (topic, data) = self.detection_subscriber.check_for_update(
                    timeout=FAST_QUEUE_TIMEOUT
                )

                if not topic:
                    break

                if topic == DetectionTypeEnum.video.value:
                    (
                        camera,
                        _,
                        frame_time,
                        current_tracked_objects,
                        motion_boxes,
                        regions,
                    ) = data

                    if self.config.cameras[camera].record.enabled:
                        self.object_recordings_info[camera].append(
                            (
                                frame_time,
                                current_tracked_objects,
                                motion_boxes,
                                regions,
                            )
                        )
                elif topic == DetectionTypeEnum.audio.value:
                    (
                        camera,
                        frame_time,
                        dBFS,
                        audio_detections,
                    ) = data

                    if self.config.cameras[camera].record.enabled:
                        self.audio_recordings_info[camera].append(
                            (
                                frame_time,
                                dBFS,
                                audio_detections,
                            )
                        )
                elif (
                    topic == DetectionTypeEnum.api.value or DetectionTypeEnum.lpr.value
                ):
                    continue

                if frame_time < run_start - stale_frame_count_threshold:
                    stale_frame_count += 1

            if stale_frame_count > 0:
                logger.debug(f"Found {stale_frame_count} old frames.")

            try:
                asyncio.run(self.move_files())
            except Exception as e:
                logger.error(
                    "Error occurred when attempting to maintain recording cache"
                )
                logger.error(e)
            duration = datetime.datetime.now().timestamp() - run_start
            wait_time = max(0, 5 - duration)

        self.requestor.stop()
        self.config_subscriber.stop()
        self.detection_subscriber.stop()
        self.recordings_publisher.stop()
        logger.info("Exiting recording maintenance...")
