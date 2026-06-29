"""Cleanup recordings that are expired based on retention config."""

import datetime
import itertools
import logging
import os
import threading
from multiprocessing.synchronize import Event as MpEvent
from pathlib import Path
from typing import Optional

from playhouse.sqlite_ext import SqliteExtDatabase

from frigate.config import CameraConfig, FrigateConfig, RetainModeEnum
from frigate.const import CACHE_DIR, CLIPS_DIR, MAX_WAL_SIZE, RECORD_DIR
from frigate.models import Previews, Recordings, ReviewSegment, UserReviewStatus
from frigate.record.util import remove_empty_directories, sync_recordings
from frigate.util.builtin import clear_and_unlink
from frigate.util.time import get_tomorrow_at_time

logger = logging.getLogger(__name__)


class RecordingCleanup(threading.Thread):
    """Cleanup existing recordings based on retention config."""

    def __init__(self, config: FrigateConfig, stop_event: MpEvent) -> None:
        super().__init__(name="recording_cleanup")
        self.config = config
        self.stop_event = stop_event

    def clean_tmp_previews(self) -> None:
        """delete any previews in the cache that are more than 1 hour old."""
        for p in Path(CACHE_DIR).rglob("preview_*.mp4"):
            logger.debug(f"Checking preview {p}.")
            if p.stat().st_mtime < (datetime.datetime.now().timestamp() - 60 * 60):
                logger.debug("Deleting preview.")
                clear_and_unlink(p)

    def clean_tmp_clips(self) -> None:
        """delete any clips in the cache that are more than 1 hour old."""
        for p in Path(os.path.join(CLIPS_DIR, "cache")).rglob("clip_*.mp4"):
            logger.debug(f"Checking tmp clip {p}.")
            if p.stat().st_mtime < (datetime.datetime.now().timestamp() - 60 * 60):
                logger.debug("Deleting tmp clip.")
                clear_and_unlink(p)

    def truncate_wal(self) -> None:
        """check if the WAL needs to be manually truncated."""

        # by default the WAL should be check-pointed automatically
        # however, high levels of activity can prevent an opportunity
        # for the checkpoint to be finished which means the WAL will grow
        # without bound

        # with auto checkpoint most users should never hit this

        if (
            os.stat(f"{self.config.database.path}-wal").st_size / (1024 * 1024)
        ) > MAX_WAL_SIZE:
            db = SqliteExtDatabase(self.config.database.path)
            db.execute_sql("PRAGMA wal_checkpoint(TRUNCATE);")
            db.close()

    def expire_review_segments(self, config: CameraConfig, now: datetime) -> None:
        """Delete review segments that are expired"""
        alert_expire_date = (
            now - datetime.timedelta(days=config.record.alerts.retain.days)
        ).timestamp()
        detection_expire_date = (
            now - datetime.timedelta(days=config.record.detections.retain.days)
        ).timestamp()
        expired_reviews: ReviewSegment = (
            ReviewSegment.select(ReviewSegment.id, ReviewSegment.thumb_path)
            .where(ReviewSegment.camera == config.name)
            .where(
                (
                    (ReviewSegment.severity == "alert")
                    & (ReviewSegment.end_time < alert_expire_date)
                )
                | (
                    (ReviewSegment.severity == "detection")
                    & (ReviewSegment.end_time < detection_expire_date)
                )
            )
            .namedtuples()
        )

        thumbs_to_delete = list(map(lambda x: x[1], expired_reviews))
        for thumb_path in thumbs_to_delete:
            Path(thumb_path).unlink(missing_ok=True)

        max_deletes = 100000
        deleted_reviews_list = list(map(lambda x: x[0], expired_reviews))
        for i in range(0, len(deleted_reviews_list), max_deletes):
            ReviewSegment.delete().where(
                ReviewSegment.id << deleted_reviews_list[i : i + max_deletes]
            ).execute()
            UserReviewStatus.delete().where(
                UserReviewStatus.review_segment
                << deleted_reviews_list[i : i + max_deletes]
            ).execute()

    def expire_existing_camera_recordings(
        self,
        continuous_expire_date: float,
        motion_expire_date: float,
        config: CameraConfig,
        reviews: ReviewSegment,
        variant: Optional[str] = None,
        hard_cap_date: Optional[float] = None,
    ) -> list[tuple[float, float]]:
        """Delete recordings for existing camera based on retention config.

        When ``variant`` is provided, only recordings with that variant are
        considered. When ``hard_cap_date`` is provided, recordings older than
        that timestamp are deleted unconditionally regardless of motion/event
        overlap — this implements the per-variant retain_days hard ceiling.

        Returns the list of (start_time, end_time) tuples that were kept so
        the caller can decide which previews to retain.
        """
        # Get the timestamp for cutoff of retained days

        # Get recordings to check for expiration
        query = (
            Recordings.select(
                Recordings.id,
                Recordings.start_time,
                Recordings.end_time,
                Recordings.path,
                Recordings.objects,
                Recordings.motion,
                Recordings.dBFS,
            )
            .where(Recordings.camera == config.name)
        )
        if variant is not None:
            query = query.where(Recordings.variant == variant)
        query = query.where(
            (
                (Recordings.end_time < continuous_expire_date)
                & (Recordings.motion == 0)
                & (Recordings.dBFS == 0)
            )
            | (Recordings.end_time < motion_expire_date)
        )
        recordings: Recordings = (
            query.order_by(Recordings.start_time).namedtuples().iterator()
        )

        # loop over recordings and see if they overlap with any non-expired reviews
        # TODO: expire segments based on segment stats according to config
        review_start = 0
        deleted_recordings = set()
        kept_recordings: list[tuple[float, float]] = []
        recording: Recordings
        for recording in recordings:
            # Hard cap: per-variant retain_days deletes anything older than the
            # cap regardless of motion or event overlap.
            if hard_cap_date is not None and recording.end_time < hard_cap_date:
                Path(recording.path).unlink(missing_ok=True)
                deleted_recordings.add(recording.id)
                continue

            keep = False
            mode = None
            # Now look for a reason to keep this recording segment
            for idx in range(review_start, len(reviews)):
                review: ReviewSegment = reviews[idx]
                severity = review.severity
                pre_capture = config.record.get_review_pre_capture(severity)
                post_capture = config.record.get_review_post_capture(severity)

                # if the review starts in the future, stop checking reviews
                # and let this recording segment expire
                if review.start_time - pre_capture > recording.end_time:
                    keep = False
                    break

                # if the review is in progress or ends after the recording starts, keep it
                # and stop looking at reviews
                if (
                    review.end_time is None
                    or review.end_time + post_capture >= recording.start_time
                ):
                    keep = True
                    mode = (
                        config.record.alerts.retain.mode
                        if review.severity == "alert"
                        else config.record.detections.retain.mode
                    )
                    break

                # if the review ends before this recording segment starts, skip
                # this review and check the next review for an overlap.
                # since the review and recordings are sorted, we can skip review
                # that end before the previous recording segment started on future segments
                if review.end_time + post_capture < recording.start_time:
                    review_start = idx

            # Delete recordings outside of the retention window or based on the retention mode
            if (
                not keep
                or (
                    mode == RetainModeEnum.motion
                    and recording.motion == 0
                    and recording.objects == 0
                    and recording.dBFS == 0
                )
                or (mode == RetainModeEnum.active_objects and recording.objects == 0)
            ):
                Path(recording.path).unlink(missing_ok=True)
                deleted_recordings.add(recording.id)
            else:
                kept_recordings.append((recording.start_time, recording.end_time))

        # expire recordings
        logger.debug(f"Expiring {len(deleted_recordings)} recordings")
        # delete up to 100,000 at a time
        max_deletes = 100000
        deleted_recordings_list = list(deleted_recordings)
        for i in range(0, len(deleted_recordings_list), max_deletes):
            Recordings.delete().where(
                Recordings.id << deleted_recordings_list[i : i + max_deletes]
            ).execute()

        return kept_recordings

    def expire_camera_previews(
        self,
        continuous_expire_date: float,
        motion_expire_date: float,
        config: CameraConfig,
        kept_recordings: list[tuple[float, float]],
    ) -> None:
        """Delete previews for a camera that no longer overlap a kept recording."""
        previews: list[Previews] = (
            Previews.select(
                Previews.id,
                Previews.start_time,
                Previews.end_time,
                Previews.path,
            )
            .where(
                (Previews.camera == config.name)
                & (Previews.end_time < continuous_expire_date)
                & (Previews.end_time < motion_expire_date)
            )
            .order_by(Previews.start_time)
            .namedtuples()
            .iterator()
        )

        # expire previews
        kept_recordings = sorted(kept_recordings)
        recording_start = 0
        deleted_previews = set()
        for preview in previews:
            keep = False
            # look for a reason to keep this preview
            for idx in range(recording_start, len(kept_recordings)):
                start_time, end_time = kept_recordings[idx]

                # if the recording starts in the future, stop checking recordings
                # and let this preview expire
                if start_time > preview.end_time:
                    keep = False
                    break

                # if the recording ends after the preview starts, keep it
                # and stop looking at recordings
                if end_time >= preview.start_time:
                    keep = True
                    break

                # if the recording ends before this preview starts, skip
                # this recording and check the next recording for an overlap.
                # since the kept recordings and previews are sorted, we can skip recordings
                # that end before the current preview started
                if end_time < preview.start_time:
                    recording_start = idx

            # Delete previews without any relevant recordings
            if not keep:
                Path(preview.path).unlink(missing_ok=True)
                deleted_previews.add(preview.id)

        # expire previews
        logger.debug(f"Expiring {len(deleted_previews)} previews")
        # delete up to 100,000 at a time
        max_deletes = 100000
        deleted_previews_list = list(deleted_previews)
        for i in range(0, len(deleted_previews_list), max_deletes):
            Previews.delete().where(
                Previews.id << deleted_previews_list[i : i + max_deletes]
            ).execute()

    def expire_recordings(self) -> None:
        """Delete recordings based on retention config."""
        logger.debug("Start expire recordings.")
        logger.debug("Start deleted cameras.")

        # Handle deleted cameras
        expire_days = max(
            self.config.record.continuous.days, self.config.record.motion.days
        )
        expire_before = (
            datetime.datetime.now() - datetime.timedelta(days=expire_days)
        ).timestamp()
        no_camera_recordings: Recordings = (
            Recordings.select(
                Recordings.id,
                Recordings.path,
            )
            .where(
                Recordings.camera.not_in(list(self.config.cameras.keys())),
                Recordings.end_time < expire_before,
            )
            .namedtuples()
            .iterator()
        )

        deleted_recordings = set()
        for recording in no_camera_recordings:
            Path(recording.path).unlink(missing_ok=True)
            deleted_recordings.add(recording.id)

        logger.debug(f"Expiring {len(deleted_recordings)} recordings")
        # delete up to 100,000 at a time
        max_deletes = 100000
        deleted_recordings_list = list(deleted_recordings)
        for i in range(0, len(deleted_recordings_list), max_deletes):
            Recordings.delete().where(
                Recordings.id << deleted_recordings_list[i : i + max_deletes]
            ).execute()
        logger.debug("End deleted cameras.")

        logger.debug("Start all cameras.")
        for camera, config in self.config.cameras.items():
            logger.debug(f"Start camera: {camera}.")
            now = datetime.datetime.now()

            self.expire_review_segments(config, now)

            base_continuous_days = config.record.continuous.days
            base_motion_days = max(
                config.record.motion.days, config.record.continuous.days
            )

            variants = config.get_record_variants()
            # Per-variant retain_days override REPLACES continuous.days; motion
            # extension still applies (bounded by retain_days as hard ceiling).
            variant_dates: list[
                tuple[str, float, float, Optional[float]]
            ] = []
            for variant in variants:
                override = config.get_variant_retain_days(variant)
                continuous_days = (
                    override if override is not None else base_continuous_days
                )
                # The motion/outer window is the actual outer deletion bound
                # (segments older than it are deleted regardless of motion), so
                # it must reach at least the override for continuous retention to
                # span the full override. The hard_cap_date below still clamps
                # the ceiling when base_motion_days legitimately exceeds the
                # override.
                motion_days = (
                    max(base_motion_days, override)
                    if override is not None
                    else base_motion_days
                )
                continuous_expire_date_v = (
                    now - datetime.timedelta(days=continuous_days)
                ).timestamp()
                motion_expire_date_v = (
                    now - datetime.timedelta(days=motion_days)
                ).timestamp()
                hard_cap_date_v: Optional[float] = (
                    (now - datetime.timedelta(days=override)).timestamp()
                    if override is not None
                    else None
                )
                variant_dates.append(
                    (
                        variant,
                        continuous_expire_date_v,
                        motion_expire_date_v,
                        hard_cap_date_v,
                    )
                )

            # Reviews are camera-wide. Pull them once based on the widest motion
            # window across variants so per-variant decisions all have data.
            widest_motion_expire_date = min(
                motion_date for (_, _, motion_date, _) in variant_dates
            )
            reviews: ReviewSegment = (
                ReviewSegment.select(
                    ReviewSegment.start_time,
                    ReviewSegment.end_time,
                    ReviewSegment.severity,
                )
                .where(
                    ReviewSegment.camera == camera,
                    # need to ensure segments for all reviews starting
                    # before the expire date are included
                    ReviewSegment.start_time < widest_motion_expire_date,
                )
                .order_by(ReviewSegment.start_time)
                .namedtuples()
            )
            # Materialize reviews so per-variant loops can iterate multiple times
            reviews = list(reviews)

            all_kept: list[tuple[float, float]] = []
            for (
                variant,
                continuous_expire_date_v,
                motion_expire_date_v,
                hard_cap_date_v,
            ) in variant_dates:
                kept = self.expire_existing_camera_recordings(
                    continuous_expire_date_v,
                    motion_expire_date_v,
                    config,
                    reviews,
                    variant=variant,
                    hard_cap_date=hard_cap_date_v,
                )
                all_kept.extend(kept)

            # Sweep rows whose variant is no longer configured for this camera
            # (e.g. camera switched from dual-stream back to single-stream);
            # without this they would never be visited by the per-variant loop
            # above and would accumulate forever.
            base_expire_date = (
                now - datetime.timedelta(days=base_motion_days)
            ).timestamp()
            orphan_recordings = (
                Recordings.select(Recordings.id, Recordings.path)
                .where(
                    Recordings.camera == config.name,
                    Recordings.variant.not_in(variants),
                    Recordings.end_time < base_expire_date,
                )
                .namedtuples()
                .iterator()
            )
            orphan_ids = []
            for recording in orphan_recordings:
                Path(recording.path).unlink(missing_ok=True)
                orphan_ids.append(recording.id)
            if orphan_ids:
                logger.debug(
                    f"Expiring {len(orphan_ids)} recordings with unconfigured variants for {camera}"
                )
                max_deletes = 100000
                for i in range(0, len(orphan_ids), max_deletes):
                    Recordings.delete().where(
                        Recordings.id << orphan_ids[i : i + max_deletes]
                    ).execute()

            # Previews are camera-wide (not per-variant); use the widest window
            # so a preview is kept if any variant still retains an overlapping segment.
            widest_continuous_expire_date = min(
                cont_date for (_, cont_date, _, _) in variant_dates
            )
            self.expire_camera_previews(
                widest_continuous_expire_date,
                widest_motion_expire_date,
                config,
                all_kept,
            )
            logger.debug(f"End camera: {camera}.")

        logger.debug("End all cameras.")
        logger.debug("End expire recordings.")

    def run(self) -> None:
        if self.config.safe_mode:
            logger.info("Safe mode enabled, skipping recording cleanup")
            return

        # on startup sync recordings with disk if enabled
        if self.config.record.sync_recordings:
            sync_recordings(limited=False)
            next_sync = get_tomorrow_at_time(3)

        # Expire tmp clips every minute, recordings and clean directories every hour.
        for counter in itertools.cycle(range(self.config.record.expire_interval)):
            if self.stop_event.wait(60):
                logger.info("Exiting recording cleanup...")
                break

            self.clean_tmp_previews()

            if (
                self.config.record.sync_recordings
                and datetime.datetime.now().astimezone(datetime.timezone.utc)
                > next_sync
            ):
                sync_recordings(limited=True)
                next_sync = get_tomorrow_at_time(3)

            if counter == 0:
                self.clean_tmp_clips()
                self.expire_recordings()
                remove_empty_directories(RECORD_DIR)
                self.truncate_wal()
