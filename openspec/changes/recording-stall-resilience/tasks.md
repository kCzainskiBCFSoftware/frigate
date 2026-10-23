# Tasks

Groups 2–8 can land as separate commits in the order given in the design's
Migration Plan. Group 5 (recovery) can be cut from this change without
affecting the others (open question 2).

## 1. Configuration

- [x] 1.1 Add a `RecordPipelineConfig` model in a new fork-only file `frigate/config/camera/record_pipeline.py` with the `record.pipeline` settings and defaults from design D11. Add it to `RecordConfig` as `pipeline`. Validate `processing_timeout + 10 < max_cached_segments × 10`, and reject negative counts and durations.
- [x] 1.2 Make every `record.pipeline` setting except `recorder_stale_timeout` global-only: the global value is used and camera-level values are ignored, as for `record.expire_interval`. Let a camera override `recorder_stale_timeout`.
- [x] 1.3 Add `wal_checkpoint` (`passive`|`truncate`), `wal_size_limit` (MB) and `cache_size` (MB) to `DatabaseConfig` in `frigate/config/database.py`.
- [x] 1.4 Add the input's `record_variant` to each `ffmpeg_cmds` entry in `CameraConfig.create_ffmpeg_cmds` (`frigate/config/camera/camera.py`).
- [x] 1.5 Extend `frigate/test/test_config.py`: defaults, the validation error for a processing wait longer than the backlog window, a camera override of `recorder_stale_timeout`, and the variant on `ffmpeg_cmds`.
- [x] 1.6 Regenerate the English config translations for `record` and `database` with `generate_config_translations.py`.

## 2. Mover: processing wait, complete-stats discard, concurrency, backlog limit

- [ ] 2.1 In `RecordingMaintainer.validate_and_move_segment`, move a continuous-retention segment once frame processing has passed `end_time`, or once `processing_timeout` seconds have passed since `end_time`. On the timeout, store `motion`, `objects`, `regions` and `dBFS` as `None`. `processing_timeout: 0` keeps the upstream wait.
- [ ] 2.2 Only consult `should_discard_segment` when processing has passed `end_time`. For continuous cameras, remove the early review-path move. A not-yet-processed segment stays cached (design D2).
- [ ] 2.3 Add the rate-limited "frame processing is N s behind" warning (once per camera per 5 min).
- [ ] 2.4 Wrap the probe-fallback and copy part of segment handling in an `asyncio.Semaphore(move_concurrency)`. `0` means unbounded.
- [ ] 2.5 Apply the backlog limit per camera and variant, after the pass's moves, keeping the existing warning texts (design D9).
- [ ] 2.6 Add the cache free-space reserve check (`os.statvfs(CACHE_DIR)`) at the start and end of each pass. Discard the oldest closed segments across cameras, with an error log.
- [ ] 2.7 Tests in `frigate/test/test_maintainer.py`:
  - a timed-out continuous segment is moved with `None` stats, and a processed one with real stats;
  - `processing_timeout: 0` waits;
  - a motion-only camera waits;
  - a segment overlapping an in-progress review while unprocessed is not discarded;
  - at most N copies are in flight;
  - a backlog moved in the same pass is not discarded;
  - per-variant limits;
  - the free-space reserve.
- [ ] 2.8 Tests in `frigate/test/test_record_cleanup.py` and `test_record_retention.py`: rows with `NULL` stats survive the continuous-only rule and the `motion`/`active_objects` review modes, expire at the outer window, and respect a variant's `retain_days` hard cap.
- [ ] 2.9 Check the API and UI with `NULL` stats:
  - `GET /api/{camera}/recordings` returns `null`;
  - `/recordings/summary` per-hour totals follow the spec;
  - `/review/activity/motion` skips them;
  - the web timeline renders and plays such a window.

  Add an API test in `frigate/test/http_api/test_http_media.py`.

## 3. Database writes off the mover's path

- [ ] 3.1 Harden `handle_insert_many_recordings` in `frigate/comms/dispatcher.py`: insert with on-conflict-ignore, catch database errors, and return `{"ok": bool, "count"|"error"}`. No exception may reach `InterProcessCommunicator.read()`.
- [ ] 3.2 Add `frigate/record/writer.py`, a writer thread in the recording process with:
  - a bounded queue of `db_write_queue` rows, with backpressure on the mover;
  - batches of at most 500 rows;
  - retry with backoff from 1 s up to 30 s;
  - warning, error and recovery logs with the queue depth.
- [ ] 3.3 Wire the writer into `RecordingMaintainer.move_files` and `RecordProcess.run` behind `async_db_writes`. With the setting off, keep the blocking call but use the hardened handler.
- [ ] 3.4 Move the `saved` publication into the writer, publishing after acknowledgement the oldest start time still cached or queued per camera.
- [ ] 3.5 Shutdown:
  - drain the writer for up to `db_flush_timeout` when `stop_event` is set;
  - write the clean-shutdown marker next to the database only after a full drain;
  - in `FrigateApp.stop()`, `join` the recording process for `db_flush_timeout` + 5 s before `terminate()`.
- [ ] 3.6 Late-row repair (design D4):
  - in the insert handler, record per camera and variant the earliest start time of rows that start before `covered_to`;
  - in `roll_up_ranges` (`frigate/record/ranges.py`), recompute `[earliest − MAX_SEGMENT_DURATION, covered_to]` with `_replace_window` before advancing, then clear the entry.
- [ ] 3.7 Tests:
  - the writer keeps moving while the requestor blocks;
  - the queue bound and backpressure;
  - a retry after a failed outcome;
  - idempotent re-send;
  - flush on stop;
  - the handler returns a failure on a locked database and the REP loop keeps serving;
  - in `frigate/test/test_record_ranges.py`, a row inserted 30 min late shows up in ranges after one rollup tick.

## 4. Recorder watchdog

- [ ] 4.1 Add `frigate/record/cache_watch.py`. It scans `CACHE_DIR` once per check, tracks per variant the newest start time from file names and that file's mtime, and remembers the last-seen freshness.
- [ ] 4.2 In `CameraWatchdog` (`frigate/video.py`):
  - judge each record process by its variant's freshness;
  - use the arrival of `latest` messages as the mover heartbeat;
  - gate `valid_stale` and `invalid_stale` on the heartbeat, measured from the last resume;
  - attribute valid and invalid messages to a variant through `cache_path`;
  - restart only the stale recorder.
- [ ] 4.3 Add the rate-limited "recording mover stalled" warning. Replace the hardcoded `120` in the checks and the message with `recorder_stale_timeout`, and the grace period with `min(90, timeout)`. Keep `recorder_watchdog: mover` as the upstream path.
- [ ] 4.4 Tests in `frigate/test/test_video.py`:
  - a paused mover with fresh cache files gives no restart;
  - a recorder with no new files is restarted after the timeout;
  - a hung `sub` with a healthy `main` restarts only `sub`;
  - invalid-only segments restart that variant's recorder while the heartbeat is fresh;
  - no restart while the backlog clears after a resume.

## 5. Recovery after an unclean shutdown

- [ ] 5.1 In `FrigateApp`, read and remove the clean-shutdown marker before the recording process starts, and pass "unclean" to `RecordingCleanup`.
- [ ] 5.2 Add `frigate/record/reconcile.py`. It lists the per-variant hour directories of the last `reconcile_window` hours and compares them with one indexed query per camera and variant. For each orphan, one at a time, it probes the file and inserts a row with `NULL` stats, the variant from the path and on-conflict-ignore. It logs files that cannot be probed, and marks late rows for the ranges repair (3.6).
- [ ] 5.3 Run it at the top of `RecordingCleanup.run()`, before `sync_recordings`, only when the previous shutdown was unclean and `reconcile_on_unclean_shutdown` is on.
- [ ] 5.4 Tests: orphans in the window are inserted with the right variant and duration, files outside the window are ignored, re-running is a no-op, and a clean marker skips the scan.

## 6. WAL maintenance

- [x] 6.1 Add `frigate/db/maintenance.py` with:
  - a passive checkpoint that returns `(busy, log, checkpointed)`;
  - the bounded `TRUNCATE` fallback (2 s busy timeout, attempted above 4 × `wal_size_limit`);
  - the `truncate` mode that restores upstream behavior.

  Call it from `RecordingCleanup.truncate_wal`.
- [x] 6.2 Set `journal_size_limit` and `cache_size` from `DatabaseConfig` in the pragmas in `FrigateApp.bind_database` and `RecordProcess.run`.
- [x] 6.3 Log the per-run summary line: each phase's duration, the WAL size before and after, and the checkpoint result.
- [x] 6.4 Tests: with a reader holding a snapshot, a writer is not blocked by the passive checkpoint; the fallback gives up within about 2 s while a reader holds a snapshot; `journal_size_limit` is set on new connections.

## 7. Shorter cleanup run

- [ ] 7.1 Make `expire_existing_camera_recordings` (and the removed-camera and orphan-variant sweeps in `expire_recordings`) return the parent directories of the files they delete. Add a scoped removal in `frigate/record/util.py` that removes those directories and their parents up to `RECORD_DIR`, as each becomes empty. Keep `remove_empty_directories` for `empty_directory_cleanup: full`.
- [ ] 7.2 Add `start_time` upper bounds to the removed-camera, per-variant and orphan-variant expiry queries in `frigate/record/cleanup.py` (design D10).
- [ ] 7.3 Tests:
  - scoped removal deletes only emptied branches and never `RECORD_DIR`;
  - equivalence of the bounded queries on a mixed fixture with null-stat, motion, review-overlapping, removed-camera and unconfigured-variant rows;
  - an `EXPLAIN QUERY PLAN` guard that each query uses a range search on `start_time`, following `TestRecordingsOverlapQuery`.

## 8. Feature flag, docs and changelog

- [ ] 8.1 Append `recordings_unknown_stats` to `FORK_FEATURES` in `frigate/const.py`, and update the fork-block test.
- [ ] 8.2 Update `ra-docs/fork-changes.md`: a new strand for recording-path resilience, covering the config keys, the new modules, the upstream files touched and the opt-outs.
- [ ] 8.3 Update `ra-docs/dual-stream-api-changes.md`. `motion` and `objects` can be `null` in recording listings and summaries, and clients detect this through `recordings_unknown_stats`. Mark it **BREAKING** for strict clients.
- [ ] 8.4 Document the `record.pipeline` and `database` settings, with their upstream-equivalent values, in the fork's config reference in `ra-docs/`.

## 9. Verification

- [ ] 9.1 Run the touched backend test modules in the Docker test image (`frigate-test-deps:local`, or the release image if present): `test_config`, `test_maintainer`, `test_record_cleanup`, `test_record_retention`, `test_record_ranges`, `test_video`, `http_api.test_http_media`, plus the new writer, reconcile and maintenance tests. Compare any failure with the base commit before treating it as a regression.
- [ ] 9.2 Run ruff (`--select E4,E7,E9,F,I --ignore E501,E711,E712`) and `ruff format --check` on `frigate/` in the container.
- [ ] 9.3 Check the web timeline and playback against a database that has `NULL`-stat rows (see 2.9).
- [ ] 9.4 Field validation on the 32-camera device, after the preview-frames bind mount is in place, watching tmpfs free space and stopping at 80%:
  - (a) `SIGSTOP` one camera's `frigate.process` for 3 min: its segments keep arriving, its rows have null motion, there are no drops, and the UI renders.
  - (b) Hold a write lock on `frigate.db` from another process for 90 s: there are no drops, and every row appears within a minute after release.
  - (c) `SIGSTOP` the recording manager for 3 min: there are no recorder restarts, and the mover-stalled warning is logged.
  - (d) Kill one recorder's ffmpeg output: only that recorder is restarted, after the timeout.
  - (e) Over a full cleanup run: there is no "Unable to keep up", the WAL is at or below `wal_size_limit` between runs, and the run summary is compared with the probe's NVMe and HDD traces.
  - (f) Record copy times from the "Copied … in N seconds" debug line, to confirm the `move_concurrency` default.
- [ ] 9.5 Answer open questions 3–5 from the field data, and record the answers in the design before archiving.
