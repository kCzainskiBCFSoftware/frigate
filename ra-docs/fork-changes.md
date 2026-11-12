# Fork Changes — Dual-Stream Recording + Playback Performance + Recording Resilience

This document is the canonical changelog for what this fork/branch adds on top of
upstream `blakeblackshear/frigate`. It is maintained by hand — update it whenever
you change fork-specific behavior.

- **Upstream base:** tag `v0.17.1` (`416a9b76`)
- **Active branch:** `dev-fork`, the repo's default branch. It replaced
  `v17.1-update-with-adaptive` on 2026-10-29; `dev` only mirrors upstream.
- **Behavior specs:** `openspec/specs/` (one per capability). This file stays the
  code-level changelog.
- **Branch commits:**
  - `4228f609` feat: Add support for dual retention for single camera
  - `87d52ed7` test: attempt 2
  - `b1ffe9c4` fix: hls updates
  - `f1795069` fix: Update recordings/summary endpoint response times
  - `648fc516` fix: Resolve SD retention overwritten by HD retention
  - `7b873dba` fix: Force restart url from config view to stay in config
  - `1520f286` fix: Config redirect path
  - `4d8e81f2` test: playback and export performace logs
  - `04e2aaa7` fix: summary read min max query update
  - `6c4c9bd1` chore: Update GA flows
  - `410c033a` chore: Disable python checks
  - `15a471c6` fix: Add caching to support prefetches for vod
  - `7246a154` perf: Precomputed playback timeline + real API concurrency (#2)

## What the fork does

The fork has **three independent strands**, all documented below. The first two
have their own client-facing guides; the third has a config reference.

### Strand 1 — dual-stream recording with per-variant retention

One physical camera can record **two streams as distinct variants** —
`main` (HD) and `sub` (SD) — each a separate row in `Recordings` and a separate
file tree on disk. Two user-facing capabilities:

1. **Dual retention** — each record variant can have its own retention via a
   per-input `retain_days` override (e.g. keep `sub` 30 days while `main`/base is 7).
2. **Variant-aware playback/export/snapshot APIs** — every recordings-facing
   endpoint accepts a `?variant=` selector (`main`/`sub`/`all`) with a `sub`
   default for playback, `main` for snapshots, and automatic fallback to the other
   variant when the requested one has no footage in range.

### Strand 2 — playback performance and the precomputed timeline

Driven by a device recording **32 cameras** on a 4-core NAS with limited RAM,
serving **4 concurrent VOD players**:

1. **A timeline API that does not ship raw segment rows.** Three new endpoints
   answer "which parts of this window have footage" directly, turning a
   32-camera day view from 32 requests and ~64 MB into one request and ~64 kB.
   Backed by a precomputed ranges table (migration `035`).
2. **Real request concurrency.** The blocking playback handlers no longer run on
   uvicorn's single event loop, so four players and a timeline fetch actually
   overlap instead of queuing behind each other.
3. **Memory and nginx tuning** for NAS-class hardware, including a gzip
   misconfiguration that had silently disabled compression on all `/api/` JSON.

### Strand 3 — recording-path resilience

On the same 32-camera device, every camera periodically logged "Unable to keep
up with recording segments in cache" in the same second and lost footage, and
the watchdog then restarted every healthy recorder. The ffmpeg recorders were
fine; the segment mover was held up by frame processing, by a blocking
database insert queued behind the cleanup's `wal_checkpoint(TRUNCATE)`, and the
watchdog read the stalled mover as dead recorders. This strand takes the mover
off those dependencies, makes WAL maintenance non-blocking, shortens the
cleanup run, and judges each recorder by its own output. Every part has a
`record.pipeline.*` or `database.*` setting that restores upstream behavior.

Client-facing guides:
[ra-docs/dual-stream-api-changes.md](dual-stream-api-changes.md) (the `?variant=`
model, and §2.1.1 for `null` activity stats) and
[ra-docs/playback-timeline-api.md](playback-timeline-api.md) (the timeline
endpoints). Config reference for strand 3:
[ra-docs/recording-pipeline-config.md](recording-pipeline-config.md). This file
documents the **code** changes.

---

## Backend changes

### Config schema & helpers
- **`frigate/config/camera/ffmpeg.py`** — added two per-input fields on
  `CameraInput`:
  - `record_variant: Literal["main","sub"]` (default `"main"`) — variant label for
    a `record` input.
  - `retain_days: Optional[float]` — per-variant continuous-retention override
    (only meaningful on a `record` input).
  - `validate_roles` now enforces: each `record_variant` used at most once per
    camera, and if any record inputs exist exactly one must be `main` (the fallback
    / legacy variant).
- **`frigate/config/camera/camera.py`** — two accessors used throughout the
  recording code:
  - `get_record_variants()` → ordered list of configured record variants (defaults
    to `["main"]`).
  - `get_variant_retain_days(variant)` → the per-input `retain_days` override or
    `None`.

### Database & migration
- **`frigate/models.py`** — `Recordings` gained `variant` (CharField, default
  `"main"`, indexed) plus `codec_name`, `width`, `height`, `bitrate`,
  `transcoded_from_main`, and a composite index `(camera, variant, start_time,
  end_time)`.
- **`migrations/033_add_recordings_variant.py`** — adds the columns/indexes and
  backfills existing rows to `variant="main"`.
- **`migrations/034_drop_redundant_recordings_indexes.py`** — drops
  `recordings_variant` and `recordings_camera`; see *Query shape* below.
- **`migrations/035_create_recording_ranges.py`** — adds `recording_ranges` and
  `recording_range_coverage` (models `RecordingRanges` / `RecordingRangeCoverage`
  in `frigate/models.py`). **DDL only** — two `CREATE TABLE` and one
  `CREATE INDEX`, no data migration, no triggers, no views, and it never touches
  `recordings`. The tables start empty and are filled by the rollup; see
  *Precomputed ranges* below.

### Recording variant helpers
- **`frigate/record/variants.py`** (new) — shared constants and query helpers:
  `RECORDING_VARIANT_MAIN/SUB/ALL`, `DEFAULT_PLAYBACK_VARIANT="sub"`,
  `DEFAULT_SNAPSHOT_VARIANT="main"`, `apply_variant_filter(query, variant)`,
  `variant_has_overlapping_recording(...)`, and `resolve_playback_variant(...)`
  (the fallback-to-other-variant logic). **Prefer these over filtering
  `Recordings.variant` directly.**

### Recording maintainer
- **`frigate/record/maintainer.py`** — cache segment filenames are now
  `camera@variant@date` (with backward-compatible parsing of the legacy
  `camera@date` = `main`). Segments are routed/stored per variant, written to
  `recordings/{date}/{hour}/{camera}/{variant}/...`, and codec/width/height/bitrate
  metadata is captured per segment. Newest-segment publishing and bandwidth scaling
  account for one segment list per `(camera, variant)`.

### Cleanup / retention  ← core of "dual retention"
- **`frigate/record/cleanup.py`** — `expire_recordings()` now computes retention
  **per variant**:
  - base windows from `record.continuous.days` / `record.motion.days`;
  - for a variant with a `retain_days` override, `continuous_days` is **replaced**
    by the override and a `hard_cap_date` (= override) deletes anything older
    unconditionally;
  - `expire_existing_camera_recordings()` takes `variant` + `hard_cap_date` and
    filters the query to that variant;
  - an **orphan sweep** removes rows whose variant is no longer configured;
  - previews are kept if **any** variant still retains an overlapping segment.
  - **Bug fix (`648fc516`):** the per-variant *outer/motion* window was capped
    with `min(base_motion_days, override)`, which reverted the `sub` variant to the
    base (7-day) deletion bound and deleted long-retention footage early. Changed to
    `max(...)` so the override window is honored; the `hard_cap_date` still clamps
    the ceiling. See [the regression test](frigate/test/test_record_cleanup.py).

### Export
- **`frigate/record/export.py`** — `RecordingExporter` takes a `variant`, resolves
  it once (so precheck and exporter agree), filters segments to that variant, and
  builds **path-form** `/vod/.../{variant}/index.m3u8` composition URLs (query-form
  variant is dropped by nginx-vod-module).
- **`frigate/api/export.py`** — the export-create endpoint accepts `?variant=` and
  passes it through.

### Other API
- **`frigate/api/media.py`** — the largest API change: `?variant=` on
  recordings list, `recordings/summary`, clip, snapshot, plus, and VOD endpoints;
  path-form variant segment for `/vod/...`; `all` allowed only on listing
  endpoints; 422 on invalid values; fallback via `resolve_playback_variant`.
  Defaults: playback `sub`, snapshot `main`.
- **`frigate/api/review.py`** — motion-activity query filters
  `Recordings.variant == "main"` to avoid double-counting camera-level motion
  across variants.
- **`frigate/storage.py`** — per-camera bandwidth (MB/hr) is the **sum** of each
  variant's write rate, so dual-stream cameras report true disk consumption.
- **New timeline endpoints** (also in `frigate/api/media.py`) — all additive,
  none of the existing endpoints changed shape:

  | Endpoint | Returns |
  |---|---|
  | `GET /api/{camera}/recordings/ranges` | bare `[{start, end}]` array + `X-Recording-Variant` header |
  | `GET /api/recordings/ranges` | `{ranges: {camera: [...]}, variants: {camera: variant}}` for many cameras in one request |
  | `GET /api/recordings/hours` | `{camera: {"YYYY-MM-DD": [24 booleans]}}`, local hours, DST-aware |

  Shared params: `after`, `before`, `variant`, and `gap` (seconds; holes smaller
  than this are encoder jitter, not a recording gap). Windows are capped at
  `MAX_RANGES_WINDOW` (8 days) with a 422. Query-param models live in
  `frigate/api/defs/query/media_query_parameters.py`.

### Infra
- **`docker/main/rootfs/usr/local/nginx/conf/nginx.conf`** — `vod_upstream_extra_args`
  so query args reach the vod upstream (path-form variant remains canonical).
- **`.github/workflows/*` and `.github/actions/setup`** — CI workflows trimmed for
  the fork. *(Not feature-related; review before merging upstream.)*
- **`.github/workflows/release.yml`, `.github/workflows/bump_version.yml`,
  `docker/release/version-stamp.Dockerfile`** — releases were a pure retag, so the
  `<Makefile VERSION>-<sha>` baked in at build time shipped unchanged and a device
  pulling `:0.17.3` reported `0.17.2-7246a15`. `Makefile` `VERSION` now always holds
  the version being worked toward (`0.17.4` from here). **Release** (run with the
  tested sha, or on a pushed `v*` tag) adds one layer on top of the tested image that
  rewrites `frigate/version.py` to the bare version, pushes `:<version>` + `:stable`,
  creates the tag, and moves the Makefile on `dev-fork` to the next patch version.
  Guards refuse a commit not on `dev-fork`, a version already tagged, and a tag that
  disagrees with the Makefile. `bump_version.yml` is only for minor/major jumps.
- **`.github/workflows/ci.yml`** — `RELEASE_BRANCH` moved from
  `v17.1-update-with-adaptive` to `dev-fork`. Only pushes to `dev-fork` publish
  `<version>-<sha>` and move `latest`.

---

## Playback performance & the precomputed timeline

A second strand of fork work, driven by a device recording 32 cameras on a
4-core NAS with limited RAM, serving 4 concurrent VOD players.

### Query shape

- **`frigate/record/variants.py` — `recordings_overlap_clause()`.** The seekable
  overlap predicate. Interval overlap is `start_time <= end_ts AND end_time >=
  start_ts`; the extra `start_time >= start_ts - MAX_SEGMENT_DURATION` lower
  bound is implied for any segment shorter than 600s but gives SQLite a
  two-sided range so it seeks `(camera, variant, start_time, end_time)` instead
  of walking the camera's entire retained history. **Every time-window query on
  `Recordings` must use it** — a hand-written `BETWEEN`-OR overlap regresses to
  a full scan. Guarded by an `EXPLAIN QUERY PLAN` test.
- **Migration `034`** drops `recordings_variant` and `recordings_camera`. A
  two-value index is a planner trap without fresh statistics, and both were pure
  write amplification on the segment-insert path.
- **`ANALYZE`** runs at startup (`frigate/app.py`) and hourly
  (`RecordingCleanup.refresh_recordings_stats`). Without statistics the planner
  picks badly on a multi-million-row table.
- **`all_recordings_summary`** uses a recursive loose ("skip") index scan that
  touches ~one row per day-with-footage. Note the MIN/MAX split it documents:
  SQLite only applies its min/max index optimization to a SELECT containing a
  *single* aggregate.

### Precomputed ranges (migration `035`)

Client-facing API guide: `ra-docs/playback-timeline-api.md`.

- **`frigate/record/ranges.py`** — the timeline core. A SQL gap-and-islands
  merge (window functions; SQLite 3.46 via bundled `pysqlite3`) turns ~8,640
  segment rows per camera-day into ~40 `{start, end}` ranges. Merged on a
  running `MAX(end_time)`, because segments overlap in the field and merging on
  the previous row's end drags a range's end backwards and loses footage.
- **`recording_ranges` / `recording_range_coverage`.** `RecordingCleanup` rolls
  settled windows up on its existing 60s tick, trims to retention hourly (after
  `expire_recordings`, so the ranges follow the recordings table), and backfills
  history a day per hour. Coverage is tracked explicitly so "no footage" is
  distinguishable from "not precomputed yet"; anything uncovered is merged live
  with the same SQL, so answers are correct from the first request.
- Stored at `MATERIALIZED_GAP = 2.0s` (tuned against measured fleet jitter; see
  the postmortem); a caller's larger `gap` is served by
  re-merging, which is equivalent to merging the raw segments at that gap.
  A smaller gap falls through to the live path.
- **`_replace_window` must preserve coverage outside the window it recomputes.**
  Stored ranges are unclipped, so a continuous run is one row that extends past
  the window on both sides; deleting everything that overlaps and regenerating
  only what `live_ranges` admits destroys the rest. The first field build did
  exactly that and reported 3.5 h for a 12 h day -- see
  [ranges-underreporting-postmortem.md](ranges-underreporting-postmortem.md).
  Guarded by `TestCoverageInvariant`.
- **Migration `036`** discards both tables so they rebuild from scratch; the rows
  damaged by that bug lie between `covered_from` and `covered_to`, which neither
  rebuild path revisits.

### API concurrency

- **The blocking playback handlers in `frigate/api/media.py` are plain `def`,
  not `async def`** — `recordings`, `recordings_summary`,
  `get_snapshot_from_recording`, `no_recordings`, `recording_clip`, `vod_ts`,
  and the sibling VOD routes. uvicorn runs a single process with a single event
  loop, so an `async` handler doing blocking SQLite/ffprobe/ffmpeg work stalls
  every other request on the box, live view included. As `def`, FastAPI runs
  them in the worker threadpool. `vod_event` and `event_clip` stay coroutines
  (they await `require_camera_access`) and reach `_vod_ts` / `_recording_clip`
  through `run_in_threadpool`. A test asserts none of them is a coroutine.
- **Threadpool bounded to 4** (`API_THREAD_POOL_SIZE`, override with
  `FRIGATE_API_THREAD_POOL_SIZE`). Peewee connection state is thread-local, so
  each worker thread opens its own SQLite connection; anyio's default of 40 is
  wrong on NAS hardware that is also recording every camera.
- **VOD mapping cacheability keys on the window's END**
  (`end_ts < now - MAX_SEGMENT_DURATION`), not its start. A window still being
  written can gain a segment seconds later; one that cannot is immutable. The
  old start-based rule marked every ≤1h fragment touching the last hour
  non-cacheable, and nginx-vod re-requests a non-cacheable mapping *per
  segment*.
- **`get_keyframe_before`** (`frigate/util/media.py`) caches the ffprobe
  keyframe index per file — bounded LRU, single-flight, negative caching.

### Memory and nginx

- **SQLite pragmas**: `cache_size` 512MB → 256MB and `mmap_size` 256MB on all
  three DB-owning processes (main app, record, embeddings). mmap'd pages are
  file-backed, so the kernel can reclaim them under pressure and they are shared
  between connections rather than duplicated per connection — which is what
  makes the API threadpool affordable.
- **`nginx.conf`**: `vod_metadata_cache` 512m → 128m and `vod_mapping_cache`
  5m/10m → 32m/1h (both are shared memory, i.e. RAM, and nginx never returns
  slab pages to the OS); `open_file_cache` 1000 → 8192 with
  `worker_rlimit_nofile 16384` (10s segments mean an hour of scrubbing touches
  ~360 files per camera, and every miss is an `open()` on the recordings
  volume); `api_cache` `max_size` 10m → 32m in `/dev/shm`.
- **gzip actually applies to `/api/` JSON now.** The server-level
  `gzip_types application/vnd.apple.mpegurl` was *replacing* the http-level list
  containing `application/json` (`gzip_types` does not merge across levels), and
  `gzip_proxied no-cache no-store private expired auth` gated on upstream
  headers the API handlers do not send. One list at http level, `gzip_proxied
  any`. Media types are deliberately excluded — gzipping H.264/fMP4 costs ~32×
  the CPU for well under 1% of the bytes.
- **`/api/` `Cache-Control` passthrough**: a `map` on `$upstream_http_cache_control`
  plus `proxy_hide_header`, so a handler can opt into caching without emitting a
  duplicate header. Everything that sets nothing still gets `no-store`.

### Observability

- **`frigate/api/perf.py`** — one JSON line per request to
  `perf-logs/api_perf.log` next to the DB (rotating, `propagate=False` so it
  stays out of the container log). Carries `resolve_variant_ms`, `sql_ms`,
  `keyframe_probe_ms`, `keyframe_probes`, `segments`, `nginx_cacheable`,
  `ffmpeg_first_byte_ms`, `stream_ms`, `total_ms`.

### Capability advertisement

`GET /api/config` carries a top-level `fork` key — the build `version` plus a
list of supported fork-only `features` — so clients on a mixed-version fleet can
detect endpoints without probing for 404s. **Append only** — never rename or
remove a shipped flag. Deliberately carries no vendor or product name: this is a
public repository.

---

## Recording-path resilience

OpenSpec change `recording-stall-resilience` (design and field evidence in its
`design.md`). No schema migration; no existing row is rewritten. Settings,
defaults and upstream values: [recording-pipeline-config.md](recording-pipeline-config.md).

### Configuration
- **`frigate/config/camera/record_pipeline.py`** (new) — `RecordPipelineConfig`,
  the `record.pipeline` block, with `apply_global_pipeline()`, which
  `FrigateConfig.post_validation` calls so every camera carries the global values
  except its own `recorder_stale_timeout`. Validates
  `processing_timeout + 10 < max_cached_segments × 10`.
- **`frigate/config/database.py`** — `wal_checkpoint`, `wal_size_limit`,
  `cache_size`.
- **`frigate/config/camera/camera.py`** — each `ffmpeg_cmds` entry carries its
  input's `record_variant` (`None` for non-record inputs), so the watchdog maps
  each record process to one recorder. Additive in `GET /api/config`.

### Segment mover (`frigate/record/maintainer.py`)
- A continuous-retention segment moves once frame processing has passed its
  end, or `processing_timeout` after it; on the timeout `motion`, `objects`,
  `regions` and `dBFS` are stored as `NULL` and `warn_processing_lag` logs at
  most once per camera per 5 minutes.
- **Bug fix (found in review):** a continuous or motion segment overlapping an
  in-progress alert/detection was moved in the review's retain mode before its
  frames were processed, and `should_discard_segment` then deleted it with no
  log line. Activity-based discard now waits for complete stats
  (`frames_processed`). Unconditional — there is no setting for the old behavior.
- Probes and copies run under an `asyncio.Semaphore(move_concurrency)`
  (`move_slot()`).
- `discard_backlog()` applies the cache limit per camera and variant **after**
  the pass's moves, with the upstream warning texts; `enforce_cache_reserve()`
  keeps `cache_free_reserve` free at the start and end of each pass (helpers in
  `frigate/record/cache_watch.py`).
- Rows go to the writer (`self.writer.submit(rows, saved)`); without it the
  upstream blocking request remains.

### Row writes (`frigate/record/writer.py`, new)
- `insert_recording_rows()` — the body of the main-process
  `INSERT_MANY_RECORDINGS` handler (`frigate/comms/dispatcher.py`). Inserts with
  `ON CONFLICT DO NOTHING` (re-sent batches are harmless), returns
  `{"ok": bool, "count"|"error"}` and never raises. **Bug fix (found in review):**
  a database error escaping the old handler ended the inter-process REP thread,
  and every REQ client in Frigate then waited forever.
- `RecordingsWriter` — bounded queue (`db_write_queue`), batches of 500,
  retry with backoff 1 s → 30 s, warning / error after 60 s / info on recovery
  with the queue depth, and the `recordings/saved` publication, which never
  runs ahead of a queued row.
- Shutdown: `RecordProcess.run` (`frigate/record/record.py`) joins the mover,
  drains the writer for `db_flush_timeout`, then writes
  `.recordings_clean_shutdown` next to the database. `FrigateApp.stop()` joins
  the recording process for `db_flush_timeout + 5` s before terminating it.
- **Timeline correctness:** `note_late_rows()` in `frigate/record/ranges.py`
  records rows that start behind a camera/variant's `covered_to`; the next
  `roll_up_ranges` tick re-merges from there. Without it, a row written more than
  `MAX_SEGMENT_DURATION` late was never reflected in the precomputed ranges (the
  bug class of [the postmortem](ranges-underreporting-postmortem.md)).

### Recovery (`frigate/record/reconcile.py`, new)
- `FrigateApp.init_recording_manager` takes the marker before starting the
  recording process; without one, `RecordingCleanup.run` calls
  `reconcile_recordings()` before `sync_recordings`. It scans the per-variant
  hour directories of the last `reconcile_window` hours, one indexed query per
  camera and variant, probes each file without a row one at a time and inserts
  it through `insert_recording_rows` with `NULL` stats. Unprobeable files are
  logged and left alone; files the new run wrote are left to the writer.

### Recorder watchdog (`frigate/video.py`, `frigate/record/cache_watch.py`)
- `RecorderWatch` (new, in `cache_watch.py`) scans the cache once per check and
  keeps, per variant, the newest output seen (file-name start time or mtime);
  the mover's `latest` messages are only a heartbeat; `valid`/`invalid` are
  kept per variant via `cache_path` and judged only while the heartbeat is
  fresh, from no earlier than its last resume.
- `CameraWatchdog.restart_if_stale()` restarts only the stale recorder and gives
  it a new startup grace; `warn_if_mover_stalled()` reports a stalled mover at
  most every 5 minutes and restarts nothing. `recorder_watchdog: mover` keeps the
  upstream block, which now uses `recorder_stale_timeout` instead of a hardcoded
  120 s and a `min(90, timeout)` grace.

### Database maintenance (`frigate/db/maintenance.py`, new)
- `maintain_wal()` — `PASSIVE` checkpoint, bounded `TRUNCATE` fallback (2 s busy
  timeout, above 4 × `wal_size_limit`), or the upstream `truncate` mode; called
  from `RecordingCleanup.truncate_wal`.
- `size_pragmas()` — `cache_size` and `journal_size_limit`, used by
  `FrigateApp.bind_database`, `init_database`, `RecordProcess.run` and the
  cleanup's `ANALYZE` connection.
- `CleanupRunSummary` — one info line per cleanup run: phase timings (expire,
  directories, checkpoint, stats, ranges), WAL size before/after, checkpoint
  result.

### Cleanup run (`frigate/record/cleanup.py`, `frigate/record/util.py`)
- Scoped empty-directory removal: `unlink_recording()` collects each deleted
  file's directory, `expire_recordings()` returns them, and
  `remove_empty_parent_directories()` removes those and their parents up to
  `recordings/` once empty. `full` keeps `remove_empty_directories()`.
- The removed-camera, per-variant and orphan-variant queries
  (`removed_camera_query`, `variant_expiry_query`, `orphan_variant_query`) get a
  `start_time` upper bound (`expiry_seek_bound`) equal to their latest
  `end_time` cutoff — no result changes, but SQLite seeks an index instead of
  reading every retained row.
- **Bug fix:** the per-variant query also selects rows older than the variant's
  `retain_days` hard cap. Before, a segment *with motion* inside a longer motion
  window was never read, so the hard cap required by the variant-retention spec
  did not apply to it.

### Feature flag
- `recordings_unknown_stats` appended to `FORK_FEATURES` (`frigate/const.py`):
  `motion`/`objects` in recording listings and summaries can be `null`.

### Fixes from the PR review
- **Memory leak (also upstream):** `move_segment` returned a moved segment's row
  before `end_time_cache.pop()`, so every moved segment's probe result stayed in
  the mover's memory — ~550 B each, ~300 MB a day on 32 dual-stream cameras.
  It is now popped once the cache file is removed.
- **Retain-all review segments:** a segment overlapping an alert/detection whose
  retain mode is `all` no longer waits for frame processing beyond
  `processing_timeout`; on the timeout it is moved with `NULL` stats, like a
  continuous segment. Before, a processing lag longer than the backlog window
  let `discard_backlog` delete alert footage that upstream kept. Activity-based
  modes (`motion`, `active_objects`) still wait for complete stats.
- **Cache reserve only on a RAM cache:** `enforce_cache_reserve` acts only when
  `CACHE_DIR` is a tmpfs/ramfs (`filesystem_type()` in `cache_watch.py`, read
  from `/proc/mounts` once). On a disk-backed cache it logs one warning and does
  nothing; before, the free space of a nearly full shared volume made it
  discard every closed segment on every pass.
- **Writer cannot die silently:** an exception from a send is retried like a
  refused write, and one from the `saved` publish is logged; `submit()` no
  longer waits on a writer thread that has stopped. If it has stopped anyway,
  the mover writes with the blocking request again, including the rows the
  writer still held (`take_rows()`), in batches of `BATCH_ROWS`.
- **Recordings sync and queued rows:** `sync_recordings` no longer deletes a
  file without a row that was moved within the last 2 hours
  (`SYNC_FILE_GRACE` in `frigate/record/util.py`); its row can still be queued
  in the writer.
- **Full row queue holds the mover instead of blocking it:** `submit()` never
  waits; the mover asks `RecordingsWriter.has_room()` before a pass and, while
  the queue is full, moves nothing, publishes no `latest` heartbeat (so the
  cache watchdog does not restart recorders for the missing `valid` messages)
  and keeps trimming frame info and applying `discard_backlog` (quietly) and the
  reserve. Before, it blocked inside `submit()`: no stop, no cache limits, a
  filling tmpfs. `submit()` also queues the rows before the `saved` times.
- **Prompt stop:** the wait between mover passes is `stop_event.wait()`, so the
  writer drain starts at once instead of up to 5 s later.
- **Start-of-pass reserve:** only half of `cache_free_reserve`
  (`START_RESERVE_FACTOR`) is enforced before a pass's moves, so a backlog the
  pass can move is no longer discarded first.
- **Directories emptied elsewhere:** `StorageMaintainer.reduce_storage_consumption`,
  `sync_recordings` and the delete-reviews API (`frigate/api/review.py`) call
  `remove_empty_parent_directories()` for what they delete; in `scoped` mode
  nothing else would remove those directories.
- **Writer reconnects:** a send answered with `""` (a ZMQ error, which can leave
  the REQ socket refusing every later send) is retried on a new requestor.
- **Recovery:** probes in one event loop and inserts in batches of
  `BATCH_ROWS` instead of one request per row.
- **Shared code:** `recording_row()` in `frigate/record/util.py` builds the row
  for both the mover and recovery; the mover parses cache names with
  `cache_watch.parse_cache_name` (its own `_parse_cache_segment` is gone) and
  takes `DEFAULT_RECORD_VARIANT` from `frigate/config/camera/ffmpeg.py`; recovery
  filters variants with `apply_variant_filter`.

### Upstream files touched
High rebase-conflict risk: `frigate/record/maintainer.py`,
`frigate/record/cleanup.py`, `frigate/video.py`. Low to medium:
`frigate/comms/dispatcher.py` (one handler), `frigate/record/util.py`,
`frigate/record/record.py`. Low and additive: `frigate/app.py`,
`frigate/config/camera/record.py`, `frigate/config/config.py` (one call),
`frigate/config/database.py`, `frigate/config/camera/camera.py`,
`frigate/const.py`.

---

## Inter-process request resilience

OpenSpec change `ipc-request-resilience` (capability `inter-process-requests`,
and changes to `recording-db-writes`). No API, config or schema change.

**Incident it explains.** A v0.17.3 device logged one
`peewee.IntegrityError: UNIQUE constraint failed: recordings.id` from the main
process's `Thread-7 (read)`, then stopped recording with no further log line.
Two segment rows that opened in the same second drew the same 6-character id
suffix (with 64 streams, many rows share each start second). The exception
ended `InterProcessCommunicator.read()`, the one thread that answers every
inter-process request in Frigate, and `InterProcessRequestor.send_data` has no
timeout, so the recording process and every later requester waited forever.
v0.17.4 stopped that traceback (`ON CONFLICT DO NOTHING`, the background
writer), but any other handler could still end the thread, the writer's
"no reply → reconnect" could not fire without a timeout, a collision silently
dropped the second row, and a row the database refused was retried forever,
which filled the queue and stopped every camera.

### Request thread (`frigate/comms/inter_process.py`)
- `read()` serves each request through `_serve_one()`: a handler exception, a
  request that is not a `(topic, payload)` pair, or one that cannot be decoded is
  answered with `[]`; a reply that cannot be encoded (e.g. a `set`) falls back
  to `send_json([])`. An outer catch-all logs and continues; only a ZMQ error
  breaks the inner drain loop, as upstream. The thread is named
  `inter_process_requests`.
- `_log_failure()` logs one traceback per topic per `ERROR_LOG_INTERVAL` (5 min);
  the next one reports how many failures were not logged.
- `busy` holds `(topic, monotonic start)` while a request is handled and `None`
  otherwise, assigned as one tuple so the watchdog reads it without a lock.
- `InterProcessRequestor(timeout=None)`: with a timeout, `RCVTIMEO`/`SNDTIMEO`
  are set; on `zmq.Again` the REQ socket is closed with `linger=0`, a new one is
  connected, a warning names the topic, and `""` is returned. Without a timeout
  nothing changes. `stop()` closes a timed socket with `linger=0`.

### Watchdog (`frigate/watchdog.py`, `frigate/app.py`)
- `FrigateWatchdog(..., inter_process=None, busy_timeout=None)`; the loop body
  moved into `check()`, and `check_request_thread()` runs after the detector
  checks. A dead reader thread, or `busy` older than
  `max(600, 2 × busy_timeout)` (640 s at 32 cameras), logs an error with the
  topic and duration and calls `restart_frigate()` once. Skipped when the app's
  or the communicator's stop event is set.
- That restart also arms a daemon `threading.Timer`: if the process is still
  alive `RESTART_EXIT_DEADLINE` (60 s) later, it logs an error and calls
  `os._exit(1)`. Without it, `FrigateApp.stop()` waited forever on
  `stats_emitter.join()` or `detected_frames_processor.join()`, threads blocked
  in an untimed request to the dead thread, until s6's 120 s `timeout-kill`.
  That is the 2-minute gap with no API and no logs seen after a UI restart on
  0.17.3. The dead-detector restart arms no timer. Either restart needs the
  container's restart policy to bring Frigate back.
- `FrigateApp.start_watchdog()` passes the communicator and the busy timeout.

### Shared busy timeout (`frigate/db/maintenance.py`)
- `busy_timeout(config)` = `max(60, 10 × cameras enabled in config)`, used by
  `FrigateApp.bind_database`, the watchdog, and the row-request timeout. The
  recording process's own connection keeps its inline formula.

### Row writes (`frigate/record/writer.py`, `frigate/record/maintainer.py`)
- `insert_recording_rows` adds `rejected` to a failed outcome:
  `True` for `peewee.IntegrityError`/`DataError` (NOT NULL, CHECK, a trigger's
  `RAISE(ABORT)`, datatype), `False` for everything else (locked, busy, I/O,
  timeout, corruption).
- `RecordingsWriter` isolates rejected rows: a rejected batch of n > 1 rows
  becomes the `suspect` range and the next send takes its first half, with no
  backoff; while the range is open, each success halves the rest (a binary
  search), and a rejected single row is popped and logged at error level with
  its path, camera, variant and error. The batch limit then doubles back from 1
  toward `BATCH_ROWS`. One bad row in 500 costs at most 20 sends. A rejection
  neither starts nor advances `failing_since`. Other failures keep the
  whole-batch retry with backoff.
- Segment-row requests time out after `row_request_timeout(config)` =
  busy timeout + `REPLY_MARGIN` (30 s): the writer's default requestor factory
  (`timed_requestor_factory`) and the mover's `self.requestor`. The mover's
  blocking request now logs an error with the batch size and its first and last
  path on no reply, as well as on `ok: false`; it does not retry that batch.

### Row ids (`frigate/record/util.py`)
- `recording_row` uses a 12-character random suffix (upstream: 6); the longest
  id is 25 characters. Collision odds with 64 rows in one second: ~4e-16 per
  second. Existing ids are unchanged.

### Region grid (`frigate/video.py`)
- `process_frames` replaces `region_grid` only when the reply is a non-empty
  list, and logs a warning otherwise. A failed request's `[]`, or the `""` of a
  ZMQ error (an upstream bug too), used to be indexed as a grid.

### Upstream files touched
Low conflict risk: `frigate/comms/inter_process.py` and `frigate/watchdog.py`
(small files upstream rarely changes; `watchdog.py`'s loop body moved into
`check()`), `frigate/app.py` (`bind_database` timeout and `start_watchdog`),
`frigate/video.py` (the region-grid refresh, a few lines). Fork files:
`frigate/db/maintenance.py`, `frigate/record/writer.py`,
`frigate/record/maintainer.py`, `frigate/record/util.py`.

### Verification
Fault injection on 2026-11-12, with two dual-stream cameras recording looped test
files in a dev container (details in the change's `tasks.md`, 9.3): a 2-minute
`BEGIN IMMEDIATE` lock lost no row and kept segments moving; a `RAISE(ABORT)`
trigger on one camera dropped exactly its rows, one error each, while the other
camera's rows kept landing; 25 requests whose handler raises were all answered,
with one traceback logged. With the request thread killed while the stats
emitter was blocked on it, `stop()` hung for good without the exit deadline.
With it, recording resumed 83 s after the thread died, and reconcile recovered
the 12 queued rows.

### Open items
- Other handlers' own bugs (e.g. `Event.get` for an event deleted before its
  description arrived) are now logged per topic instead of fatal; each is still
  a bug to fix on its own.
- The watchdog's restarts and the per-topic failure counts are not exposed in
  `/api/stats`, so fleet monitoring cannot alert on them yet.
- Other restarts, from the UI, the API or a dead detector, can still hang in
  `FrigateApp.stop()` while the request thread is dead or stuck, until s6's
  120 s `timeout-kill`. The exit deadline is armed only by the request-thread
  watchdog.
- 9.4 (device-only): after a week on the new image, confirm no request-thread
  restart, and record the old id exposure measured with
  `SELECT SUM(c*(c-1)/2.0) FROM (SELECT COUNT(*) c FROM recordings GROUP BY start_time);`
  divided by retained days and by 36^6.

---

## Frontend changes (`web/`)

- **`src/components/player/RecordingPlaybackPreferenceSelect.tsx`** (new) — the
  in-player Auto/Main/Sub variant selector.
- **`src/hooks/use-recording-playback-preference.ts`** (new) — persists the
  per-camera variant choice.
- **`src/components/player/dynamic/DynamicVideoPlayer.tsx`** — uses the preference
  to build variant-aware playback URLs.
- **`src/components/overlay/ExportDialog.tsx`** — passes the playback variant
  preference into the export job.
- **`src/components/overlay/detail/SearchDetailDialog.tsx`,
  `TrackingDetails.tsx`** — variant-aware media references.
- **`src/types/record.ts`** — variant types; `motion`, `objects` and `dBFS` on
  recording rows are `number | null` (unknown activity stats). No component
  reads them: the timeline uses the ranges endpoints and the motion graph uses
  `/review/activity/motion`, which leaves unknown segments out.
- **`public/locales/en/components/player.json`** — strings for the variant selector.

---

## Tests

### Strand 1 — dual-stream

- **`frigate/test/test_config.py`** — dual-variant config validation
  (`test_dual_record_distinct_variants_ok`, duplicate-variant rejection).
- **`frigate/test/http_api/test_http_media.py`** + **`base_http_test.py`** —
  `TestHttpVodVariants` covers variant-aware media endpoints;
  `insert_mock_recording` gained `variant`/`path`.
- **`frigate/test/test_record_cleanup.py`** — exercises
  `RecordingCleanup.expire_recordings()` end-to-end and guards the per-variant
  retention regression (sub retained to its 30-day override; main to base 7 days).

### Strand 2 — timeline and concurrency

- **`frigate/test/test_record_ranges.py`** (new, 22 tests) — the core.
  - `TestRangeMergeCorrectness` — the SQL merge against an independently-written
    Python reference over randomised jittered segments, plus the three semantics
    external clients depend on: **running-`max(end)` merging** (a last-end merge
    loses 30s of footage in the overlapping-segment case), **unclipped window
    edges**, and `gap` behaving as a real parameter. Includes the
    `EXPLAIN QUERY PLAN` guard and a check that the hand-written SQL predicate
    returns the same rows as `recordings_overlap_clause()`.
  - `TestRematerializedMerge` — **the load-bearing one.** Re-merging ranges
    stored at `MATERIALIZED_GAP` must equal merging the raw segments at the
    caller's gap, for every gap at or above it. This is the one correctness risk
    precomputation introduces.
  - `TestRollup` — watermark advance, trailing-overlap recomputation (a
    late-arriving segment still merges correctly), never materialising an
    unsettled window, the bounded first rollup, stitching a live tail onto
    stored ranges, falling back to live for uncovered windows, and the
    `ROLLUP_MIN_ADVANCE` cadence guard.
  - `TestRetentionTrim` — expired ranges dropped, `covered_from` raised, and a
    range straddling the cutoff clipped rather than deleted.
- **`frigate/test/http_api/test_http_media.py`** — three new classes:
  `TestRecordingRangesApi` (response shapes, per-camera variant fallback,
  unclipped edges, empty-is-`[]`, 422 over-window, hours agreeing with ranges),
  `TestPlaybackHandlerConcurrency` (asserts the converted handlers are **not**
  coroutines, and that `vod_event` / `event_clip` still are), and
  `TestVodMappingCacheability` (settled window cacheable, live window not, and
  the `MAX_SEGMENT_DURATION` boundary).
- **Fixed a pre-existing broken guard.** `test_query_plan_seeks_composite_index`
  passed in isolation but **failed in the full suite** — on the base commit too.
  It inserted a single row, so with no `sqlite_stat1` the planner had no basis to
  prefer the variant composite index and the assertion came down to a tie-break.
  It now seeds representative rows across both cameras and variants and runs
  `ANALYZE`, matching production, which ANALYZEs at startup and hourly.

### Strand 3 — recording-path resilience

- **`frigate/test/test_maintainer.py`** — processing timeout (`NULL` stats,
  `0` waits, motion-only waits, rate-limited warning), complete-stats discard
  (reproduces the silent deletion on the old code), the copy cap, a backlog moved
  in the same pass is not discarded, per-variant limits, the free-space reserve,
  and the hand-off to the writer while the database blocks.
- **`frigate/test/test_record_writer.py`** (new) — the writer (no waiting on a
  blocked database, backpressure, retry and outage logging, `saved` never ahead
  of queued rows, flush on stop, unsent rows reported), the marker, and the
  handler (idempotent re-send, `NULL` stats, a locked database is a failed
  outcome, and the real REQ/REP loop keeps serving after one).
- **`frigate/test/test_record_reconcile.py`** (new) — recovery in and outside the
  window, re-run no-op, unprobeable files, files of the new run left alone,
  recovered rows reaching the precomputed timeline, and recovery running before
  `sync_recordings` only after an unclean shutdown.
- **`frigate/test/test_video.py`** — `RecorderWatch` and the per-recorder
  restart: paused mover, no new files, hung `sub`, invalid-only segments,
  backlog clearing after a resume, grace and the stalled-mover warning.
- **`frigate/test/test_db_maintenance.py`** (new) — size pragmas on new
  connections, the WAL cut back to its limit, passive checkpoint not blocking
  writers, the bounded fallback giving up within its timeout, upstream
  `truncate` mode, the run summary.
- **`frigate/test/test_record_cleanup.py`** — scoped directory removal,
  equivalence of the bounded expiry queries, `EXPLAIN QUERY PLAN` guards on
  `start_time`, the hard cap with motion, `NULL`-stat retention.
- **`frigate/test/test_record_retention.py`**, **`test_record_ranges.py`**,
  **`http_api/test_http_media.py`**, **`http_api/test_http_app.py`**,
  **`test_config.py`** — `NULL` stats under review retain modes, a row written
  30 minutes late, the API with `NULL` stats, the fork feature block, and the
  new settings.

### Inter-process request resilience

- **`frigate/test/test_inter_process.py`** (new) — over a real REP/REQ pair on a
  temp `ipc://` endpoint: a raising handler gets `[]` and the next request on
  another requestor is answered; an unencodable reply gets `[]`; 50 failures of
  one topic log one traceback and the next window reports 49; `busy` during and
  after a slow handler; malformed requests; a timed requestor returns `""` within
  its timeout and its next request is answered on a new connection.
- **`frigate/test/test_watchdog.py`** (new) — dead reader → one restart, a 641 s
  request at busy timeout 320 → restart naming the topic, 300 s and the 600 s
  floor → none, no restart while stopping, the upstream signature, and the exit
  deadline (fires after a request-thread restart, is a daemon timer, and is not
  armed by a dead-detector restart; `_hard_exit` is always patched).
- **`frigate/test/test_record_writer.py`** — rejected vs locked outcomes with a
  real database (a `RAISE(ABORT)` trigger, a held `BEGIN IMMEDIATE`), one bad
  row in 500 (499 written, one error, < 25 sends), two bad rows far apart, locked
  batches never split, `has_room()` while isolating, the requestor timeout, an
  integration test where the main process answers past the writer's timeout
  (batch stored once, queue drained), and 64 distinct 12-character ids.
- **`frigate/test/test_maintainer.py`** — the mover's timed requestor, and its
  blocking request logging a missing reply with the batch size and paths.
- **`frigate/test/test_video.py`** — an empty or missing region-grid reply keeps
  the grid `process_frames` started with.
- **`frigate/test/test_db_maintenance.py`** — `busy_timeout()` for 3, 32 and
  7 + 1 disabled cameras.

Every new test was checked to fail with its fix removed (mutations applied in
the container copy only).

Backend tests do not run natively on Windows (`frigate/log.py` calls
`os.register_at_fork` at import). Run them in the image:

```bash
docker run --rm --workdir=/opt/frigate --entrypoint= -e PYTHONDONTWRITEBYTECODE=1 -v "$PWD:/opt/frigate" <frigate-image> python3 -u -m unittest
```

Current state: **374 tests**, `ruff check` clean, `ruff format` clean apart from
the unchanged `frigate/config/camera/ffmpeg.py` (see *Housekeeping*). mypy was
last checked for strand 2, not for strand 3. See *Housekeeping* for the
environment-only failures to expect.
---

## Housekeeping / known issues

- **Stray `.pyc` files removed from tracking** (`79a2adc1`+). Five were tracked
  (`frigate/__init__.pyc`, `frigate/config/__init__.pyc`,
  `frigate/detectors/__init__.pyc`, `frigate/test/**/__init__.pyc`) — Python 2.7
  build artifacts from a Windows checkout. Two of them embedded the absolute
  build path, i.e. a local username and directory names, in a public repository.
  `.gitignore` had `__pycache__` but not `*.pyc`, which is how they got in; both
  are now ignored.
  **They remain in git history** — untracking only fixes HEAD. Purging them needs
  a history rewrite (`git filter-repo`) and a force push, which is disruptive on a
  shared branch; left as a deliberate decision rather than done silently.
- Two pre-existing test-environment failures to expect and ignore:
  `TestGo2rtcStreamAccess` needs a live go2rtc on `127.0.0.1:1984`, and mypy needs
  `types-peewee` installed or every peewee model reports
  "Class cannot subclass Model".
- `ra-docs/` holds this changelog, the two client guides, the recording
  pipeline config reference and the ranges postmortem, all tracked. Note this
  file lives in `ra-docs/`, not the repo root.
- Found while building strand 3, **not fixed** (separate changes):
  - `expire_recordings()` fetches reviews with `start_time` before the *oldest*
    variant motion cutoff (`min(...)`), so on a dual-stream camera whose `sub`
    keeps footage longer, `main` segments overlapping alerts or detections
    between the two windows are expired. It likely wants `max(...)`.
  - ~~The `INSERT_PREVIEW`, `UPSERT_REVIEW_SEGMENT` and other inter-process
    handlers can still raise into `InterProcessCommunicator.read()` and end the
    REP thread; only `INSERT_MANY_RECORDINGS` is hardened.~~ Fixed by
    `ipc-request-resilience`: every handler is guarded at the transport.
  - The main REP thread still waits on the database while it is locked, so other
    inter-process clients stall as upstream (design open question 6). Since
    `ipc-request-resilience`, a request stuck past `max(600, 2 × busy timeout)`
    restarts Frigate.
- Test-environment notes: `ruff format --check` with a current ruff (0.17)
  flags `frigate/config/camera/ffmpeg.py`, which this fork has not changed.
  **Fixed a flaky test harness:** `BaseTestHttp.tearDown` deleted `test.db`
  without stopping the test's `SqliteQueueDatabase` writer thread, which kept
  the deleted files open. Later tests then failed at random with "disk I/O
  error" or "attempt to write a readonly database" (on the base commit too, e.g.
  `TestRematerializedMerge` when run from a copy inside the container), and a
  write queued to a writer that had died at connect waited forever, hanging the
  full suite. `tearDown` now calls `self.db.stop()`.
