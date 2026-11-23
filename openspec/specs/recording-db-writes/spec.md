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

The writer SHALL hold `record.pipeline.db_write_queue` rows, plus at most the rows of the one mover pass that filled it. The default SHALL be 8192. While the queue is full, the mover SHALL move no segments and SHALL log an error once per outage. It SHALL NOT block on the writer: it SHALL keep applying the cache backlog limit and free-space reserve to the segments it holds, without a warning per discard, and SHALL stop promptly when Frigate stops. While holding, it SHALL NOT publish its `latest` heartbeat, as a blocked mover would not. The writer SHALL NOT drop rows, except a row the database rejects as described in "A rejected row does not stop the writer". Any other failed write SHALL be retried with backoff until it succeeds. The writer SHALL log, each time with the queue depth: a warning on the first failure, an error once writes have been failing for 60 seconds, and an info message when writes succeed again.

#### Scenario: Long outage fills the queue
- **WHEN** the database refuses writes long enough for the queue to fill
- **THEN** the mover logs one error and moves no segments until the queue has room
- **AND** no queued row is discarded
- **AND** each recorder keeps its newest `max_cached_segments` segments in the cache, which move once the queue has room

#### Scenario: Stop while the queue is full
- **WHEN** Frigate stops while the queue is full
- **THEN** the mover exits without waiting for room, and the writer drains for up to `db_flush_timeout`

#### Scenario: Recovery is logged
- **WHEN** writes have failed for 2 minutes and then succeed
- **THEN** one warning, one error and one info message are logged for that outage, each with the queue depth

### Requirement: The writer keeps running

An exception while the writer sends a batch SHALL be retried like a failed write. A send that gets no reply SHALL be retried on a new connection to the main process. An exception while it publishes `recordings/saved` SHALL be logged, and SHALL NOT stop it. If the writer thread stops anyway, the mover SHALL log an error once and write each pass's rows with the blocking request, together with the rows the writer still held. It SHALL NOT wait for room in a stopped writer's queue.

#### Scenario: Send raises
- **WHEN** sending a batch raises an exception twice and then succeeds
- **THEN** the batch is written, and the writer keeps taking rows

#### Scenario: No reply
- **WHEN** a send gets no reply from the main process
- **THEN** the writer retries the batch on a new connection

#### Scenario: Writer thread gone
- **WHEN** the writer thread has stopped while holding queued rows
- **THEN** the next pass writes those rows and its own with the blocking request, and an error is logged once

### Requirement: Row writes are acknowledged and idempotent

The main process SHALL acknowledge each batch of rows with its outcome. When the database rejects some rows' values, it SHALL store the other rows and return a successful outcome that lists each rejected row with its path and error. It SHALL return any other database error as a failed outcome. A database error SHALL NOT stop the main process from answering other processes' requests. Writing the same batch twice SHALL store each segment once.

#### Scenario: Insert fails while the database is locked
- **WHEN** an insert fails with "database is locked"
- **THEN** the writer receives a failed outcome and retries the batch
- **AND** requests from other processes, such as camera status updates, are still answered afterwards

#### Scenario: Insert rejected by a constraint
- **WHEN** the database rejects one row's values in a batch, for example through a NOT NULL or CHECK constraint or a trigger that aborts the insert
- **THEN** the batch's other rows are stored
- **AND** the outcome is success, and lists the rejected row with its path and the database error

#### Scenario: Batch sent twice
- **WHEN** a batch that the database already stored is sent again
- **THEN** no duplicate row is created and the outcome is success

### Requirement: Rows are flushed on shutdown

On a normal shutdown, Frigate SHALL write every queued row before the recording process exits. The mover SHALL stop as soon as Frigate stops, without finishing its wait between passes, so the drain starts at once. Frigate SHALL wait up to `record.pipeline.db_flush_timeout` seconds for this, with a default of 10. If rows cannot be written in that time, Frigate SHALL log an error with their count and the paths of the oldest and newest segment, and SHALL treat the shutdown as unclean.

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

### Requirement: Recordings sync does not delete files whose rows are pending

A segment's file is moved into recordings before its row is written. The recordings sync (`record.sync_recordings`) SHALL NOT delete a file that has no row if the file was moved less than 2 hours ago.

#### Scenario: Sync during a database stall
- **WHEN** the recordings sync runs while segments moved in the last 20 minutes still have their rows queued
- **THEN** those files are not deleted, and they are listed once their rows are written

### Requirement: Recovered rows carry probed metadata and unknown statistics

Each row added by recovery SHALL have the file's probed duration and stream metadata, the variant taken from the file's directory, and null activity statistics. A file that cannot be probed SHALL be logged and left in place, without a row. Recovery SHALL probe files one at a time and insert their rows in batches of at most 500.

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

### Requirement: Segment-row requests time out

A segment-row request that gets no reply within the main process's database busy timeout plus 30 seconds SHALL stop waiting, and the next request from the same sender SHALL go over a new connection to the main process. The writer SHALL then retry the batch, as "The writer keeps running" requires. The mover's blocking request SHALL keep the rows instead, as "The blocking request keeps the rows it could not write" requires.

#### Scenario: No reply within the timeout
- **WHEN** a send gets no reply from the main process within the busy timeout plus 30 seconds
- **THEN** the writer retries the batch on a new connection
- **AND** once the main process answers again, each segment of that batch is stored once

#### Scenario: Blocking request gets no reply
- **WHEN** the mover writes rows with its blocking request and gets no reply within the busy timeout plus 30 seconds
- **THEN** the mover starts its next pass without waiting longer, and that pass sends those rows again

### Requirement: The blocking request keeps the rows it could not write

The mover uses a blocking request when `record.pipeline.async_db_writes` is off or the writer thread has stopped. It SHALL keep every row of a batch that gets no reply or a failed outcome, and every later row of that pass, and SHALL send the kept rows first on its next pass. That batch SHALL end the pass's requests, so a main process that does not answer delays a pass by at most one request timeout. A pass with no rows SHALL send no request.

#### Scenario: Main process stops answering, then recovers
- **WHEN** the writer thread has stopped, the main process answers no segment-row request for three passes, and then answers again
- **THEN** each of those passes sends one request and keeps its rows
- **AND** the next pass stores every row of the four passes

#### Scenario: Backlog of several batches
- **WHEN** a pass has 1200 rows to write and the main process does not answer
- **THEN** the pass sends one request, not three, and keeps all 1200 rows for the next pass

### Requirement: Rows the blocking request keeps are bounded and logged

The mover SHALL keep at most `record.pipeline.db_write_queue` rows. Past that, it SHALL drop the oldest and log one error per outage that names their paths, and their segment files SHALL stay in recordings. It SHALL log a warning when writes start failing, an error once they have failed for 60 seconds, and an info message when they succeed again.

#### Scenario: More rows than the queue holds
- **WHEN** the main process answers no segment-row request until the mover keeps more than `db_write_queue` rows
- **THEN** the oldest rows are dropped, one error names their paths, and their files stay in recordings

#### Scenario: Outage is logged
- **WHEN** the blocking request fails for 2 minutes and then succeeds
- **THEN** one warning, one error and one info message are logged for that outage

### Requirement: Rows the blocking request still keeps at shutdown are recovered

When Frigate stops while the mover still keeps rows it could not write, the mover SHALL log their count and the paths of the oldest and newest, and the shutdown SHALL count as unclean.

#### Scenario: Stop with rows kept
- **WHEN** Frigate stops while the mover still keeps rows it could not write
- **THEN** an error names how many rows were not written, and the shutdown is unclean
- **AND** the next start adds rows for their segment files, as "Segments without rows are recovered after an unclean shutdown" requires

### Requirement: A rejected row does not stop the writer

When the database rejects a row's values, the main process SHALL find that row within the same request and store the batch's other rows. The writer and the mover SHALL drop each rejected row, and its segment file SHALL stay in recordings. Any other failure SHALL be retried whole, with backoff.

#### Scenario: One bad row in a full batch
- **WHEN** the database rejects one row of a 500-row batch
- **THEN** the other 499 rows are stored by that one request
- **AND** one error names the rejected row's path and the database error
- **AND** the mover keeps moving segments, and the queue does not fill

#### Scenario: Locked database is not split
- **WHEN** a batch fails because the database is locked
- **THEN** the writer retries the whole batch with backoff and drops no row

### Requirement: Rejected rows are logged at most every 5 minutes

Rejected rows SHALL be logged as an error that names the first rejected row's path and the database error, at most once every 5 minutes. Each such error SHALL include the number of rows rejected since the previous one.

#### Scenario: Every row is rejected
- **WHEN** a constraint rejects every segment row for 9 minutes
- **THEN** two errors about rejected rows are logged in that time, the second with the number of rows rejected since the first

### Requirement: Segment row ids do not collide

Each segment row's id SHALL be the segment's start time followed by `-` and 12 random lowercase letters or digits, and SHALL be at most 30 characters long. Rows written before this change SHALL keep their ids.

#### Scenario: Every stream starts a segment in the same second
- **WHEN** 32 dual-stream cameras each move a `main` and a `sub` segment that start in the same second
- **THEN** all 64 rows are stored, each with a distinct id of at most 30 characters
