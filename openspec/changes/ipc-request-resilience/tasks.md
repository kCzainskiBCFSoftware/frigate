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
- [x] 3.3 In the mover's blocking fallback, log an error with the batch size and the first and last path when the reply is not a dict (no reply), as well as on `ok: false`. Verify with a test in `frigate/test/test_maintainer.py` that uses a requestor returning `""`.
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

## 6. Rejected rows cannot stall the writer (`frigate/record/writer.py`)

- [x] 6.1 `insert_recording_rows` returns `rejected: True` for `peewee.IntegrityError` and `peewee.DataError`, and `False` otherwise. Verify in `TestInsertHandler` with a real DB: a `BEFORE INSERT` trigger with `RAISE(ABORT, 'x')` gives `rejected: True`, and a held `BEGIN IMMEDIATE` lock gives `rejected: False`. Use `from peewee import sqlite3` for the lock holder.
- [x] 6.2 `RecordingsWriter`: on a rejected batch of n > 1 rows, send the first `n // 2` next with no backoff, and double the limit back toward `BATCH_ROWS` on success. A rejected single row is popped and logged at error level with its path, camera, variant and error. A rejection does not start or advance `failing_since`. Replies without `rejected` keep the current backoff.
- [x] 6.3 Tests in `frigate/test/test_record_writer.py`. Extend `FakeRequestor` with a set of rejected paths. Verify:
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
  - patch a handler to raise: the request thread survives, and the error is logged once per 5 min.

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
- [ ] 9.4 On a field device (device-only, may stay open at archive with the user's OK): after a week on the new image, confirm there is no `restart` from the request-thread watchdog. Measure the old id exposure with `SELECT SUM(c*(c-1)/2.0) FROM (SELECT COUNT(*) c FROM recordings GROUP BY start_time);` divided by retained days and by 36^6, and record it in `ra-docs/fork-changes.md`.

## Workflow follow-up

- Archive with `/opsx:archive` once 9.1–9.3 pass. Ask the user before archiving while 9.4 is open.
- Before archiving, confirm that `ra-docs/fork-changes.md` reflects the change.
