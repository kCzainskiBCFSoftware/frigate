# Proposal: keep recordings flowing when the segment mover stalls

## Why

On a 32-camera dual-stream device (4 cores, 7.8 GB RAM with ~3.8 GB swapped
out, a 5.2 GB `frigate.db` on the NVMe that also holds swap), every camera
periodically logs "Unable to keep up with recording segments in cache" in the
same second and loses 10–60 s of footage per stream. In the worst runs the
camera watchdog then restarts every healthy recorder, which adds another gap.

The ffmpeg recorders stay healthy the whole time. What stops is the segment
mover (`RecordingMaintainer`). Each mover pass depends on parts of Frigate that
are not on the recording path:

1. **Frame processing.** A continuous-retention segment moves only once the
   camera's frame processing has passed the segment's end.
2. **The database.** The pass ends with a blocking ZMQ request that waits until
   the `Recordings` insert commits in the main process. That insert queues
   behind every other main-process write, including the closing
   `wal_checkpoint(TRUNCATE)` of each recordings cleanup run.
3. **The watchdog.** The camera watchdog reads the mover's progress messages as
   recorder health. A stalled mover looks like a dead recorder.

Any stall longer than about a minute then costs footage, because the pass that
would clear the backlog first discards everything beyond a fixed segment count.

Field traces tie the stalls to the end of each cleanup run (every ~94 min), to
the hourly preview build while memory is swapped out, and, in the worst case,
to the cleanup's database phase overlapping the preview build. All three
couplings exist in upstream Frigate v0.17.x as well.

Reviewing the code for this proposal found three more problems on the same
path. They come from reading the code and have not been reproduced on a device
yet. The design covers them in detail.

- A continuous-retention segment that overlaps an alert or detection still in
  progress can be **deleted without a log line** while frame processing lags.
- After a stall, one mover pass starts one ffmpeg copy per waiting segment, all
  at once. The 06:00 backlog cleared about 250 segments in 5 s, which means up
  to ~250 copies ran together.
- The cleanup run reads its expiry candidates through queries that are not
  index-seekable on time. One of them scans the whole recordings table every
  run.

## What Changes

Each item has a config setting. Its default fixes the problem, and a device can
switch it back to upstream behavior without a rebuild.

- **Continuous segments stop waiting for frame processing.** If processing has
  not reached a segment's end `processing_timeout` seconds (default 30) after
  the segment closed, the segment moves anyway. Its activity stats (`motion`,
  `objects`, `regions`, `dBFS`) are stored as unknown (`NULL`), and retention
  keeps such a segment for the camera's outer window rather than expiring it
  early. Motion-only and review-based retention keep waiting, as today.
  **BREAKING** for strict external clients: `motion` and `objects` can now be
  `null` in recording listings and hourly summaries. A new fork feature flag
  advertises this.
- **Discard decisions use complete stats only.** A segment is no longer
  discarded for "no activity" while frames for its time span are still
  unprocessed. This fixes the silent deletion described above.
- **Database writes no longer hold up the mover.** Segment rows go to a
  bounded background writer in the recording process. Writes are acknowledged,
  retried idempotently and flushed on shutdown, and rows are never dropped
  silently. A database error can no longer kill the main process's
  inter-process request thread. After an unclean shutdown, recent segment files
  that have no row are found and given one.
- **The watchdog judges recorders by their own output.** Each recorder
  (camera + variant) is checked against its files in the cache, so a stalled
  mover no longer restarts healthy recorders. Validity checks apply only while
  the mover is keeping up. The 120 s threshold becomes configurable. A stalled
  mover is reported as a mover problem.
- **WAL maintenance never blocks writers.** The closing checkpoint becomes
  `PASSIVE`, and the WAL file is bounded with `journal_size_limit`. A blocking
  `TRUNCATE` with a short timeout remains only as a fallback for an oversized
  WAL. Each run logs its phase timings and checkpoint result.
- **Mover copies are capped** at `move_concurrency` (default 8) in-flight
  probes and copies. The handover listed this as lower priority. It is required
  here because the other changes keep backlogs instead of dropping them, and
  one pass over an uncapped backlog can spawn hundreds of ffmpeg processes on a
  box with ~2 GB free.
- **The cleanup run gets shorter.** Empty-directory removal visits only the
  directories the run deleted from, instead of walking ~10 M files. Expiry
  queries get index-seekable time bounds.
- **The cache backlog limit is configurable** per recorder. Excess segments are
  discarded only after the pass has tried to move them, and a free-space
  reserve protects `/tmp/cache` from filling.
- **The SQLite page-cache size is configurable.** The default stays at the
  fork's current 256 MB.

Unchanged: preview generation and quality, `record_variant` naming, and where
preview frames are stored (a deployment bind mount handles that).

## Capabilities

### New Capabilities

- `recording-segment-mover`: when and how cached segments are moved into recordings: waiting for frame processing, unknown stats, discard rules, move concurrency and the cache backlog limit.
- `recording-db-writes`: how segment rows reach the database without blocking the mover: queueing, acknowledgement, retries, shutdown flush, recovery after an unclean shutdown, and the "recordings saved" signal.
- `recorder-watchdog`: how a recorder's health is judged and when it is restarted.
- `database-maintenance`: WAL checkpointing and size bounds, the cost of the recordings cleanup run, and database memory settings.

### Modified Capabilities

- `playback-timeline-api`: a segment row written long after its segment (after a database stall or recovery) updates the precomputed timeline.
- `fork-capabilities`: adds the `recordings_unknown_stats` feature flag.

## Impact

- **Upstream files touched, by expected rebase conflict:**
  - High: `frigate/record/maintainer.py` and `frigate/record/cleanup.py`, which
    the fork already changes heavily, and `frigate/video.py` (`CameraWatchdog`),
    which upstream edits often.
  - Low to medium: `frigate/comms/dispatcher.py` (one handler),
    `frigate/record/util.py` (`remove_empty_directories`) and
    `frigate/record/record.py`.
  - Low: `frigate/app.py` (pragmas, startup marker, shutdown order),
    `frigate/config/camera/record.py`, `frigate/config/database.py`,
    `frigate/config/camera/camera.py` (adds the variant to `ffmpeg_cmds`
    entries) and `frigate/const.py`. These changes are additive.
- **New fork-only modules** keep most of the logic out of upstream files:
  `frigate/record/writer.py`, `frigate/record/reconcile.py`,
  `frigate/record/cache_watch.py` and `frigate/db/maintenance.py`, plus a new
  config model for the `record.pipeline` block.
- **Database:** no migration. No existing rows are rewritten or backfilled.
  `motion`, `objects`, `regions` and `dBFS` have been nullable since migrations
  006, 018 and 023. Only new rows can carry `NULL`.
- **API:** **BREAKING** for external clients that treat `motion` or `objects`
  as non-null, in `GET /api/{camera}/recordings` rows and in the per-hour
  totals of `GET /api/{camera}/recordings/summary`. Endpoints and parameters do
  not change.
- **Cost on the 32-camera, 4-core, 8 GB device:** a net reduction. Added
  steady-state RAM is about 12 MB at most (the row queue). The copy cap removes
  burst memory of an estimated 1–2.5 GB. The watchdog's cache checks cost well
  under 1% of one core. The cleanup run stops walking ~10 M directory entries on
  the HDDs and stops scanning roughly 10 M index entries on the NVMe every run.
  The design has the full table.
- **Upstream:** each part is self-contained, so each can be offered upstream as
  its own PR.
