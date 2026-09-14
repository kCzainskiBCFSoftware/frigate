"""Run recording maintainer and cleanup."""

import logging
from multiprocessing.synchronize import Event as MpEvent

from playhouse.sqliteq import SqliteQueueDatabase

from frigate.config import FrigateConfig
from frigate.const import PROCESS_PRIORITY_HIGH
from frigate.models import Recordings, ReviewSegment
from frigate.record.maintainer import RecordingMaintainer
from frigate.util.process import FrigateProcess

logger = logging.getLogger(__name__)


class RecordProcess(FrigateProcess):
    def __init__(self, config: FrigateConfig, stop_event: MpEvent) -> None:
        super().__init__(
            stop_event,
            PROCESS_PRIORITY_HIGH,
            name="frigate.recording_manager",
            daemon=True,
        )
        self.config = config

    def run(self) -> None:
        self.pre_run_setup(self.config.logger)
        db = SqliteQueueDatabase(
            self.config.database.path,
            pragmas={
                "auto_vacuum": "FULL",  # Does not defragment database
                # 256MB page-cache ceiling (was 512MB). This process is
                # write-heavy -- one row per camera per variant every segment_time --
                # so it benefits least of the three from a large read cache.
                "cache_size": -256 * 1000,
                # Read pages through an mmap of the db instead of copying them into
                # this connection's private cache: the mapping is file-backed, so the
                # kernel can reclaim it under memory pressure (SQLite's own page cache
                # cannot be), and one mapping is shared by every connection reading the
                # file rather than duplicated per connection.
                "mmap_size": 256 * 1024 * 1024,
                "journal_mode": "wal",  # required for synchronous=NORMAL to be crash-safe
                "synchronous": "NORMAL",  # Safe when using WAL https://www.sqlite.org/pragma.html#pragma_synchronous
            },
            timeout=max(
                60, 10 * len([c for c in self.config.cameras.values() if c.enabled])
            ),
        )
        models = [ReviewSegment, Recordings]
        db.bind(models)

        maintainer = RecordingMaintainer(
            self.config,
            self.stop_event,
        )
        maintainer.start()
