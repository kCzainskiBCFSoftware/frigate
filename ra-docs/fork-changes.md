# Fork Changes — Dual-Stream Recording + Playback Performance + Recording Resilience

This document is the canonical changelog for what this fork/branch adds on top of
upstream `blakeblackshear/frigate`. It is maintained by hand — update it whenever
you change fork-specific behavior.

- **Upstream base:** tag `v0.17.1` (`416a9b76`)
- **Active branch:** `v17.1-update-with-adaptive`
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

### Upstream files touched
High rebase-conflict risk: `frigate/record/maintainer.py`,
`frigate/record/cleanup.py`, `frigate/video.py`. Low to medium:
`frigate/comms/dispatcher.py` (one handler), `frigate/record/util.py`,
`frigate/record/record.py`. Low and additive: `frigate/app.py`,
`frigate/config/camera/record.py`, `frigate/config/config.py` (one call),
`frigate/config/database.py`, `frigate/config/camera/camera.py`,
`frigate/const.py`.

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

Backend tests do not run natively on Windows (`frigate/log.py` calls
`os.register_at_fork` at import). Run them in the image:

```bash
docker run --rm --workdir=/opt/frigate --entrypoint= -e PYTHONDONTWRITEBYTECODE=1 -v "$PWD:/opt/frigate" <frigate-image> python3 -u -m unittest
```

Current state: **280 tests**, `ruff check`/`format` clean, mypy clean on every
changed file. See *Housekeeping* for the two environment-only failures to expect.
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
  - The `INSERT_PREVIEW`, `UPSERT_REVIEW_SEGMENT` and other inter-process
    handlers can still raise into `InterProcessCommunicator.read()` and end the
    REP thread; only `INSERT_MANY_RECORDINGS` is hardened.
  - The main REP thread still waits on the database while it is locked, so other
    inter-process clients stall as upstream (design open question 6).
- Test-environment notes: `ruff format --check` with a current ruff flags
  `frigate/config/camera/ffmpeg.py`, which this fork has not changed. Running the
  suite from a copy inside the container (instead of the bind mount) makes
  `TestRematerializedMerge` fail with "disk I/O error" on the base commit too —
  the HTTP test base never stops its `SqliteQueueDatabase` writer threads.
