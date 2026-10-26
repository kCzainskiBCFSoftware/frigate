# database-maintenance Specification

## Purpose
Keep database maintenance from blocking the saving of recordings, and keep the recordings cleanup run cheap on a database with millions of segment rows. Upstream Frigate ends each cleanup run with a checkpoint that blocks writers. It walks the whole recordings tree every run, and reads each camera's whole retained history to find what to expire.

## Requirements

### Requirement: WAL maintenance does not block writers

With `database.wal_checkpoint: passive`, which is the default, the checkpoint after each recordings cleanup run SHALL be a passive checkpoint, which blocks neither readers nor writers. A blocking `TRUNCATE` checkpoint SHALL be attempted only when the WAL file is still larger than four times `database.wal_size_limit` after the passive checkpoint. That attempt SHALL give up after waiting 2 seconds for other connections. The value `truncate` SHALL restore upstream behavior.

#### Scenario: End of a cleanup run
- **WHEN** a cleanup run ends with an 80 MB WAL file and `wal_size_limit: 64`
- **THEN** segment row writes are not blocked by the checkpoint for more than 2 seconds
- **AND** no "Unable to keep up" warning follows the run

#### Scenario: Oversized WAL
- **WHEN** the WAL file is still 300 MB after the passive checkpoint, with `wal_size_limit: 64`
- **THEN** a `TRUNCATE` checkpoint is attempted
- **AND** if other connections keep it waiting for 2 seconds, it gives up and logs a warning with the WAL size

### Requirement: The WAL file size is bounded between runs

Every database connection opened by the main and recording processes SHALL set `journal_size_limit` to `database.wal_size_limit` megabytes, with a default of 64. SQLite then cuts the WAL file back to that size each time it restarts the WAL.

#### Scenario: Normal recording between runs
- **WHEN** a device records normally between two cleanup runs, and no checkpoint is held back by a long-running reader
- **THEN** the WAL file is no larger than `wal_size_limit` when the next run starts

### Requirement: Each cleanup run reports its cost

After each recordings cleanup run, Frigate SHALL log one info line with:

- the duration of each phase (expiry, empty directories, checkpoint, statistics and timeline ranges);
- the WAL file size before and after the checkpoint;
- the result of the checkpoint.

#### Scenario: Run summary
- **WHEN** a cleanup run finishes
- **THEN** one info line reports each phase's duration, the WAL size before and after the checkpoint, and the checkpoint result

### Requirement: Empty-directory removal is limited to what the run deleted

With `record.pipeline.empty_directory_cleanup: scoped`, which is the default, a cleanup run SHALL remove only the directories it deleted a file from, and their parents up to the recordings root, and only once they are empty. It SHALL NOT walk the whole recordings tree. The value `full` SHALL restore the upstream whole-tree walk.

#### Scenario: An hour of footage expires
- **WHEN** a run deletes the last segments in `recordings/2026-09-23/04/cam1/main`
- **THEN** that directory is removed, along with `cam1` and `04` if they become empty
- **AND** no other directory under `recordings/` is listed

#### Scenario: Directory emptied outside cleanup
- **WHEN** a directory under `recordings/` was emptied by hand
- **THEN** a scoped run leaves it in place, and a `full` run removes it

### Requirement: Retention reads only rows that can expire

The retention queries of a cleanup run SHALL be bounded on segment start time, so that the database reads only rows that can expire in that run, not each camera's whole retained history. Their results SHALL be the same as without the bound. This covers the queries for removed cameras, for configured variants and for unconfigured variants.

#### Scenario: Query plans
- **WHEN** the retention queries are planned on a database with the fork's indexes
- **THEN** none of them scans the recordings table or a camera's entire index range, and each uses a range search on `start_time`

#### Scenario: Same rows expire
- **WHEN** cleanup runs on a fixture that has expired, kept, motion-bearing, null-statistics and review-overlapping rows for two variants and a removed camera
- **THEN** exactly the same rows and files are deleted as without the bound

### Requirement: The database page-cache size is configurable

`database.cache_size` SHALL set the SQLite page-cache ceiling, in megabytes, for each connection that the main and recording processes open. The default SHALL be 256, the fork's current value.

#### Scenario: Smaller cache on a low-memory device
- **WHEN** a device sets `database.cache_size: 96`
- **THEN** every main-process and recording-process connection uses a page-cache ceiling of 96 MB
