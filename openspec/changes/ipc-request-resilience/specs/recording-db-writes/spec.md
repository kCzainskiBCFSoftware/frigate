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

The main process SHALL acknowledge each batch of rows with its outcome. It SHALL return a database error as a failed outcome, which SHALL say whether the database rejected the rows' values, as a constraint or datatype failure does. A database error SHALL NOT stop the main process from answering other processes' requests. Writing the same batch twice SHALL store each segment once.

#### Scenario: Insert fails while the database is locked
- **WHEN** an insert fails with "database is locked"
- **THEN** the writer receives a failed outcome that is not marked rejected, and retries the batch
- **AND** requests from other processes, such as camera status updates, are still answered afterwards

#### Scenario: Insert rejected by a constraint
- **WHEN** an insert fails because the database rejects a row's values, for example a NOT NULL or CHECK constraint or a trigger that aborts the insert
- **THEN** the writer receives a failed outcome marked rejected

#### Scenario: Batch sent twice
- **WHEN** a batch that the database already stored is sent again
- **THEN** no duplicate row is created and the outcome is success

## ADDED Requirements

### Requirement: Segment-row requests time out

A segment-row request that gets no reply within the main process's database busy timeout plus 30 seconds SHALL stop waiting. The writer SHALL then retry the batch on a new connection to the main process, as "The writer keeps running" requires. The mover's blocking request, used when the writer thread has stopped, SHALL instead log an error with the number of rows it could not write, and start its next pass.

#### Scenario: No reply within the timeout
- **WHEN** a send gets no reply from the main process within the busy timeout plus 30 seconds
- **THEN** the writer retries the batch on a new connection
- **AND** once the main process answers again, each segment of that batch is stored once

#### Scenario: Blocking request gets no reply
- **WHEN** the writer thread has stopped and the mover's blocking request gets no reply within the busy timeout plus 30 seconds
- **THEN** the mover logs an error with the number of rows not written and starts its next pass

### Requirement: A rejected row does not stop the writer

When the database rejects a batch's values, the writer SHALL split the batch until each rejected row is alone, and SHALL write every other row of the batch. It SHALL log each rejected row as an error with its path and the database error, then drop it. The row's segment file SHALL stay in recordings. Any other failure SHALL be retried whole, with backoff.

#### Scenario: One bad row in a full batch
- **WHEN** the database rejects one row of a 500-row batch
- **THEN** the other 499 rows are stored
- **AND** one error names the rejected row's path and the database error
- **AND** the mover keeps moving segments, and the queue does not fill

#### Scenario: Locked database is not split
- **WHEN** a batch fails because the database is locked
- **THEN** the writer retries the whole batch with backoff and drops no row

### Requirement: Segment row ids do not collide

Each segment row's id SHALL be the segment's start time followed by `-` and 12 random lowercase letters or digits, and SHALL be at most 30 characters long. Rows written before this change SHALL keep their ids.

#### Scenario: Every stream starts a segment in the same second
- **WHEN** 32 dual-stream cameras each move a `main` and a `sub` segment that start in the same second
- **THEN** all 64 rows are stored, each with a distinct id of at most 30 characters
