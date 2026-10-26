"""Recording-pipeline settings (fork-only).

Every setting here can put one part of the recording path back to upstream
behavior without a rebuild. All of them are process-wide: the global value is
the one used and a camera-level value is ignored, as for
``record.expire_interval``. The one exception is ``recorder_stale_timeout``,
which a camera may override.
"""

from enum import Enum
from typing import TYPE_CHECKING, Self

from pydantic import Field, model_validator

from frigate.const import MAX_SEGMENTS_IN_CACHE

from ..base import FrigateBaseModel

if TYPE_CHECKING:
    from .record import RecordConfig

__all__ = [
    "EmptyDirectoryCleanupEnum",
    "RecordPipelineConfig",
    "RecorderWatchdogEnum",
    "apply_global_pipeline",
]

# Nominal recorder segment length the backlog window is measured in.
SEGMENT_SECONDS = 10


class RecorderWatchdogEnum(str, Enum):
    cache = "cache"
    mover = "mover"


class EmptyDirectoryCleanupEnum(str, Enum):
    scoped = "scoped"
    full = "full"


class RecordPipelineConfig(FrigateBaseModel):
    processing_timeout: int = Field(
        default=30,
        ge=0,
        title="Seconds a continuous segment waits for frame processing before it is moved with unknown activity stats (0 waits indefinitely, as upstream).",
    )
    move_concurrency: int = Field(
        default=8,
        ge=0,
        title="Most segment probes and copies the mover runs at once (0 is unbounded, as upstream).",
    )
    max_cached_segments: int = Field(
        default=MAX_SEGMENTS_IN_CACHE,
        ge=0,
        title="Closed segments kept in the cache per camera and variant before the oldest are discarded.",
    )
    cache_free_reserve: float = Field(
        default=10,
        ge=0,
        lt=100,
        title="Percent of the cache filesystem kept free by discarding the oldest closed segments (0 is off, as upstream).",
    )
    async_db_writes: bool = Field(
        default=True,
        title="Write segment rows from a background writer instead of blocking the mover (false is upstream).",
    )
    db_write_queue: int = Field(
        default=8192,
        ge=1,
        title="Most segment rows the background writer holds before the mover waits.",
    )
    db_flush_timeout: float = Field(
        default=10,
        gt=0,
        title="Seconds the background writer drains queued rows on shutdown.",
    )
    reconcile_on_unclean_shutdown: bool = Field(
        default=True,
        title="After an unclean shutdown, add rows for recent segment files that have none (false is upstream).",
    )
    reconcile_window: float = Field(
        default=2,
        gt=0,
        title="Hours of recordings the unclean-shutdown recovery scans.",
    )
    recorder_watchdog: RecorderWatchdogEnum = Field(
        default=RecorderWatchdogEnum.cache,
        title="Signal the camera watchdog judges recorders by: their cache output, or the mover's messages (upstream).",
    )
    recorder_stale_timeout: int = Field(
        default=120,
        gt=0,
        title="Seconds without new output before a recorder is restarted.",
    )
    empty_directory_cleanup: EmptyDirectoryCleanupEnum = Field(
        default=EmptyDirectoryCleanupEnum.scoped,
        title="Remove only the directories a cleanup run deleted from, or walk the whole recordings tree (full, upstream).",
    )

    @model_validator(mode="after")
    def validate_processing_window(self) -> Self:
        # a segment waiting for frame processing still counts against the
        # backlog limit, so the wait must end before the backlog discards it
        backlog_window = self.max_cached_segments * SEGMENT_SECONDS

        if self.processing_timeout + SEGMENT_SECONDS >= backlog_window:
            raise ValueError(
                f"record.pipeline.processing_timeout ({self.processing_timeout}s) "
                f"must be shorter than the cache backlog window: processing_timeout "
                f"+ {SEGMENT_SECONDS} must be less than max_cached_segments "
                f"({self.max_cached_segments}) x {SEGMENT_SECONDS}s"
            )

        return self


def apply_global_pipeline(
    camera_record: "RecordConfig", global_record: "RecordConfig"
) -> None:
    """Replace a camera's pipeline settings with the global ones.

    Only ``recorder_stale_timeout`` keeps the camera's own value.
    """
    camera_record.pipeline = global_record.pipeline.model_copy(
        update={"recorder_stale_timeout": camera_record.pipeline.recorder_stale_timeout}
    )
