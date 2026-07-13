"""Shared helpers for recording variants (dual-stream main/sub recordings)."""

import logging

from frigate.const import MAX_SEGMENT_DURATION
from frigate.models import Recordings

logger = logging.getLogger(__name__)

RECORDING_VARIANT_MAIN = "main"
RECORDING_VARIANT_SUB = "sub"
RECORDING_VARIANT_ALL = "all"

DEFAULT_PLAYBACK_VARIANT = RECORDING_VARIANT_SUB
DEFAULT_SNAPSHOT_VARIANT = RECORDING_VARIANT_MAIN

OTHER_VARIANT = {
    RECORDING_VARIANT_MAIN: RECORDING_VARIANT_SUB,
    RECORDING_VARIANT_SUB: RECORDING_VARIANT_MAIN,
}


def apply_variant_filter(query, variant: str):
    """Apply a variant filter to a Recordings query. 'all' is a no-op."""
    if variant and variant != RECORDING_VARIANT_ALL:
        return query.where(Recordings.variant == variant)
    return query


def recordings_overlap_clause(start_ts: float, end_ts: float):
    """Predicate matching recordings that overlap [start_ts, end_ts].

    Interval overlap is simply start_time <= end_ts AND end_time >= start_ts.
    The extra lower bound on start_time is implied for any segment shorter
    than MAX_SEGMENT_DURATION, but it gives SQLite a two-sided start_time
    range so the (camera, variant, start_time, end_time) index can seek the
    window instead of scanning the camera's entire retained history.
    """
    return (
        (Recordings.start_time <= end_ts)
        & (Recordings.end_time >= start_ts)
        & (Recordings.start_time >= start_ts - MAX_SEGMENT_DURATION)
    )


def variant_has_overlapping_recording(
    camera_name: str, variant: str, start_ts: float, end_ts: float
) -> bool:
    """Quickly check whether the given variant has at least one recording
    overlapping the requested time range for the given camera."""
    return (
        Recordings.select(Recordings.id)
        .where(
            Recordings.camera == camera_name,
            Recordings.variant == variant,
            recordings_overlap_clause(start_ts, end_ts),
        )
        .exists()
    )


def resolve_playback_variant(
    camera_name: str, variant: str, start_ts: float, end_ts: float
) -> str:
    """Return the variant to actually serve. If the requested variant has no
    overlapping recording in the time range, fall back to the other variant.
    'all' is returned as-is (no fallback)."""
    if variant == RECORDING_VARIANT_ALL:
        return variant
    fallback = OTHER_VARIANT.get(variant)
    if fallback is None:
        return variant
    if variant_has_overlapping_recording(camera_name, variant, start_ts, end_ts):
        return variant
    if variant_has_overlapping_recording(camera_name, fallback, start_ts, end_ts):
        logger.debug(
            "Falling back to variant '%s' for %s because no '%s' recordings exist between %s and %s",
            fallback,
            camera_name,
            variant,
            start_ts,
            end_ts,
        )
        return fallback
    return variant
