# Tasks

Backend tests run in Docker (`frigate-test-deps:local` or the ECR image), with
`pysqlite3-binary` installed to match CI. Every new test must fail with its fix removed;
check that before ticking the task.

## 1. Shared database busy timeout

- [x] 1.1 Add `busy_timeout(config) -> int` (`max(60, 10 × cameras enabled in config)`) to `frigate/db/maintenance.py` and use it in `FrigateApp.bind_database` instead of the inline formula. Verify with a test in `frigate/test/test_db_maintenance.py` covering 3 and 32 enabled cameras plus a disabled one.

## 2. The request thread cannot die (`frigate/comms/inter_process.py`)

- [x] 2.1 In `InterProcessCommunicator.read()`, guard the dispatch and the reply as in design Decision 1: a handler exception becomes the `[]` reply, a failing `send_json(response)` falls back to `send_json([])`, and an outer catch-all logs and continues. The `zmq.ZMQError` break of the inner drain loop stays. Set `self.busy = (topic, time.monotonic())` before the dispatch and `None` after the reply.
- [x] 2.2 Add per-topic error logging: at most one traceback per topic per 5 minutes, and the next logged error reports how many failures were suppressed.
- [x] 2.3 Add `frigate/test/test_inter_process.py`, using a real REP/REQ pair on a temp `ipc://` endpoint (patch `SOCKET_REP_REQ`; take the module through a `real_inter_process()`-style import like `frigate/test/test_record_writer.py:439`). Verify:
  - a raising handler gets `[]` and the next request on another requestor is answered;
  - a handler returning `{1, 2}` (not JSON) gets `[]`;
  - 50 failures of one topic give one traceback, and the next window reports 49;
  - `busy` is set during a slow handler and `None` after it;
  - the thread is still alive after all of the above.

## 3. Timed requests (`InterProcessRequestor`) and their use on the recording path

- [x] 3.1 Add `InterProcessRequestor(timeout: Optional[float] = None)`. With a timeout, set `RCVTIMEO`/`SNDTIMEO`. On `zmq.Again`, close the socket with `linger=0`, open and connect a new REQ socket, log a warning with the topic, and return `""`. Without a timeout, behavior is unchanged. Verify in `test_inter_process.py`: against a REP that never answers, `send_data` returns `""` within timeout + 1 s, and once a working REP answers, the next `send_data` on the same requestor gets its reply.
- [x] 3.2 Build the writer's requestor with `timeout=busy_timeout(config) + 30` (`frigate/record/record.py`, `requestor_factory`), and give the mover's `self.requestor` (`frigate/record/maintainer.py`) the same timeout. Verify with a test that the factory passes the timeout.
- [x] 3.3 (Superseded by 10.2: the rows are now kept, not dropped.) In the mover's blocking fallback, log an error with the batch size and the first and last path when the reply is not a dict (no reply), as well as on `ok: false`. Verify with a test in `frigate/test/test_maintainer.py` that uses a requestor returning `""`.
- [x] 3.4 Integration test in `frigate/test/test_record_writer.py`: a real communicator whose `INSERT_MANY_RECORDINGS` handler blocks past a short writer timeout, then recovers. The writer reconnects, the batch is stored once, and the queue drains. This is the spec scenario "No reply".

## 4. Region grid keeps the current grid (`frigate/video.py`)

- [x] 4.1 In `process_frames`, replace `region_grid` only when the reply is a non-empty list. Verify with a test in `frigate/test/test_video.py`: with a requestor returning `[]` and then `""`, the grid passed to region selection is still the initial one.

## 5. Watchdog for the request thread (`frigate/watchdog.py`, `frigate/app.py`)

- [x] 5.1 Give `FrigateWatchdog` the optional arguments `inter_process` and `busy_timeout`, and pass both from `FrigateApp.start_watchdog()`. On each 10 s check, unless the app's or the communicator's stop event is set:
  - reader thread not alive → log an error naming the inter-process request thread, then `restart_frigate()`;
  - `busy` older than `max(600, 2 × busy_timeout)` → log an error with the topic and duration, then `restart_frigate()`.
- [x] 5.2 Tests in a new `frigate/test/test_watchdog.py`, with `restart_frigate` patched and the check run directly rather than through the 10 s loop. Verify:
  - a dead reader → one restart;
  - `busy` 641 s old at a busy timeout of 320 → restart, with the topic in the log;
  - `busy` 300 s old → no restart;
  - stop event set with a dead reader → no restart;
  - no `inter_process` passed → the existing detector checks only.
- [x] 5.3 Arm an exit deadline when the watchdog restarts for the request thread (added during implementation, see design Decision 3 and Risks). `FrigateApp.stop()` can wait forever on a main-process thread blocked in an untimed request to that thread, such as the stats emitter or the tracked object processor. A daemon timer logs an error and calls `os._exit(1)` if the process is still alive `RESTART_EXIT_DEADLINE` (60 s) later. The dead-detector restart arms nothing. Verify in `test_watchdog.py` with `_hard_exit` patched: a request-thread restart exits after a short deadline and logs, the timer is a daemon, and a dead-detector restart arms no timer.
- [x] 5.4 Arm the same deadline for every other restart and stop (added after `confirmed-ui-restart`, see design Decision 3 and Risks; extended by 10.4). `FrigateApp.stop()` first calls `FrigateWatchdog.arm_stop_deadline()`, which logs a warning and arms the timer if the reader thread is dead or `busy` is older than the busy timeout, unless the timer is already armed or the communicator is stopped. Verify in `test_watchdog.py`:
  - a dead reader at stop → warning, then the deadline exits and logs, and nothing calls `restart_frigate()`;
  - `busy` 300 s old at a busy timeout of 60 → armed, with the topic and duration in the warning;
  - idle, `busy` 30 s old, a stopped communicator, or no `inter_process` → nothing armed;
  - after a watchdog restart → the same timer, and no second warning;
  - `FrigateApp.stop()` calls it before it sets the stop event or touches anything else.

## 6. Rejected rows cannot stall the writer (`frigate/record/writer.py`)

- [x] 6.1 (Superseded by 10.1.) `insert_recording_rows` returns `rejected: True` for `peewee.IntegrityError` and `peewee.DataError`, and `False` otherwise. Verify in `TestInsertHandler` with a real DB: a `BEFORE INSERT` trigger with `RAISE(ABORT, 'x')` gives `rejected: True`, and a held `BEGIN IMMEDIATE` lock gives `rejected: False`. Use `from peewee import sqlite3` for the lock holder.
- [x] 6.2 (Superseded by 10.1: the writer no longer splits.) `RecordingsWriter`: on a rejected batch of n > 1 rows, send the first `n // 2` next with no backoff, and double the limit back toward `BATCH_ROWS` on success. A rejected single row is popped and logged at error level with its path, camera, variant and error. A rejection does not start or advance `failing_since`. Replies without `rejected` keep the current backoff.
- [x] 6.3 (Superseded by 10.1; the tests were rewritten there.) Tests in `frigate/test/test_record_writer.py`. Extend `FakeRequestor` with a set of rejected paths. Verify:
  - one bad row in 500 → 499 written, one error naming its path, under 25 sends;
  - two bad rows far apart → both isolated;
  - `database is locked` failures are retried whole with backoff and never split;
  - `has_room()` stays true while a bad row is being isolated.

## 7. Collision-proof ids (`frigate/record/util.py`)

- [x] 7.1 Change `recording_row` to a 12-character suffix. Verify with a test: for 64 rows with the same `start_time`, every id matches `^\d+\.\d+-[a-z0-9]{12}$`, is at most 30 characters, and all 64 are distinct.

## 8. Documentation

- [x] 8.1 Update `ra-docs/fork-changes.md`:
  - the new behavior;
  - the upstream files touched: `comms/inter_process.py`, `watchdog.py`, `app.py`, `video.py`;
  - the incident it explains (an id collision ended the request thread in v0.17.3);
  - the open items left: other handlers' own bugs, and stats exposure.
  Verify that the file names every touched module.
- [x] 8.2 Add `inter-process-requests` to the capability list in `CLAUDE.md` ("OpenSpec: the fork's behavior contract"). No API changes, so the client guides stay as they are.

## 9. Integration checks

- [x] 9.1 Full backend suite in Docker over the bind mount, with `pysqlite3-binary` installed. The only allowed failures are the four `TestGo2rtcStreamAccess` errors.
- [x] 9.2 Run `ruff check --select E4,E7,E9,F,I --ignore E501,E711,E712 frigate/` and `ruff format --check` on the touched files; both must be clean. Run `openspec validate --all --strict`: the change must pass, and specs that already failed on the base commit for long requirement text must not get new failures.
- [x] 9.3 Fault injection in a dev container with at least two cameras recording:
  - hold `BEGIN IMMEDIATE` on `frigate.db` for 2 min: segments keep moving, the writer warns, then recovers, and no row is lost;
  - add the `RAISE(ABORT)` trigger for one camera's paths: the other cameras' rows keep landing, one error per rejected row, and the mover never holds;
  - patch a handler to raise: the request thread survives, and the error is logged once per 5 min;
  - kill the request thread in a running container with a restart policy: the watchdog logs, Frigate restarts, and the container is back and recording within 3 minutes;
  - restart from the UI's endpoint while one request has run past the busy timeout (5.4): Frigate exits within 60 s of starting to stop, and is back and recording.

  Run on 2026-11-12 with this worktree's Frigate in the `frigate-test-deps` image plus Debian
  ffmpeg 7.1: two cameras, each recording `main` and `sub` from looped test files. No detector
  model is in that image, so the detector process was stubbed (detection is off on both
  cameras); the embeddings process could not load `vec0.so`, which does not touch recording.
  Results: a 2 min lock gave one writer warning, recovery after 59 s, and rows equal to files
  afterwards, with segments moving throughout (5 → 15 files per stream during the lock). The
  cam1 trigger gave 14 rejected rows and 14 errors, files kept, cam2 rows landing throughout,
  no "queue is full" error, and cam1 rows landing again once dropped. 25 requests whose real
  handler raises (`update_event_description` for a missing event) all got `[]`, one traceback
  was logged, and the region-grid request after them was answered.

  Killed request thread, run the same day in the same container with `--restart always`.
  A harness-only topic held the thread busy for 20 s and then raised `SystemExit`, so the
  stats emitter (`stats_interval: 15`) was blocked on it, as on a busy device. Without
  5.3, `stop()` never returned from `stats_emitter.join()`: no restart and no new segment
  in 150 s (s6 would have killed it at 120 s). With 5.3, the thread died at 10:06:28, the
  watchdog logged at 10:06:38, the deadline exited at 10:07:38, the container restarted,
  and the first new segment landed at 10:07:51, 83 s after the thread died. Reconcile
  recovered the 12 rows the writer could not drain, and rows equal files afterwards.

  UI restart with a stuck request, run on 2026-11-16 in the same container (2 cameras,
  busy timeout 60 s). A harness-only topic held the request thread for 400 s, and 75 s in,
  `POST /restart` on port 5001 (the UI's `POST /api/restart` behind nginx) restarted
  Frigate. With 5.4, `stop()` began at 14:36:07 and warned that the request had run
  77 s. The deadline exited at 14:37:07, the container restarted, and the first new
  segment landed at 14:37:19, 73 s after the restart. Without 5.4 (the call removed from
  `stop()`), `stop()` began at 14:39:59 and the process was still alive 150 s later, with
  no new segment.
- [ ] 9.4 (Left open at archive on 2026-11-17 with the user's OK; done on the device.) On a field device (device-only, may stay open at archive with the user's OK): after a week on the new image, confirm there is no `restart` from the request-thread watchdog. Measure the old id exposure with `SELECT SUM(c*(c-1)/2.0) FROM (SELECT COUNT(*) c FROM recordings GROUP BY start_time);` divided by retained days and by 36^6, and record it in `ra-docs/fork-changes.md`.

## 10. Code-review fixes (2026-11-17)

Review of PR 8 found regressions and gaps; see design Decisions 3, 4 and 5 ("refined after
code review").

- [x] 10.1 Rejected rows are found by the handler (`frigate/record/writer.py`). When the batch insert raises `IntegrityError`/`DataError`, `insert_recording_rows` inserts the rows one at a time (`_insert_each()`) and replies `{"ok": true, "count", "rejected": [{path, camera, variant, error}]}`; a non-rejection failure in that pass is `{"ok": false}` and the batch is retried whole. `note_late_rows` gets the stored rows only. `RejectedRowsLog` logs rejected rows once per `REJECTED_LOG_INTERVAL` (5 min) with a count; the writer and the mover each keep one. The writer's split (`suspect`, `batch_limit`, `_isolate`) is removed. Verify in `test_record_writer.py`: a trigger refusing one row stores the others and lists it; late rows are noted for stored rows only; one bad row in 500 → 499 written in one request, one error; 600 rejected rows → one error, and the next window reports the count; locked batches are retried whole; `has_room()` stays true.
- [x] 10.2 The mover's blocking request keeps the rows it could not write (`frigate/record/maintainer.py`, `frigate/record/record.py`). `send_rows()` sends kept rows first; the first batch with no reply, `[]` or `ok: false` ends the pass and keeps it plus every later row; at most `db_write_queue` are kept, the oldest dropped with one error per outage; warning at the start, error after 60 s, info on recovery; no request for a pass with no rows. `report_unsent_rows()` logs kept rows at shutdown, and `RecordProcess` then writes no clean-shutdown marker. Verify in `test_maintainer.py`: no reply → rows kept, next pass sends them with its own and logs recovery; 1200 rows and no reply → one request, all kept; a failure after a written batch keeps only the rest; the cap drops the oldest with one error; unsent rows at shutdown are reported; success logs nothing; no rows → no request; rejected rows in a reply are logged, not kept. The mover tests' requestor now answers like the main process.
- [x] 10.3 The timed requestor reconnects on any ZMQ error, not only a timeout, and logs one warning per `ERROR_LOG_INTERVAL` with a count (`frigate/comms/inter_process.py`). The writer no longer replaces its requestor (`_replace_requestor` removed). Verify in `test_inter_process.py`: three timeouts → one warning, and the next window reports two; a closed socket → `""`, then the next request is answered. In `test_record_writer.py`: a missing reply is retried on the same requestor.
- [x] 10.4 The stop deadline watches the whole stop (`frigate/watchdog.py`). `arm_stop_deadline()` checks at once, then a daemon `stop_deadline_monitor` checks every `STOP_CHECK_INTERVAL` (1 s) until `stop()` has stopped the reader. `_exit_now()` exits only if `request_thread_problem()` still reports a dead or stuck thread; otherwise it logs, clears the timer, and a later wedge can re-arm it. Verify in `test_watchdog.py`: a request that crosses the busy timeout during the stop arms it; a reader that dies during the stop arms it; a request that finishes before the deadline → no exit; a reader stopped by `stop()` → no exit, and the monitor ends. The earlier 5.3/5.4 tests still pass.
- [x] 10.5 `busy_timeout(config)` replaces the inline formulas in `frigate/record/record.py` and `frigate/embeddings/maintainer.py`.
- [x] 10.6 The region-grid warning in `frigate/video.py` uses lazy `%s` logging.
- [x] 10.7 Full backend suite in Docker with `pysqlite3-binary`: 450 tests, only the four `TestGo2rtcStreamAccess` errors. `ruff check --select E4,E7,E9,F,I --ignore E501,E711,E712 frigate/` and `ruff format --check` on the touched files are clean; mypy reports nothing in them. `openspec validate ipc-request-resilience --strict` passes.
- [x] 10.8 Re-run two 9.3 cases on this code in the dev container: the `RAISE(ABORT)` trigger for one camera's paths (expect its rows dropped, the other camera's rows landing, and one error per 5 minutes with a count rather than one per row), and a UI restart 20 s into a request held for 400 s on a 2-camera device (expect an exit about 60 s after the request passes the 60 s busy timeout, then recording again).

  Run on 2026-11-17 at `ae2d0d7d1`, with the 9.3 harness (same image, two dual-stream
  cameras, stubbed detector). Trigger held 330 s: cam1's rows stayed at 5 while its files
  grew to 38 per stream (66 files without a row, all cam1), cam2's rows matched its files
  throughout (20/20, 38/38), and cam1's rows landed again once the trigger was dropped.
  Two errors in total: at 10:24:25, 8 s after the trigger ("1 more rows in the same
  batch"), and at 10:29:35 ("1 more rows in the same batch and 60 more since the last
  report"). No "queue is full" error and no write-failure warning.

  UI restart: the hold began at 10:31:32 and `POST /restart` came at 10:31:52; `stop()`
  began at 10:31:54. At 10:32:33 the stop monitor warned that the request had run 61 s
  and armed the deadline; at 10:33:33 Frigate exited ("running for 121s"). The container
  restarted 102 s after the UI restart, Frigate started at 10:33:45, and new segments
  landed from 10:33:46. The same run at `323423a5b` (before the review fixes) armed
  nothing at stop, since the request had run only 20 s: the process was still alive 201 s
  after the UI restart, with no new segment.

## Workflow follow-up

- Archive with `/opsx:archive` once 9.1–9.3 and 10.8 pass. Ask the user before archiving while 9.4 is open.
- Before archiving, confirm that `ra-docs/fork-changes.md` reflects the change.
