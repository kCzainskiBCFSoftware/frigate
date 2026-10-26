# Design

## Context

The motivation and the field evidence are in `proposal.md`. This document
checks the handover's claims against the code on `v17.1-update-with-adaptive`
(upstream v0.17.1 plus the fork), records what the review added, and sets out
how each part is built and what it costs on the target device.

The handover cites upstream v0.17.2 line numbers. The fork is based on
upstream v0.17.1, and the functions cited are the same in it. Note that the
fork's own tags `v0.17.2` and `v0.17.3` are fork releases, not upstream ones.

### Where the recording path runs

| Process | Thread | Role on the recording path |
|---|---|---|
| per-camera capture process | `CameraWatchdog` (`frigate/video.py`) | starts the ffmpeg recorders (one per record input, so one per variant) and restarts them when it judges them stale |
| ffmpeg recorder | – | writes 10 s segments to `/tmp/cache/{camera}@{variant}@{time}.mp4` (tmpfs) |
| per-camera `frigate.process` | – | publishes processed-frame info (motion boxes, objects, regions) |
| `frigate.recording_manager` | `RecordingMaintainer` (`frigate/record/maintainer.py`) | the mover: a pass every 5 s that probes, copies (`ffmpeg -c copy -movflags +faststart`) to `recordings/` on the HDDs and requests the row insert |
| main | `InterProcessCommunicator` REP thread (`frigate/comms/inter_process.py`) | serves every inter-process request in Frigate, one at a time, including `INSERT_MANY_RECORDINGS` |
| main | `SqliteQueueDatabase` writer thread (`frigate/app.py`, `bind_database`) | the main process's only writer; busy timeout `max(60, 10 × cameras)` = 320 s |
| main | `RecordingCleanup` (`frigate/record/cleanup.py`) | retention, empty-directory removal, the WAL checkpoint, `ANALYZE`, and the timeline-ranges rollup on a 60 s tick |

```
ffmpeg recorder ──writes──▶ /tmp/cache ──mover pass──▶ recordings/ (HDD)
                                │             │
frigate.process ──frame info────┘ (gate)      └─REQ/REP, no timeout──▶ main REP thread
                                                         │
                                                         ▼
RecordingCleanup ──deletes, TRUNCATE checkpoint──▶ SqliteQueueDatabase writer ──▶ frigate.db (NVMe, shared with swap)

mover ──latest / valid / invalid messages──▶ CameraWatchdog ──restart──▶ ffmpeg recorder
```

## Goals / Non-Goals

**Goals:**
- A healthy recorder's footage reaches `recordings/` while frame processing,
  the database or the main process is slow.
- A recorder is restarted only when that recorder is broken.
- WAL maintenance never blocks writers.
- No new steady-state RAM beyond a few MB on the 32-camera device, and lower
  peak RAM than today.
- Each behavior can be switched back to upstream behavior in config.

**Non-Goals:**
- Backfilling activity stats into rows that were moved without them. The
  reasons are under D1.
- Moving preview frames off the tmpfs, or turning previews off or down. This is
  deployment work and out of scope.
- Changing `record_variant` naming or the on-disk layout.
- Freeing the main REP thread for other inter-process clients during database
  stalls. See Open Questions.
- Fixing swap placement (swap shares the NVMe with the database). This is a
  deployment concern, noted under Risks.

## Validity review of the handover

Findings marked **Found in review** come from reading the code on this branch.
None has been reproduced on a device yet. The tests in `tasks.md` reproduce
each of them before fixing it.

### Change 1: continuous segments vs. frame processing

**Verdict: valid, but not sufficient on its own for the 06:00 event, and it
must ship with an extra fix.**

- The gate exists as described. In `validate_and_move_segment`, a segment
  moves with retention mode `all` only once
  `most_recently_processed_frame_time >= end_time`.
- *Correction.* At 06:00 the cache grew from 116 to 669 segments (~21 per
  camera), while the per-camera limit is 12 (`MAX_SEGMENTS_IN_CACHE = 6` per
  variant). That limit is applied inside a mover pass. If passes had been
  completing, the cache could not have reached ~21 per camera without
  "Too many unprocessed" drops. So passes were also held up, most likely in
  the insert request (change 2) or by the mover's own swap-ins. Change 1 is
  necessary for this event, but changes 2 and the copy cap are needed too.
- **Found in review: silent deletion.** For a continuous camera whose frames
  are not processed yet, `validate_and_move_segment` falls through to the
  review check. If an alert or detection is in progress, it calls
  `move_segment` with the review's retain mode (default `motion`).
  `segment_stats` then sees no frames for that time span, and
  `should_discard_segment` returns true, so the segment is unlinked with no
  log line. During a processing stall, any camera with an open review item
  loses footage this way. This affects motion-mode cameras as well. The fix
  (requirement "Activity-based discard uses complete statistics") is to never
  make an activity-based discard while the segment's frames are unprocessed.
- *Interplay with the cache limit.* Segments still waiting for processing
  count against "Too many unprocessed …". The wait must be shorter than the
  cache window, or segments are dropped before they time out:
  `processing_timeout + 10 s < max_cached_segments × 10 s`. With the defaults,
  30 + 10 < 60. Config validation enforces this.
- *Limitation: retention.* `NULL` stats are not expired by the
  continuous-only rule (`motion == 0 AND dBFS == 0`), and the Python
  `motion`/`active_objects` mode checks treat `None` as "keep". Such rows
  therefore live until the outer window. **On this device that costs no extra
  disk**: both variants set `retain_days`, which sets both the continuous and
  the outer window, and acts as a hard cap. On a device with
  `motion.days > continuous.days`, a camera whose processing lags persistently
  would keep no-motion footage for the longer window.
- *Limitation: diagnostics.* For continuous cameras, "Too many unprocessed
  recording segments … issue with the detect stream" stops firing, because
  those segments now move. A rate-limited warning replaces it.
- *Benefit not in the handover.* Continuous recording no longer depends on
  the detect stream being healthy. Today a dead detect stream also costs
  continuous footage.
- *Pre-existing gap (open question 1).* The mover decides "continuous" from
  the camera's `record.continuous.days` and ignores a variant's `retain_days`,
  while cleanup treats `retain_days` as continuous retention. This device sets
  `continuous.days` to 7, so it is not affected.

### Change 2: the database insert

**Verdict: valid. The proposed shape needs four additions to be safe.**

- *Clarification.* `asyncio.gather` finishes all copies, and each copy removes
  its cache file, before `send_data` is called. The request does not stop
  copying within a pass. It delays the next pass, and that pass then discards
  the backlog. The effect is the one described, but the timing matters for
  crash analysis: today, the rows at risk in a crash are those of one pass.
- **Found in review: a latent hang.** `InterProcessCommunicator.read()` catches
  only `zmq.ZMQError`. `handle_insert_many_recordings` calls
  `Recordings.insert_many(...).execute()`, and `SqliteQueueDatabase` re-raises
  the writer's exception there, for example `database is locked` after the
  320 s busy timeout. That exception would end the REP thread. Every REQ client
  in Frigate then blocks forever: the mover, all camera watchdogs, review and
  previews. The handler must catch database errors and return them as a result.
- **Found in review: timeline correctness.** `roll_up_ranges` assumes every row
  lands within `MAX_SEGMENT_DURATION` (600 s) of its segment's start. It
  re-merges only that much behind `covered_to`. A queue that holds rows longer,
  and recovery that inserts rows hours late, would leave the precomputed
  timeline permanently short. This is the bug class in
  `ra-docs/ranges-underreporting-postmortem.md`. Late rows must repair the
  ranges.
- *Also needed:* retries must be idempotent (`recordings_path` is a unique
  index, so a retried batch that had already landed would otherwise fail
  forever). Batches must stay under SQLite's bound-variable limit
  (17 columns × rows). The `saved` message, which LPR and audio-transcription
  post-processing read as "recordings available through", must not get ahead
  of the rows actually written.
- *Limitation.* The main REP thread still waits on the database while it is
  locked, so other inter-process clients stall as they do today. The recording
  path no longer does.
- *Limitation: crash window.* Rows at risk grow from one pass to the queue
  depth, which is normally near zero and minutes long during a database stall.
  Files that are moved but have no row are never expired, because cleanup is
  driven by rows and `sync_recordings` is off. They would leak disk. This is
  why the design adds bounded recovery after an unclean shutdown (D5). The
  stalls in question come with heavy swapping, which is exactly when an OOM
  kill is most likely.

### Change 3: the watchdog

**Verdict: valid, but the handover's suggested signal does not work as
transported today.**

- **Correction.** `latest_cache_segment_time` also comes from the mover. It is
  published at the top of each `move_files` pass. When the mover stalls,
  `cache_stale` goes stale too. The watchdog must therefore observe the cache
  itself.
- **Found in review: per-camera, not per-recorder.** All three timestamps are
  kept per camera, across variants. A hung `sub` recorder is masked as long as
  `main` keeps writing. A stale signal restarts every record process of the
  camera, healthy ones included.
- *Limitation.* While the mover is stalled, a recorder that writes only invalid
  segments is caught only after the mover resumes. While the mover is stalled,
  the cache freshness check still catches a recorder that stops writing.
- *Limitation.* Today a dead mover at least causes restart noise. After this
  change it would be silent. The watchdog must therefore report a stalled mover
  explicitly.

### Change 4: the WAL checkpoint

**Verdict: valid for the blocking. The "no NVMe burst" acceptance criterion
does not necessarily follow.**

- `truncate_wal()` runs `PRAGMA wal_checkpoint(TRUNCATE)` whenever the WAL file
  is over 10 MB. TRUNCATE has RESTART semantics: it blocks new writers while it
  waits for readers to finish, and while it copies. Its duration therefore also
  depends on concurrent API readers, not only on the WAL size.
- A `PASSIVE` checkpoint copies the same frames without blocking writers. If
  the 55 s NVMe burst is copy I/O from an accumulated WAL, the burst moves
  rather than disappears. It still competes with swap on the same NVMe. What
  changes for the recording path is that no writer waits, and with change 2
  the mover would not wait anyway.
- *Why the WAL accumulates is unknown.* Open question 3 lists the hypotheses.
  The design adds the instrumentation to tell them apart. The fix does not
  depend on the answer.
- *Limitation.* PASSIVE cannot finish while readers pin old frames (checkpoint
  starvation). A bounded TRUNCATE fallback covers an oversized WAL.

### Lower-priority items

- **Copy concurrency cap: valid, promoted to required.** One pass spawns an
  ffmpeg copy for every movable segment, all at once. The 06:00 backlog cleared
  about 250 segments in 5 s, so up to ~250 copies ran together. At an estimated
  5–10 MB private memory each, that is 1.2–2.5 GB of burst memory on a box
  with ~2 GB free. Changes 1–3 keep backlogs instead of
  dropping them, so they make large passes more frequent. The cap must cover
  the ffprobe fallback as well as the copy. The cv2 probe is synchronous and
  already runs one at a time.
- **SQLite page cache: partly done already.** The fork uses 256 MB plus a
  256 MB mmap, not the upstream 500 MB. `cache_size` is per connection, though,
  and peewee opens one connection per thread. In the main process, the writer,
  4 API threads, cleanup, the storage maintainer and others can each grow to
  256 MB. Make it configurable. Lower the default only after measuring API
  latency on the device.
- **Shorten the cleanup run: valid, and the database phase can shrink too.**
  `remove_empty_directories` walks ~70 k directories and ~10 M entries on the
  HDDs every run. The expiry queries also read far more than they delete:
  - The deleted-cameras query (`camera NOT IN (…) AND end_time < …`) has no
    usable index. Every run reads the whole table, or a whole index covering
    all ~10 M rows.
  - The per-variant query seeks only on `(camera, variant)`. It then walks every
    index entry of that variant's retained history, ~320 k per dual-stream
    camera, so ~10 M per run.
  - The orphan-variant query does the same per camera.

  `start_time ≤ end_time` always holds, so adding `start_time < bound` changes
  no result and lets SQLite seek
  `recordings_camera_variant_start_time_end_time`,
  `recordings_camera_start_time_end_time` or `recordings_start_time`. This is
  likely a large part of the 19-minute NVMe phase. EXPLAIN QUERY PLAN tests and
  the probe will confirm it.
- **Raise the cache limit 12 → 20: valid only after preview frames leave the
  tmpfs.** `/tmp/cache` is RAM, and on this box that means more swap. A
  better fix for the same problem is to stop discarding before the mover has
  tried to move the backlog (D9). The default stays at upstream's 6 per variant.
  On this device, 10 per variant is recommended once the preview bind mount is
  in place.

## Decisions

### D1. Continuous segments time out of the processing wait; stats are `NULL`

The timeout applies when the segment's variant has continuous retention and
processing has not reached `end_time` within `processing_timeout` seconds of
`end_time`. The segment then moves with mode `all`, and `motion`, `objects`,
`regions` and `dBFS` are `NULL`. All four are set to `NULL`, so a row's stats
are either all known or all unknown. A segment whose stats are complete is
moved as today.

- *Alternative:* write `0`. Rejected. Cleanup would expire the segment early
  as "no motion".
- *Alternative:* backfill stats with an `UPDATE` when frames arrive. Rejected
  for now. The frames arrive in a burst exactly when the stall clears, so the
  backfill would add a write burst to a database that is just recovering. It
  also needs per-segment bookkeeping beyond the existing pruning of
  `object_recordings_info`. It can be a follow-up if the gaps in the motion
  timeline turn out to matter.
- A rate-limited warning (at most once per camera per 5 minutes) names the
  camera and how far its processing is behind.

### D2. Activity-based discard needs complete statistics

`should_discard_segment` is consulted only when the processed-frame time is at
or past the segment's end. Otherwise the segment stays in the cache for a later
pass: for continuous cameras until D1's timeout, and for other cameras until
the processing catches up or the existing "unprocessed" limit applies. For
continuous cameras, the review path is no longer a separate early exit. Once
the segment is ready it moves with mode `all`, because continuous retention
keeps it anyway and cleanup applies the longer review retention later.

### D3. A bounded, acknowledged row writer in the recording process

- A new `frigate/record/writer.py` owns its own `InterProcessRequestor`. The
  mover hands rows to it and continues. The writer sends batches of at most
  500 rows.
- The main-process handler inserts with `INSERT … ON CONFLICT DO NOTHING`. The
  `recordings_path` unique index makes a retried batch harmless. The handler
  catches database errors and replies `{"ok": false, "error": …}`. On success
  it replies `{"ok": true, "count": n}`.
- On failure the writer retries the batch with backoff (1 s, doubling, up to
  30 s). It logs a warning on the first failure, an error once writes have
  failed for 60 s, and an info line on recovery, each with the queue depth.
- The queue is bounded at `db_write_queue` rows (default 8192, about 21 minutes
  at 32 dual-stream cameras). When it is full, the mover waits for room and
  logs an error. Rows are never dropped. Backpressure then falls back to
  today's cache limit, which logs its own warnings.
- On shutdown the mover stops first, and the writer then drains for up to
  `db_flush_timeout` (default 10 s). `FrigateApp.stop()` already stops the
  recording process before the dispatcher and the database, but it sends
  SIGTERM straight away, and the recording process has no SIGTERM handler. So
  the stop sequence must first `join` the recording process for
  `db_flush_timeout` + 5 s and only then terminate it. Rows still unsent are
  logged at error level with their count and their oldest and newest paths,
  and the clean-shutdown marker (D5) is not written.
- `saved` is published by the writer after a batch is acknowledged. For each
  camera it carries the oldest start time still in the cache or in the queue,
  so it never advertises unwritten footage.
- `async_db_writes: false` restores the blocking call, still with the hardened
  handler.
- *Alternative:* make the main-process handler non-blocking. Rejected for this
  change, because the mover would still depend on the main process answering,
  which is the process that swaps. It is listed as a follow-up for the other
  REQ clients.
- *Alternative:* write directly from the recording process. Rejected. A second
  writer process would contend for the SQLite lock, and the design would
  diverge from upstream's single-writer layout.
- *Alternative:* a REQ timeout. Rejected. A REQ socket that times out must be
  rebuilt (the "lazy pirate" pattern), and the reply to the abandoned request
  can still arrive.

### D4. Late rows repair the precomputed timeline

The insert handler keeps, per camera and variant, the earliest start time among
rows that start before that variant's `covered_to`. The next rollup tick
recomputes `[earliest − MAX_SEGMENT_DURATION, covered_to]` with
`_replace_window` before advancing coverage, and then clears the entry. In
normal operation no row is that late, so this costs nothing. The state lives in
memory in the main process. A restart before the tick loses it, so D5's
recovery marks its own late rows after a restart the same way.

### D5. Recovery after an unclean shutdown

- **Marker.** The writer writes a clean-shutdown marker next to the database
  after a full drain. At startup, the main process reads and removes it before
  the recording process starts.
- **Scan.** If the marker is missing, `RecordingCleanup` scans, before
  `sync_recordings` and before its first tick, the
  `recordings/{date}/{hour}/{camera}/{variant}` directories of the last
  `reconcile_window` hours (default 2, well above the queue depth). It compares
  them with one indexed query per recorder and finds files that have no row.
- **Insert.** Each orphan is probed (duration, codec, size) one at a time and
  inserted with `NULL` stats. Inserts are idempotent through the path index.
  Late-row repair (D4) applies to them.
- **Unprobeable files.** A file that cannot be probed is logged and left alone.
- **Cost.** About 46 k directory entries, 64 indexed queries, and one probe per
  orphan, run only after an unclean shutdown.
- **Interaction with `sync_recordings`.** Running before `sync_recordings`
  matters: when sync is on, it deletes files that have no row.
- `reconcile_on_unclean_shutdown: false` accepts the risk instead.

### D6. Recorder freshness comes from the cache, per variant

- **Cache check.** Each check (every `ffmpeg.retry_interval`, 10 s by default),
  the watchdog scans `/tmp/cache` once and picks up names starting with
  `{camera}@`. For each variant it keeps the newest start time parsed from the
  name. It stats only the newest file, to read its mtime.
- **Freshness.** A recorder is fresh if its newest start time or that file's
  mtime is within `recorder_stale_timeout`. The value is remembered across
  checks, so a recorder whose last file was moved away still ages correctly.
- **Variant per command.** `ffmpeg_cmds` entries gain the input's
  `record_variant`, so each record process maps to one recorder.
- **Mover heartbeat.** This is the arrival time of `latest` messages, which
  every pass publishes for every camera. Valid and invalid timestamps are
  tracked per variant, using the `cache_path` carried in their messages.
- **Validity checks.** `valid_stale` and `invalid_stale` apply only while the
  heartbeat is fresh. Their staleness is measured from the later of the event
  and the moment the heartbeat was last seen resuming. This avoids a race in
  which a resumed mover has not yet republished `valid` for its backlog.
- **Stalled mover.** When the heartbeat is stale beyond the timeout, the
  watchdog logs a rate-limited warning naming the mover, and restarts nothing.
- **Upstream mode.** `recorder_watchdog: mover` restores the upstream signals.
- **Startup grace.** The 90 s grace after start or enable is unchanged, but it
  becomes a named constant tied to the timeout: `min(90, timeout)`.

### D7. WAL maintenance is passive, and the WAL is bounded

- **Connection pragma.** All main-process and recording-process connections
  set `journal_size_limit` to `wal_size_limit` (default 64 MB). Whenever SQLite
  resets the WAL, the file is truncated back to that size.
- **Closing checkpoint.** After each cleanup run, `wal_checkpoint(PASSIVE)` runs
  and its `(busy, log, checkpointed)` result is logged.
- **Fallback.** Only if the WAL file is still over 4 × `wal_size_limit`,
  `wal_checkpoint(TRUNCATE)` runs on a connection with a 2 s busy timeout. If it
  is busy, it gives up and logs a warning. It does not hold writers for long.
- **Upstream mode.** `wal_checkpoint: truncate` restores the upstream behavior,
  including its 10 MB threshold.
- **Run summary.** One info line per run records the duration of each phase
  (expire, directories, checkpoint, stats, ranges), the WAL size before and
  after, and the checkpoint result. Correlating this with the probe answers
  open question 3.

### D8. Mover concurrency is capped

An `asyncio.Semaphore(move_concurrency)` limits how many segments are in the
subprocess part of `validate_and_move_segment` at once: the ffprobe fallback
and the copy. The default is 8, and `0` means unbounded (upstream).
Steady-state demand is 6.4 segments/s at 32 dual-stream cameras. At 0.2–1 s
per copy, 8 slots give 8–40 segments/s, and the existing debug line
"Copied … in N seconds" gives the real figure.

### D9. The cache limit is per recorder and applied after the moves

- **Per recorder.** `max_cached_segments` (default 6) applies per camera and
  variant, so one lagging variant cannot cost the other variant footage.
- **Order.** The "Unable to keep up" and "Too many unprocessed" discards run at
  the end of a pass, on what is still in the cache. A backlog that the pass
  moved is therefore never discarded. The log texts stay the same, so existing
  log monitoring keeps working.
- **Free-space reserve.** At the start and end of each pass, if `/tmp/cache`
  free space is below `cache_free_reserve` percent (default 10, `0` = off), the
  oldest closed segments across all cameras are discarded until the reserve is
  restored, with an error log. This prevents the 09:00 "No space left on
  device" failure, in which every recorder truncated its segment and the
  watchdog then restarted them all.

### D10. The cleanup run touches only what it expired

- **Empty directories.** With `empty_directory_cleanup: scoped` (the default),
  the run collects the parent directory of every file it deleted. After expiry
  it removes those directories and their parents up to `recordings/`, as each
  becomes empty. `full` restores the whole-tree walk. Directories emptied by
  other means stay until a `full` run. Empty directories are harmless.
- **Seekable queries.** Expiry queries get a `start_time` upper bound equal to
  the largest end-time cutoff they use. The deleted-cameras query uses
  `recordings_start_time`. The per-variant query uses the composite variant
  index. The orphan-variant query uses `(camera, start_time, end_time)`.
  Equivalence and `EXPLAIN QUERY PLAN` tests guard them, following
  `TestRecordingsOverlapQuery`.

### D11. Configuration lives in fork-owned models

Process-wide settings go in a new `record.pipeline` block. Its model lives in a
new fork-only file, and the global value is the one used. Camera-level values
are ignored, as for `record.expire_interval`. The exception is
`recorder_stale_timeout`, which a camera may override. Database settings go on
`DatabaseConfig`.

| Setting | Default | Upstream behavior | Scope |
|---|---|---|---|
| `record.pipeline.processing_timeout` | 30 s | `0` = wait indefinitely | global |
| `record.pipeline.move_concurrency` | 8 | `0` = unbounded | global |
| `record.pipeline.max_cached_segments` | 6 per variant | 6 | global |
| `record.pipeline.cache_free_reserve` | 10 % | `0` = off | global |
| `record.pipeline.async_db_writes` | `true` | `false` | global |
| `record.pipeline.db_write_queue` | 8192 rows | – | global |
| `record.pipeline.db_flush_timeout` | 10 s | – | global |
| `record.pipeline.reconcile_on_unclean_shutdown` | `true` | `false` | global |
| `record.pipeline.reconcile_window` | 2 h | – | global |
| `record.pipeline.recorder_watchdog` | `cache` | `mover` | global |
| `record.pipeline.recorder_stale_timeout` | 120 s | 120 s | global, camera override |
| `record.pipeline.empty_directory_cleanup` | `scoped` | `full` | global |
| `database.wal_checkpoint` | `passive` | `truncate` | global |
| `database.wal_size_limit` | 64 MB | 10 MB threshold, no limit | global |
| `database.cache_size` | 256 MB | 500 MB (fork today: 256) | global |

Validation: `processing_timeout + 10 < max_cached_segments × 10`, with all values
non-negative and timeouts positive.

### D12. A feature flag for unknown stats

`recordings_unknown_stats` is appended to `FORK_FEATURES`. A client that sees it
knows that `motion` and `objects` can be `null` and must not read `null` as zero.

## Resource cost on the target device

Figures assume 32 dual-stream cameras, 4 cores and 7.8 GB RAM. "Today" is
`v17.1-update-with-adaptive`.

| Part | CPU | RAM | Disk I/O |
|---|---|---|---|
| D1 processing timeout | none | segments leave the tmpfs sooner | none; no extra retained disk on this device (see review) |
| D2 complete-stats discard | none | a few segments stay seconds longer in the tmpfs | none |
| D3 row writer | one mostly idle thread | ≤ ~12 MB with a full queue (~1.5 KB per row) | same rows; fewer transactions after a stall |
| D4 late-row repair | one seekable merge per late window; zero normally | none | NVMe reads for that window only |
| D5 recovery | after unclean shutdown only: ~46 k directory entries, 64 indexed queries, one probe per orphan, serialized | negligible | HDD metadata reads for 2 h of directories |
| D6 cache watchdog | 32 × one tmpfs `scandir` (≈120 entries, ≤ ~1 k under backlog) + ≤ 2 `stat` per 10 s: well under 1% of a core | negligible | tmpfs only |
| D7 passive WAL | none | none | same checkpoint I/O without the writer lock; WAL file ≤ 64 MB between runs |
| D8 copy cap | flattens bursts | ≤ 8 ffmpeg copies (est. ≤ 80 MB) instead of hundreds (est. 1.2–2.5 GB) | spreads HDD writes |
| D9 cache limit + reserve | one `statvfs` per pass | default unchanged; +0.4 GB of tmpfs if raised to 10 per variant | none |
| D10 cleanup run | removes ~14 min of directory walking per run | less dentry/inode cache churn, so less pressure to swap process memory | removes ~10 M HDD directory entries and ~10 M NVMe index entries read per run |
| `database.cache_size` | none | default unchanged; lower values save up to 192 MB per busy connection | more NVMe reads at lower values |

## Risks / Trade-offs

- [Unknown stats extend retention on devices where `motion.days` > `continuous.days`] → Not the case on this device. It is documented, the processing-lag warning makes it visible, and `processing_timeout: 0` opts out.
- [A camera whose processing lags persistently shows no motion activity on the timeline] → The rate-limited warning names the camera. The footage itself is kept.
- [Rows are queued in memory when a crash happens] → D5 recovers them after an unclean shutdown. The remaining risk is a file that cannot be probed after a power loss mid-write. It is logged and left on disk.
- [The container's stop grace period is shorter than the shutdown flush, so Docker kills Frigate mid-drain] → The marker is not written, so D5 recovers on the next start. Check that the device's compose file sets a stop grace period of at least 30 s.
- [During a database stall the newest minutes of footage are on disk but not yet listed] → Accepted. The rows appear when the database recovers, and D4 keeps the timeline correct.
- [The main REP thread still blocks during database stalls] → Unchanged from upstream for other clients. Listed as a follow-up.
- [Invalid-segment detection is deferred while the mover is stalled] → Bounded by the mover's recovery. The stalled mover itself is reported.
- [PASSIVE checkpoints starve under constant readers] → Bounded TRUNCATE fallback, and the run summary logs the WAL size.
- [The copy cap is too low if copies are slow on busy HDDs] → The cap is configurable, and the debug copy-time log measures it. Validate on the device before release.
- [Discarding after the moves lets the tmpfs fill within a pass] → The free-space reserve runs at the start and end of every pass.
- [Field tests that pause the mover can fill the 1.2 GB tmpfs] → Run them after the preview bind mount, watch free space, and stop at 80%.
- [Swap and the database share one NVMe, so any heavy database I/O slows swap-ins for every process] → Out of scope. D7 and D10 reduce the database's share.
- [A larger diff in `maintainer.py`, `video.py` and `cleanup.py` means rebase conflicts] → The logic goes in fork-only modules, and the upstream files only call into them.

## Migration Plan

- No schema migration. Upgrading does not touch existing rows.
- All parts are on by default. Any part can be switched back to upstream
  behavior in config, without a rebuild, using the "Upstream behavior" column
  above.
- Rollback to the previous image is safe: rows with `NULL` stats are valid
  there, and its cleanup keeps them under the same SQL.
- Suggested field order on one device: D7 + D10 (most frequent stall, lowest
  risk) → D8 + D1 + D2 → D3 + D4 + D5 → D6. Each step uses the toggles and is
  checked against the probe before the next.

## Open Questions

1. Should the move decision honor a variant's `retain_days` when the camera's
   `continuous.days` is 0? Cleanup does honor it. This device is not affected.
   Recommendation: a separate change, because it changes what gets recorded.
2. Should D5 ship in this change? Recommendation: yes. The stalls happen under
   memory pressure, which is when OOM kills happen. The tasks keep it as its
   own group, so it can be cut.
3. Why does the WAL hold about a minute of work at the end of a run? There are
   three hypotheses, which the probe's WAL-size log and D7's run summary can
   tell apart:
   - (a) `auto_vacuum = FULL` (set in the pragmas) relocates pages on every
     delete commit and multiplies WAL volume. Check `PRAGMA auto_vacuum` on the
     device database.
   - (b) A long-lived reader pins old frames, so autocheckpoints cannot
     complete. Steady WAL growth during the tree walk would show this.
   - (c) The burst is mostly the main process swapping back in after the walk,
     plus the TRUNCATE's sync of the database file.
4. Is 8 the right copy cap for this HDD layout, or is 4 better for seek
   patterns? Measure.
5. Can `database.cache_size` default to 64–128 MB? Measure API latency on the
   device first.
6. Follow-up: make the main-process insert handler non-blocking, so the REP
   thread stays free for other clients during database stalls.
