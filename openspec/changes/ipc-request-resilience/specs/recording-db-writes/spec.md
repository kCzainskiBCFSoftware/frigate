# Spec Delta

## MODIFIED Requirements

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

### Requirement: Row writes are acknowledged and idempotent

The main process SHALL acknowledge each batch of rows with its outcome. When the database rejects some rows' values, as a constraint, a trigger or a datatype failure does, the main process SHALL store the batch's other rows and SHALL return a successful outcome that lists each rejected row with its path and the database error. It SHALL return any other database error as a failed outcome. A database error SHALL NOT stop the main process from answering other processes' requests. Writing the same batch twice SHALL store each segment once.

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

## ADDED Requirements

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
