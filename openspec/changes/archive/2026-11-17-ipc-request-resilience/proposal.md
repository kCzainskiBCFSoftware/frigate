# Proposal: one failed inter-process request must not stop recording

## Why

A device on v0.17.3 logged a single `peewee.IntegrityError: UNIQUE constraint failed:
recordings.id` from the main process's `Thread-7 (read)` and then stopped recording,
with no further log line. Two separate defects combined to cause it.

1. **Trigger: a recording id collision.** A segment row's id is
   `"<segment start, whole seconds>-<6 random [a-z0-9]>"`. Every stream whose segment
   opens in the same wall-clock second shares the prefix, so the 6-character suffix
   (36^6 ≈ 2.2e9 values) is the only thing that tells those rows apart. A 32-camera
   dual-stream device writes 64 rows every 10 s. That gives roughly one collision every
   4 months per device when segments cluster into the same second, which they do because
   all recorders start together, or one every 3 years when segments spread evenly. Across
   a fleet, collisions happen. No other path in v0.17.3 can produce a duplicate id: the
   mover is the only sender of segment rows, each row gets a fresh id, and nothing
   retries.
2. **Blast radius: the main process's only request thread dies.**
   `InterProcessCommunicator.read()` catches only ZMQ errors. When a request handler
   raises, the thread that answers **every** inter-process request in Frigate ends, and
   the request it was serving is never answered. `InterProcessRequestor.send_data` has no
   receive timeout. The recording process therefore waited forever, and so did every
   other process that sent a request afterwards: review segments, previews, camera
   activity, region grids. The database connection was healthy throughout.

v0.17.4 (`recording-stall-resilience`) already stops this exact traceback.
`INSERT_MANY_RECORDINGS` now inserts with `ON CONFLICT DO NOTHING` and returns
`{"ok": false}` instead of raising, and rows go through a background writer with retry.
The same outage can still happen in four ways:

- Every other handler can still raise and kill the thread. Examples are `Event.get` for
  an event deleted before its description arrived, a preview or review-segment write
  that outlasts the busy timeout, and a reply that cannot be serialized.
- The writer's "no reply → reconnect" requirement never triggers in production. Without
  a receive timeout, `send_data` blocks and never returns its "no reply" value. If the
  request thread dies, the writer hangs, its queue fills, and the mover holds segments
  until the cache limit discards them.
- An id collision is now silent footage loss. `ON CONFLICT DO NOTHING` drops the second
  segment's row, and its file is never listed.
- A row that fails for a deterministic reason is retried forever. The queue fills and
  the mover stops, so one bad row stops every camera.

## What Changes

- **The main-process request thread survives handler errors.** Each request is answered
  even when its handler raises or returns a value that cannot be serialized. The error
  is logged with its topic. The thread exits only when Frigate stops.
- **A request thread that dies or stays stuck restarts Frigate.** The main-process
  watchdog checks the thread every 10 s. If it has died, or a single request has been
  running longer than `max(600 s, 2 × the database busy timeout)`, the watchdog logs an
  error naming the topic and restarts Frigate. This matches the existing rule for a
  dead detector. The v0.17.4 writer drain and unclean-shutdown recovery already make
  this restart safe for queued rows.
- **A restart or stop cannot hang on that thread.** While Frigate stops, it watches the
  request thread until it has stopped it. Once the thread is dead, or one request has
  run past the busy timeout, Frigate exits 60 s later if that still holds, so the
  container's restart policy brings it back. A slow request that finishes in time, or a
  healthy stop, is never cut short.
- **Segment-row requests time out.** The recording writer and the mover's blocking
  fallback use a receive timeout equal to the database busy timeout plus 30 s. After a
  timeout, or any connection error, the requestor reconnects, so the existing writer
  retry now works as specified. The blocking fallback keeps the rows it could not write
  and sends them first on its next pass, instead of waiting forever as upstream does.
  Every other requestor keeps upstream's untimed request.
- **Recording ids cannot collide in practice.** The random suffix grows from 6 to 12
  characters (at most 25 characters in total, under the column's 30). The format and
  existing rows are unchanged.
- **One bad row cannot stop the writer.** When the database rejects a row's values, for
  example through a constraint or a datatype failure, the main process finds that row in
  the same request, stores the rest of the batch, and lists the rejected rows in its
  reply. They are dropped, their files stay on disk, and they are logged with the first
  row's path and error at most once every 5 minutes, with a count. Every other failure
  (locked, busy, I/O, timeout, corruption) keeps today's whole-batch retry with backoff.
- **A failed region-grid request keeps the current grid.** A camera's frame processor
  now ignores an empty or missing reply. Without this, the empty answer that a failed
  request now gets would make it index an empty grid.

No API, config or database schema change. Nothing here is **BREAKING**.

## Capabilities

### New Capabilities
- `inter-process-requests`: the main process keeps answering other processes' requests
  when a handler fails, timed requests recover from a missing reply, and a dead or stuck
  request thread restarts Frigate, and a restart or stop cannot hang on that thread.

### Modified Capabilities
- `recording-db-writes`: a send with no reply is detected by a timeout, the blocking
  fallback keeps the rows it could not write for its next pass, a deterministic row
  failure is isolated and dropped with an error instead of retried forever (this relaxes
  "the writer SHALL NOT drop rows"), and segment row ids are unique across streams that
  share a start second.

## Impact

- **Code:**
  - `frigate/comms/inter_process.py` (upstream): handler guard, reply fallback, heartbeat,
    optional requestor timeout.
  - `frigate/watchdog.py` (upstream): request-thread check, exit deadline, stop monitor.
  - `frigate/app.py` (upstream): watchdog wiring, shared busy-timeout helper, one call at
    the top of `stop()`.
  - `frigate/record/writer.py` and `frigate/record/util.py` (fork code): rejected rows
    found by the handler, throttled rejection log, id length.
  - `frigate/record/maintainer.py` and `frigate/record/record.py` (fork-modified): the
    fallback request gets the timeout and keeps unwritten rows, and a stop with rows kept
    is unclean.
  - `frigate/embeddings/maintainer.py` (upstream): its database timeout uses the shared
    helper.
  - `frigate/video.py` (upstream, already fork-modified): keep the region grid on an empty
    reply.
- **Upstream merge impact:** low. `inter_process.py` and `watchdog.py` are small files
  that upstream rarely changes; their last functional changes were typing passes. The
  `app.py` hunks are a few lines in the DB-init and watchdog-start methods, which the
  fork already modifies.
- **Database:** no migration. Existing ids stay valid. New ids are 6 characters longer,
  under the 30-character limit.
- **Cost on the 4-core QNAP target:** negligible. One `is_alive()` check and two
  timestamp reads every 10 s (every 1 s while stopping), a 12-character string instead
  of 6 per segment, and single-row inserts only for a batch the database rejects. A
  restart costs a ~30 s recording
  gap, but it only replaces an outage that otherwise lasts until someone restarts
  Frigate by hand.
- **Clients:** none. Recording ids were already opaque strings.
