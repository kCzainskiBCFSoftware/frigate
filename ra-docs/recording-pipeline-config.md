# Recording pipeline and database settings (fork)

Config reference for the settings added by the recording-path resilience
change (OpenSpec `recording-stall-resilience`). Every setting can put one part
of the recording path back to upstream Frigate behavior without a rebuild; the
"Upstream" column gives that value. The defaults fix the stalls; none of the
settings needs changing on a typical device.

Code-level changelog: [fork-changes.md](fork-changes.md), section
*Recording-path resilience*. Behavior contract: `openspec/specs/`
(`recording-segment-mover`, `recording-db-writes`, `recorder-watchdog`,
`database-maintenance`).

## `record.pipeline`

All of these are **global only**: set them under the top-level `record:` key.
A value set under a camera's `record:` is validated but ignored, as for
`record.expire_interval`. The one exception is `recorder_stale_timeout`, which a
camera may override.

| Setting | Default | Upstream | What it does |
|---|---|---|---|
| `processing_timeout` | `30` (s) | `0` | A continuous-retention segment waits at most this long after it closed for the camera's frame processing; then it is saved with unknown (`NULL`) activity stats and a rate-limited warning names the camera. `0` waits indefinitely. Cameras without continuous retention always wait. |
| `move_concurrency` | `8` | `0` | Most segment probes and `ffmpeg -c copy` moves the mover runs at once, across all cameras. `0` is unbounded. |
| `max_cached_segments` | `6` | `6` | Closed segments kept in the cache **per camera and variant** before the oldest are discarded with the existing "Unable to keep up" / "Too many unprocessed" warnings. Applied after each pass's moves, so a backlog the pass can move is never discarded. Raising it costs tmpfs (RAM): ~0.4 GB at 10 per variant on 32 dual-stream cameras. |
| `cache_free_reserve` | `10` (%) | `0` | At the start and end of every pass, if the cache filesystem has less than this much free, the oldest closed segments across all cameras are discarded until it does, with an error naming the cameras. `0` turns it off. |
| `async_db_writes` | `true` | `false` | Segment rows go to a bounded writer thread in the recording process instead of a request that blocks the mover until they commit. `false` keeps the blocking request (with the hardened handler). |
| `db_write_queue` | `8192` (rows) | — | Most rows the writer holds (~21 minutes at 32 dual-stream cameras, ≤ ~12 MB). When full, the mover waits and logs an error; rows are never dropped. |
| `db_flush_timeout` | `10` (s) | — | How long the writer drains on shutdown. The main process waits this + 5 s for the recording process before terminating it. |
| `reconcile_on_unclean_shutdown` | `true` | `false` | After an unclean shutdown (no clean-shutdown marker), segment files of the last `reconcile_window` hours that have no row get one, before `sync_recordings` runs. |
| `reconcile_window` | `2` (h) | — | How far back that recovery scan looks. |
| `recorder_watchdog` | `cache` | `mover` | `cache` judges each recorder (camera + variant) by its own files in the cache and restarts only the stale one. `mover` keeps the upstream per-camera signals from the mover's messages. |
| `recorder_stale_timeout` | `120` (s) | `120` | Seconds without new output before a recorder is restarted; used by both watchdog modes and stated in the restart message. **Camera override allowed.** The startup grace after a recorder starts, is restarted or the camera is enabled is `min(90, timeout)`. |
| `empty_directory_cleanup` | `scoped` | `full` | `scoped` removes only the directories a cleanup run deleted files from (and their parents up to `recordings/`, once empty). `full` walks the whole recordings tree every run. Directories emptied by hand stay until a `full` run; they are harmless. |

Validation: `processing_timeout + 10 < max_cached_segments × 10`, because a
segment waiting for processing still counts against the backlog limit. Counts
and durations must not be negative; `db_write_queue`, `db_flush_timeout`,
`reconcile_window` and `recorder_stale_timeout` must be positive.

## `database`

| Setting | Default | Upstream | What it does |
|---|---|---|---|
| `wal_checkpoint` | `passive` | `truncate` | The checkpoint that ends each recordings cleanup run. `passive` never blocks writers; a `TRUNCATE` with a 2 s busy timeout is tried only if the WAL is still larger than 4 × `wal_size_limit`, and gives up with a warning. `truncate` restores the upstream blocking `TRUNCATE` whenever the WAL is over 10 MB. |
| `wal_size_limit` | `64` (MB) | none | `journal_size_limit` on every main- and recording-process connection: SQLite cuts the WAL file back to this size each time it restarts it. |
| `cache_size` | `256` (MB) | `500` (fork before: `256`) | SQLite page-cache ceiling per connection in the main and recording processes. Each API worker thread has its own connection. Lower values save RAM per busy connection at the cost of more NVMe reads; measure API latency before lowering it. |

## Example: everything back to upstream

```yaml
record:
  pipeline:
    processing_timeout: 0
    move_concurrency: 0
    cache_free_reserve: 0
    async_db_writes: false
    reconcile_on_unclean_shutdown: false
    recorder_watchdog: mover
    empty_directory_cleanup: full
database:
  wal_checkpoint: truncate
```

`max_cached_segments: 6` and `recorder_stale_timeout: 120` already equal the
upstream values. `cache_size` stays at the fork's 256 MB unless set.

## Deployment notes

- **Stop grace period.** Give the container at least 30 s
  (`stop_grace_period: 30s` in compose; Docker's default is 10 s). Otherwise
  Docker kills Frigate while the writer drains, the clean-shutdown marker is not
  written, and the next start runs the recovery scan (cheap, but avoidable).
- **First start after the upgrade** has no marker yet, so it runs the recovery
  scan once. It normally finds nothing.
- **What to watch in the log:** one `Recordings cleanup run took …` info line
  per cleanup run (phase timings, WAL size before and after, checkpoint result);
  `Frame processing for <camera> is Ns behind` (segments saved without stats);
  `The recording mover has reported no progress …` (stalled mover, no restarts);
  `Writing recording rows failed …` / `… have not been written for …` /
  `… being written again …` (database outage and recovery, with queue depth).
