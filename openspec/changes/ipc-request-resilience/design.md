# Design

## Context

See `proposal.md` (Why) for the incident and why v0.17.4 does not close it. The
requirements are in `specs/inter-process-requests` and `specs/recording-db-writes`.

What the design builds on, all on `dev-fork` at v0.17.4:

| Piece | Where | Relevant behaviour today |
|---|---|---|
| REP server | `InterProcessCommunicator.read()`, `frigate/comms/inter_process.py` | one thread for all of Frigate. It catches only `zmq.ZMQError`, so an exception from `self._dispatcher(...)` **or** from `send_json(response)` ends the thread with the REP socket still waiting to send a reply. |
| REQ client | `InterProcessRequestor.send_data`, same file | No timeout. Returns `""` only on `zmq.ZMQError`, which a lost reply never raises. 14 call sites; only three read the reply: the writer, the mover fallback, and the region grid (`frigate/video.py:913`). |
| Dispatcher | `Dispatcher._receive`, `frigate/comms/dispatcher.py` | Shared with MQTT and WebSocket. Handlers raise freely (`Event.get`, `Previews.insert`, `ReviewSegment` upserts, `KeyError` on a removed camera). |
| Row handler | `insert_recording_rows`, `frigate/record/writer.py` | `INSERT … ON CONFLICT DO NOTHING`. Never raises; returns `{"ok": False, "error"}`. |
| Writer | `RecordingsWriter`, same file | Retries every failure with backoff 1→30 s. Replaces its requestor when `send_data` returns a non-dict. |
| Row builder | `recording_row`, `frigate/record/util.py` | `id = f"{start_time}-{6 × [a-z0-9]}"`. |
| Main DB | `FrigateApp.bind_database`, `frigate/app.py` | `SqliteVecQueueDatabase`, busy timeout `max(60, 10 × enabled cameras)`, 320 s on the 32-camera target. |
| Watchdog | `FrigateWatchdog`, `frigate/watchdog.py` | Main process, 10 s loop. Calls `restart_frigate()` when a detector process is dead. Started in `start_watchdog()` after the communicator exists. Joined in `stop()` before `inter_process_communicator.stop()`. |

The `recordings_path` unique index (migration 003) is still present after 034, so
`DO NOTHING` absorbs a re-sent batch through either the path or the id.

## Goals / Non-Goals

**Goals:**
- No exception, in any handler or in encoding a reply, can end the REP thread or leave a
  request unanswered.
- The recording path recovers on its own from a main process that stops answering,
  whether it is dead, stuck, or just slow.
- No bad row and no id collision can stop recording.

**Non-Goals:**
- Making other request handlers correct, for example `Event.get` on a deleted event. They
  only stop being fatal. Their bugs are logged with a topic and can be fixed one by one
  later.
- Timeouts for the other 11 requestor call sites. Their callers treat `""` or a hang
  differently, and the watchdog restart covers a stuck main process for all of them.
- Guarding the MQTT and WebSocket paths into the dispatcher. Their threads already
  survive a handler exception (paho and the websocket server catch per message), and
  they do not block other processes.
- Restarting the REP thread in place. See Decision 3.

## Decisions

### 1. Guard at the transport, reply with `[]`

The `try/except Exception` goes in `InterProcessCommunicator.read()`, around both the
dispatch and the `send_json`, not in `Dispatcher._receive`. Only the transport can
guarantee that every received request gets exactly one reply, which a REP socket requires
before it can receive again, and only the transport covers a reply that cannot be encoded.

The structure per message:

1. `recv_json`.
2. Set `busy = (topic, monotonic())`.
3. Call the handler. If it raises, log it and set `response = None`.
4. `send_json(response if response is not None else [])`. If that raises (other than a
   ZMQ error), `send_json([])`.
5. Set `busy = None`.

An outer `try/except Exception` around the loop body logs and continues. It never
breaks; the existing `zmq.ZMQError → break` of the inner drain loop stays as it is.

**Reply value on failure: `[]`**, the value the server already sends for every handler
that returns `None`. Fire-and-forget callers ignore it. The writer and the mover fallback
see a non-dict, which they treat as "no reply": the writer retries the batch and the
mover keeps it for its next pass, and the retry is idempotent. The region-grid caller is the only one that indexes the reply, so it
gets the guard in Decision 6. *Alternative:* an `{"ok": false, "error": …}` dict. It is
more informative, but `video.py` would index a dict as a grid, and every fire-and-forget
caller would need auditing for a new reply shape. Rejected.

**Logging:** one `logger.error(..., exc_info=True)` per topic per 5 minutes. Failures
inside the window are counted and reported with the next logged traceback for that
topic. This is a dict of `topic → (last_logged, suppressed)` owned by the REP thread, so
no lock is needed. A handler that fails on every request (e.g. camera activity at 1 Hz)
would otherwise flood the log, which on the target device is on the same NVMe as the DB.

### 2. Heartbeat as one tuple attribute

`busy: Optional[tuple[str, float]]` is assigned as a whole tuple by the REP thread, so the
watchdog's read is atomic under the GIL and topic and start time are always consistent.
Nothing else is shared. No `last_loop` timestamp is needed: a dead thread is
`not reader_thread.is_alive()`, and a stuck one is `busy` older than the threshold. An
idle thread is neither.

### 3. Watchdog restarts Frigate rather than the thread

The user chose to restart Frigate when a request runs too long. Recovering in place is
not possible:
- Python cannot interrupt a thread blocked inside SQLite or a handler.
- A thread that died mid-request left the REP socket owing a reply.
- Re-binding `ipc:///tmp/cache/comms` with a new socket leaves every untimed requester
  (11 call sites) waiting forever on a reply that will never come.

A full restart through `restart_frigate()` is the only action that recovers every
process. It is the same action the watchdog already takes for a dead detector.

- **Thresholds:** dead → the next 10 s check. Stuck → `max(600, 2 × busy_timeout)` (640 s
  at 32 cameras). A handler can legitimately wait one busy timeout in SQLite, plus up to
  one more behind the request queued ahead of it. At 64 streams the writer queue (8192
  rows) fills in about 21 minutes. Restarting at about 11 minutes means footage keeps
  moving to disk until the restart and nothing is discarded.
- **No false positives at stop:** `FrigateWatchdog` exits on `stop_event`, and
  `FrigateApp.stop()` joins it before stopping the communicator. The check also skips
  when the communicator's own `stop_event` is set.
- **Wiring:** `FrigateWatchdog(detectors, stop_event, inter_process=…, busy_timeout=…)`
  with optional arguments, so its upstream signature still works.
- **Restart loop:** if the database stays locked by something outside Frigate, each run
  restarts after about 11 minutes. Each restart is logged with its topic. That is still
  better than one silent permanent stop. No cap is added; the dead-detector rule has none.
- **Exit deadline (added during implementation):** `restart_frigate()` only starts a
  graceful stop, and `FrigateApp.stop()` joins main-process threads that may be blocked
  in an untimed request to the dead or stuck thread: the stats emitter, the tracked
  object processor (`UPDATE_CAMERA_ACTIVITY`), and with a stuck thread the reader's own
  join. The watchdog therefore arms a daemon timer when it restarts for the request
  thread. If the process is still alive `RESTART_EXIT_DEADLINE` (60 s) later, the timer
  logs an error and calls `os._exit(1)`. 60 s is half of s6's `timeout-kill` (120 s) for
  the frigate service. The dead-detector restart arms no timer itself. *Alternative:*
  bounded joins in `stop()`. Rejected: every blocked thread is non-daemon, so interpreter
  exit would wait on them anyway, and the stuck reader's join is one more hang.
- **Every other restart (added after `confirmed-ui-restart`):** `FrigateApp.stop()`
  first calls `FrigateWatchdog.arm_stop_deadline()`. It arms the same timer if the reader
  thread is dead, or if one request has run longer than the busy timeout. The UI, the
  API, MQTT, a dead detector and a plain stop all end in `stop()`, so one call covers them.
  The threshold is the busy timeout, not the watchdog's `max(600, 2 × busy_timeout)`.
  Frigate is already stopping, and a request that waits on the database gives up after
  the busy timeout, so one running longer is not about to answer. The timer is armed
  once: a watchdog restart has already armed it before its own stop begins.
- **The whole stop is watched (refined after code review):** a single check when
  `stop()` begins missed a request that was still within the busy timeout then and never
  finished, and a reader that died during the stop; both hung until s6's kill. So
  `arm_stop_deadline()` checks at once, then a daemon `stop_deadline_monitor` thread
  checks every `STOP_CHECK_INTERVAL` (1 s) until `stop()` has stopped the reader itself
  (the communicator's stop event is set and the reader has ended). A stop whose thread
  never meets either condition arms nothing, so a slow but healthy stop is never cut
  short.
- **The timer checks again before it exits:** `_exit_now()` exits only if the thread is
  still dead or still busy past the busy timeout (`request_thread_problem()`). If the
  request finished, or `stop()` has stopped the thread, nothing waits on it any more:
  the timer logs that and stands down, and a later wedge can arm it again. This keeps a
  slow request that finishes just after the busy timeout from ending a healthy stop, and
  a stop that completed from ending with `os._exit(1)` during interpreter teardown.
  *Alternative:* cancel the timer at the end of `stop()`. Rejected: it leaves the race
  with a timer firing just before, and does nothing for a request that finished
  mid-stop.

### 4. Requestor timeout: opt-in, reconnect inside the requestor

The constructor becomes `InterProcessRequestor(timeout: Optional[float] = None)`. With a
timeout:
- set `RCVTIMEO` and `SNDTIMEO` (in ms) on the REQ socket;
- on any `zmq.ZMQError` (a timeout raises `zmq.Again`), close it with `linger=0`, open
  and connect a fresh REQ socket, and return `""`;
- log a warning with the topic at most once per `ERROR_LOG_INTERVAL` (5 min) per
  requestor, with the number of failures not logged since. A writer retrying through a
  long outage would otherwise log one warning per retry.

A REQ socket that missed its reply, or failed mid-request, refuses every later send
(EFSM), which is why the socket is recreated inside the requestor. A reconnect that
fails leaves a closed socket, whose next send fails with a ZMQ error and reconnects
again. *Refined after code review:* the writer's `_replace_requestor`, which built a
second requestor after the first had already reconnected, is removed; reconnecting is
the requestor's job alone.

*Alternative:* `ZMQ_REQ_RELAXED` with `ZMQ_REQ_CORRELATE`. It avoids the new socket, but
it changes how stale replies are matched, and every reconnect path already exists.
Rejected.

**Value:** `busy_timeout + 30 s` (350 s at 32 cameras). It is used by the writer, through
`requestor_factory=lambda: InterProcessRequestor(timeout=…)` in
`frigate/record/record.py`, and by the mover's own requestor, which is used only for the
blocking fallback. When the timeout is shorter than a real stall, the batch is sent twice,
and `DO NOTHING` makes the second send a no-op. The busy-timeout formula moves from
inline `frigate/app.py` code into `frigate/db/maintenance.py` as
`busy_timeout(config) -> int`. It is used by `bind_database`, by the recording and
embeddings processes' own connections (which had their own inline copies counting
`enabled` cameras, the same set at start), by the row-request timeout, and by the
watchdog, so the values cannot drift apart.

**Mover fallback (`maintainer.py`, blocking path, refined after code review):** this path
runs when `async_db_writes` is off, upstream's mode, or when the writer thread has died.
The first draft logged and dropped a batch that got no reply. That lost rows upstream
would have written, since upstream waits for the reply: the files stayed on disk with no
row, never listed, and never expired by retention while `sync_recordings` is off (its
default). It also kept sending the pass's later batches, each waiting a full timeout. Now
`send_rows()` keeps every row of the first batch that is not written (no reply, a ZMQ
error, `[]` or `ok: false`) and every later row in `unsent_rows`, stops that pass's
requests, and sends the kept rows first on the next pass. A main process that stops
answering therefore costs one request timeout per pass. The kept rows are capped at
`record.pipeline.db_write_queue`; past it the oldest are dropped with one error per
outage naming their paths. Logging follows the writer: a warning when failures start, an
error after `FAILING_ERROR_AFTER` (60 s), an info message on recovery. A pass with no
rows sends no request; upstream sent an empty one. At shutdown, `report_unsent_rows()`
logs what is still kept, and `RecordProcess` then writes no clean-shutdown marker, so
reconcile finds those files at the next start. *Alternative:* hold segments in the cache
while rows are unsent, as the writer's full queue does. Rejected: the blocking path has
no queue to measure, and moving the files keeps them safe from the cache limits.

### 5. Rejected vs. retried failures; rejected rows found by the handler

A failure caused by the rows' values is a `peewee.IntegrityError` or `peewee.DataError`:
NOT NULL, CHECK, a trigger's `RAISE(ABORT)`, and datatype errors. Everything else is
retried whole, as today:
- `OperationalError`: locked, busy, I/O error, disk full, or a missing column after a
  bad migration;
- `ResultTimeout`;
- `DatabaseError` from corruption;
- programming errors.

These are not row-specific. Splitting would drop every row one by one while the files
keep accumulating with no rows. Holding them in the queue keeps them recoverable once the
cause is fixed.

**Handler (refined after code review):** when the batch insert raises `IntegrityError` or
`DataError`, `insert_recording_rows` inserts the same rows one at a time in the same
request (`_insert_each()`), each with `ON CONFLICT DO NOTHING`. It replies
`{"ok": True, "count": <stored>, "rejected": [{path, camera, variant, error}, ...]}`, with
`rejected` present only when a row was refused, and calls `note_late_rows` with the stored
rows only. If a row fails for any other reason during that pass, the reply is
`{"ok": False, "error": ...}` and the caller retries the whole batch; rows already stored
are ignored then. Any other failure of the batch insert is `{"ok": False, "error": ...}`
too. The per-row pass runs only on a rejection, so a real outage is never split.

**Writer and mover fallback:** a successful reply's `rejected` rows are dropped, since
the rest of the batch is stored, and their files stay in recordings. `RejectedRowsLog`
logs them at error level at most once per `REJECTED_LOG_INTERVAL` (5 min): the first
row's `path`, `camera`, `variant` and error, plus how many rows were rejected since the
previous report (each row also at debug). A constraint that refuses every row therefore
logs a line per 5 minutes, not one per segment. `failing_since` and its warning and error
logs are not triggered by a rejection, which is not an outage. The writer sends
`BATCH_ROWS` at a time as in v0.17.4.

*Alternative:* split rejected batches in the writer, a binary search over the rows known
to hold the bad one. That was the first implementation. Rejected after code review: it
cost up to 20 requests for one bad row in 500, kept a state machine (`suspect`,
`batch_limit`) across writer iterations, and did nothing for the mover's blocking path,
which dropped the whole batch. Isolating in the handler costs one request, and up to one
single-row insert per row of that batch on the main process's request thread.

*Alternative:* insert row by row after any failure. It would split and drop rows during
real outages. Rejected.

### 6. Region grid keeps the current grid

In `process_frames` (`frigate/video.py`), the new grid is assigned only when the reply is
a non-empty list. Otherwise the camera keeps the current grid, and `next_region_update`
moves on to the next day as today. This also fixes the existing upstream path where a
ZMQ error returned `""` and the next `region_grid[x][y]` raised `IndexError`.

### 7. Id suffix of 12 characters

`recording_row` uses `k=12`. The longest id is `"1760781080.0-" + 12` = 25 characters,
under `VARCHAR(30)`, and SQLite does not enforce the length anyway. With 64 rows in one
second the collision odds are 2016 / 36^12 ≈ 4e-16 per second, or about 4e-12 per day.
This holds for both variants. `main` and `sub` of one camera usually open their segments
in the same second, so they are the most likely colliding pair today.

*Alternatives:*
- An id derived from the path. It is idempotent by construction, but it changes id
  semantics for a file that is re-moved after deletion.
- Retry with a new id on conflict. That needs a conflict target and a second round trip.

Both are rejected. `DO NOTHING` stays untargeted, because it still absorbs re-sent
batches.

The variant dimension does not otherwise touch this design. Rows of both variants share
one queue and one batch, and finding, logging and dropping a rejected row are per row, so
the log line names the variant.

## Risks / Trade-offs

- [A handler bug is now logged instead of fatal, so it can go unnoticed] → it is logged at
  error level with its topic every 5 minutes, which is louder than one traceback followed
  by silence.
- [Restart loop while something outside Frigate holds the DB lock] → one restart per
  ~11 min, each logged with its topic. Recording works between restarts, and an
  operator sees the cause.
- [A watchdog restart hangs in `FrigateApp.stop()`: a main-process thread blocked in an
  untimed request to the dead or stuck thread is never joined. Seen on 0.17.3 as about
  2 minutes with no API and no logs after a UI restart, until s6 killed Frigate after its
  120 s `timeout-kill`] → the exit deadline in Decision 3 ends the process 60 s after a
  request-thread restart. Recovery takes up to 10 s to detect, up to 60 s to exit, then
  startup. It reproduced in a dev container with the stats emitter blocked: without the
  deadline, `stop()` never finished; with it, recording resumed about 83 s after the
  thread died.
- [A restart from the UI, the API, MQTT or a dead detector while the request thread is
  dead or stuck] → its `stop()` arms the exit deadline once the thread is dead or one
  request has run longer than the busy timeout, whether at the start of the stop or
  during it (Decision 3). Since
  `confirmed-ui-restart`, the UI's Restart sends `POST /api/restart`, which calls
  `restart_frigate()` 1 s later, and the dialog waits for Frigate to answer again.
  In the dev container, a UI restart 75 s into a stuck request was back and recording
  73 s later. Without the call in `stop()`, the process was still alive after 150 s.
  Arming the deadline on every restart was rejected because it would cut short a slow
  but healthy stop.
- [A request that gets stuck early in a stop arms the deadline only once it passes the
  busy timeout] → that stop exits up to busy timeout + 60 s after the request began,
  which can still exceed s6's 120 s `timeout-kill` on a large device; s6 then ends it, as
  upstream. A stop that hangs for a reason other than the request thread is not bounded
  here either. The UI shows its slow-start notice only after 6 minutes down, which
  covers both.
- [A slow request that finishes after the deadline is armed] → the timer checks again
  before it exits and stands down, so the stop goes on. A stop whose request thread
  keeps answering slowly, each request past the busy timeout, re-arms after each one,
  and s6's kill remains the bound.
- [The handler's row-by-row pass holds the request thread] → it runs only for a batch
  the database rejects: up to `BATCH_ROWS` single-row inserts, each through the write
  queue, typically well under a few seconds. Other processes' requests wait that long.
- [Rows the blocking request keeps live only in the recording process's memory] → a
  crash loses them, but the shutdown is then unclean and reconcile recovers their files
  within `reconcile_window` (2 h). A clean stop with kept rows is treated as unclean for
  the same reason.
- [The restart relies on a supervisor bringing Frigate back] → under s6 the whole
  container stops, and only the container's restart policy (`restart: always` or
  `unless-stopped`) starts it again. The dead-detector restart has the same dependency.
  A deployment without a restart policy stays down after any watchdog restart.
- [The exit deadline skips the rest of `stop()`] → the writer's drain already failed,
  since its requests go to the same thread. The next start finds no clean-shutdown
  marker, and reconcile recovers the rows; in the dev container all 12 rows that were
  queued at the exit were recovered. Shared memory left behind is reused at the next
  start (`start_detectors` handles `FileExistsError`), and a restarted container
  started cleanly.
- [A dropped rejected row leaves a file without a row; `sync_recordings` deletes it after
  the 2 h grace if sync is on] → it is logged with its path. Such a row is a data bug
  that blocked every camera before this change.
- [A spurious timeout under heavy contention makes the main thread process the batch
  twice] → the second insert is a `DO NOTHING` no-op. The 350 s timeout makes this rare.
- [`[]` for a failed request reaches a caller that treats it as success] → only the
  three callers that read replies matter, and each is covered: writer, mover fallback,
  region grid.
- [The review fixes have unit tests but no fault injection yet] → the 9.3 runs
  exercised the earlier code. Task 10.8 re-runs the two cases whose behavior changed.

## Migration Plan

No schema migration and no config. New ids apply to rows written after the upgrade, and
old ids stay valid. Rollback is the previous image. Rows written by the new version have
longer ids, which the old code treats as opaque strings.

## Open Questions

- Should the watchdog's restarts and the per-topic handler failure counts be exposed in
  `/api/stats` so fleet monitoring can alert on them? That would be additive and could
  follow later, without changing these specs.
