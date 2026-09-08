# Fork Changes — Dual-Stream Recording with Per-Variant Retention

This document is the canonical changelog for what this fork/branch adds on top of
upstream `blakeblackshear/frigate`. It is maintained by hand — update it whenever
you change fork-specific behavior.

- **Upstream base:** `dev` (merge-base `416a9b76`)
- **Active branch:** `task/multi-camera-playback-perf-1229bc`
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
  - *(working tree)* playback concurrency + precomputed timeline ranges

## What the fork does

One physical camera can now record **two streams as distinct variants** —
`main` (HD) and `sub` (SD) — each a separate row in `Recordings` and a separate
file tree on disk. The two key user-facing capabilities:

1. **Dual retention** — each record variant can have its own retention via a
   per-input `retain_days` override (e.g. keep `sub` 30 days while `main`/base is 7).
2. **Variant-aware playback/export/snapshot APIs** — every recordings-facing
   endpoint accepts a `?variant=` selector (`main`/`sub`/`all`) with a `sub`
   default for playback, `main` for snapshots, and automatic fallback to the other
   variant when the requested one has no footage in range.

Detailed external-client/API behavior lives in
[ra-docs/dual-stream-api-changes.md](ra-docs/dual-stream-api-changes.md) and
[ra-docs/be-migration-dual-stream.md](ra-docs/be-migration-dual-stream.md). This
file documents the **code** changes.

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
  - **Bug fix (working tree):** the per-variant *outer/motion* window was capped
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

### Infra
- **`docker/main/rootfs/usr/local/nginx/conf/nginx.conf`** — `vod_upstream_extra_args`
  so query args reach the vod upstream (path-form variant remains canonical).
- **`.github/workflows/*` and `.github/actions/setup`** — CI workflows trimmed for
  the fork. *(Not feature-related; review before merging upstream.)*

---

## Playback performance & the precomputed timeline

A second strand of fork work, driven by a device recording 32 cameras on a
4-core QNAP with limited RAM, serving 4 concurrent VOD players.

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
- Stored at `MATERIALIZED_GAP = 1.0s`; a caller's larger `gap` is served by
  re-merging, which is equivalent to merging the raw segments at that gap.
  A smaller gap falls through to the live path.

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

`GET /api/config` carries a top-level `fork` key listing supported fork-only
features, so clients on a mixed-version fleet can detect endpoints without
probing for 404s. **Append only** — never rename or remove a shipped flag.

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
- **`src/types/record.ts`** — variant types.
- **`public/locales/en/components/player.json`** — strings for the variant selector.

---

## Tests

- **`frigate/test/test_config.py`** — dual-variant config validation
  (`test_dual_record_distinct_variants_ok`, duplicate-variant rejection).
- **`frigate/test/http_api/test_http_media.py`** + **`base_http_test.py`** —
  variant-aware media endpoint tests; `insert_mock_recording` gained `variant`/`path`.
- **`frigate/test/test_record_cleanup.py`** (new, working tree) — exercises
  `RecordingCleanup.expire_recordings()` end-to-end and guards the per-variant
  retention regression (sub retained to its 30-day override; main to base 7 days).

Run backend tests inside the image (local Python lacks the deps):
```bash
docker run --rm --workdir=/opt/frigate --entrypoint= \
  -v "$PWD:/opt/frigate" <frigate-image> \
  python3 -u -m unittest frigate.test.test_record_cleanup frigate.test.test_config
```

---

## Housekeeping / known issues

- **Stray `.pyc` files** are tracked/untracked in the repo
  (`frigate/__init__.pyc`, `frigate/config/__init__.pyc`,
  `frigate/detectors/__init__.pyc`, `frigate/test/**/__init__.pyc`). These are build
  artifacts (compiled by the local Python 2.7 on Windows) and should be removed and
  gitignored.
- Two pre-existing test-environment failures to expect and ignore:
  `TestGo2rtcStreamAccess` needs a live go2rtc on `127.0.0.1:1984`, and mypy needs
  `types-peewee` installed or every peewee model reports
  "Class cannot subclass Model".
- `ra-docs/` holds working notes; `dual-stream-api-changes.md` and this file are
  tracked, the rest are local. Note this file lives in `ra-docs/`, not the repo
  root (CLAUDE.md still says root).
