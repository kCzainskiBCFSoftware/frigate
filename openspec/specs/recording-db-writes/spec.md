# recording-db-writes Specification

## Purpose
Get each recorded segment's row into the database without making the segment mover wait, and without losing rows. Upstream Frigate ends every mover pass with a request that blocks until the insert commits. A database error during that insert can end the main process's inter-process request thread.

## Requirements

### Requirement: Moving segments does not wait for the database

With `record.pipeline.async_db_writes` enabled, which is the default, the mover SHALL hand each segment's row to a background writer and SHALL start its next pass without waiting for the database. With it disabled, the mover SHALL wait for each pass's insert, as upstream does.

#### Scenario: Database locked for 90 seconds
- **WHEN** another process holds a write lock on `frigate.db` for 90 seconds while 32 dual-stream cameras record
- **THEN** segments keep moving from the cache into recordings on every pass, and no segment is discarded
- **AND** every segment's row is in the database within one minute after the lock is released

### Requirement: Queued rows are bounded and never dropped silently

The writer SHALL hold at most `record.pipeline.db_write_queue` rows. The default SHALL be 8192. When the queue is full, the mover SHALL wait for room and log an error. It SHALL NOT drop rows. A failed write SHALL be retried with backoff until it succeeds. The writer SHALL log, each time with the queue depth: a warning on the first failure, an error once writes have been failing for 60 seconds, and an info message when writes succeed again.

#### Scenario: Long outage fills the queue
- **WHEN** the database refuses writes long enough for the queue to fill
- **THEN** the mover logs an error and stops taking new rows until the queue has room
- **AND** no queued row is discarded

#### Scenario: Recovery is logged
- **WHEN** writes have failed for 2 minutes and then succeed
- **THEN** one warning, one error and one info message are logged for that outage, each with the queue depth

### Requirement: Row writes are acknowledged and idempotent

The main process SHALL acknowledge each batch of rows with its outcome. It SHALL return a database error as a failed outcome. A database error SHALL NOT stop the main process from answering other processes' requests. Writing the same batch twice SHALL store each segment once.

#### Scenario: Insert fails while the database is locked
- **WHEN** an insert fails with "database is locked"
- **THEN** the writer receives a failed outcome and retries the batch
- **AND** requests from other processes, such as camera status updates, are still answered afterwards

#### Scenario: Batch sent twice
- **WHEN** a batch that the database already stored is sent again
- **THEN** no duplicate row is created and the outcome is success

### Requirement: Rows are flushed on shutdown

On a normal shutdown, Frigate SHALL write every queued row before the recording process exits. It SHALL wait up to `record.pipeline.db_flush_timeout` seconds for this, with a default of 10. If rows cannot be written in that time, Frigate SHALL log an error with their count and the paths of the oldest and newest segment, and SHALL treat the shutdown as unclean.

#### Scenario: Restart from the UI with rows queued
- **WHEN** Frigate is restarted while 200 rows are queued and the database is writable
- **THEN** all 200 rows are in the database before the recording process exits

### Requirement: Segments without rows are recovered after an unclean shutdown

When the previous run did not shut down cleanly, and `record.pipeline.reconcile_on_unclean_shutdown` is enabled (the default), Frigate SHALL add a row for each segment file that has none. It SHALL look at the last `record.pipeline.reconcile_window` hours, with a default of 2, and do this at startup, before any recordings sync. After a clean shutdown, no recovery scan SHALL run.

#### Scenario: Recording process killed with rows queued
- **WHEN** the recording process is killed while 300 rows are queued, and Frigate then starts again
- **THEN** those 300 segments are listed in recordings, and they appear on the playback timeline

#### Scenario: Clean restart
- **WHEN** Frigate restarts after a clean shutdown
- **THEN** no recovery scan runs

### Requirement: Recovered rows carry probed metadata and unknown statistics

Each row added by recovery SHALL have the file's probed duration and stream metadata, the variant taken from the file's directory, and null activity statistics. A file that cannot be probed SHALL be logged and left in place, without a row.

#### Scenario: Recovered sub segment
- **WHEN** recovery finds `recordings/2026-09-23/04/cam1/sub/22.34.mp4` without a row
- **THEN** it adds a `sub` row for `cam1` with the probed duration, codec and size, and null `motion`, `objects`, `regions` and `dBFS`

#### Scenario: Truncated file
- **WHEN** recovery finds a file that cannot be probed
- **THEN** it logs the path, adds no row and leaves the file in place

### Requirement: The "recordings saved" signal never runs ahead of the database

The `recordings/saved` update SHALL report, per camera, a time before which every moved segment of that camera has its row written.

#### Scenario: Rows queued during a stall
- **WHEN** a camera's rows are queued because the database is locked
- **THEN** the saved time for that camera does not advance past the start of its oldest queued row until that row is written
