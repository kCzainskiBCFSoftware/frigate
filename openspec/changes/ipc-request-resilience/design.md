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
see a non-dict, which they already treat as "no reply": they reconnect and retry, and the
retry is idempotent. The region-grid caller is the only one that indexes the reply, so it
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
  the frigate service. The dead-detector restart arms no timer. *Alternative:* bounded
  joins in `stop()`. Rejected: every blocked thread is non-daemon, so interpreter exit
  would wait on them anyway, and the stuck reader's join is one more hang.

### 4. Requestor timeout: opt-in, reconnect inside the requestor

The constructor becomes `InterProcessRequestor(timeout: Optional[float] = None)`. With a
timeout:
- set `RCVTIMEO` and `SNDTIMEO` (in ms) on the REQ socket;
- on `zmq.Again`, close it with `linger=0`, open and connect a fresh REQ socket, log a
  warning with the topic, and return `""`.

A REQ socket that missed its reply refuses every later send (EFSM), which is why the
socket is recreated inside the requestor. The writer's `_replace_requestor` stays as a
second line of defence.

*Alternative:* `ZMQ_REQ_RELAXED` with `ZMQ_REQ_CORRELATE`. It avoids the new socket, but
it changes how stale replies are matched, and every reconnect path already exists.
Rejected.

**Value:** `busy_timeout + 30 s` (350 s at 32 cameras). It is used by the writer, through
`requestor_factory=lambda: InterProcessRequestor(timeout=…)` in
`frigate/record/record.py`, and by the mover's own requestor, which is used only for the
blocking fallback. When the timeout is shorter than a real stall, the batch is sent twice,
and `DO NOTHING` makes the second send a no-op. The busy-timeout formula moves from
inline `frigate/app.py` code into `frigate/db/maintenance.py` as
`busy_timeout(config) -> int`, used by `bind_database`, the recording process, and the
watchdog, so the three values cannot drift apart.

**Mover fallback (`maintainer.py`, blocking path):** a non-dict reply is now logged as an
error with the batch size and its first and last path. Rows of that batch are not
retried; this is the degraded path, used only when the writer thread has died. The files
stay on disk and the existing reconcile and sync rules apply to them.

### 5. Rejected vs. retried failures; splitting in the writer

`insert_recording_rows` returns `{"ok": False, "error": str, "rejected": bool}`, where
`rejected` is `isinstance(e, (peewee.IntegrityError, peewee.DataError))`. That covers
NOT NULL, CHECK, a trigger's `RAISE(ABORT)`, and datatype errors: failures caused by the
rows' values. Everything else is retried whole, as today:
- `OperationalError`: locked, busy, I/O error, disk full, or a missing column after a
  bad migration;
- `ResultTimeout`;
- `DatabaseError` from corruption;
- programming errors.

These are not row-specific. Splitting would drop every row one by one while the files
keep accumulating with no rows. Holding them in the queue keeps them recoverable once the
cause is fixed. A reply without `rejected` (an older main process, or the `[]` from
Decision 1) is treated as not rejected.

**Writer:** on a rejected batch of n > 1 rows, the next send takes the first `n // 2`
rows, with no backoff because the database is healthy. A rejected batch of one row is
popped and logged at error level with `path`, `camera`, `variant` and the error.
`failing_since` and its warning and error logs are not triggered by a rejection, which is
not an outage.

*Refined during implementation:* the writer remembers how many rows at the front of the
queue are known to hold the rejected row (`suspect`). While that range is open, a
successful send does not double the limit; the next send takes half of the rest of the
range, which is a plain binary search. Once the row is dropped, the limit starts at 1 and
doubles back toward `BATCH_ROWS` on each success. Doubling on every success, as first
drafted, re-sends the known-bad range and costs up to 33 requests for one bad row in 500,
over the spec test's bound of 25. The refined rule costs at most 20 for one bad row, and
about one request per row when every row is bad.

*Alternative:* insert row by row after any failure. That is simpler, but it costs 500
requests per bad batch, and it would split and drop rows during real outages. Rejected.

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
one queue and one batch, and splitting, logging and dropping are per row, so the log line
names the variant.

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

## Migration Plan

No schema migration and no config. New ids apply to rows written after the upgrade, and
old ids stay valid. Rollback is the previous image. Rows written by the new version have
longer ids, which the old code treats as opaque strings.

## Open Questions

- Should the watchdog's restarts and the per-topic handler failure counts be exposed in
  `/api/stats` so fleet monitoring can alert on them? That would be additive and could
  follow later, without changing these specs.
