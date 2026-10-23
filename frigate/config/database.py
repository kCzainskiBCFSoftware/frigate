from enum import Enum

from pydantic import Field

from frigate.const import DEFAULT_DB_PATH

from .base import FrigateBaseModel

__all__ = ["DatabaseConfig", "WalCheckpointEnum"]


class WalCheckpointEnum(str, Enum):
    passive = "passive"
    truncate = "truncate"


class DatabaseConfig(FrigateBaseModel):
    path: str = Field(default=DEFAULT_DB_PATH, title="Database path.")  # noqa: F821
    wal_checkpoint: WalCheckpointEnum = Field(
        default=WalCheckpointEnum.passive,
        title="Checkpoint after each recordings cleanup run: passive, which never blocks writers, or truncate (upstream).",
    )
    wal_size_limit: int = Field(
        default=64,
        ge=1,
        title="Size in MB the WAL file is cut back to each time it restarts.",
    )
    cache_size: int = Field(
        default=256,
        ge=1,
        title="SQLite page-cache ceiling in MB for each main and recording process connection.",
    )
