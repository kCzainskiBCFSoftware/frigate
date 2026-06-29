# Fork Changes — Dual-Stream Recording with Per-Variant Retention

This document is the canonical changelog for what this fork/branch adds on top of
upstream `blakeblackshear/frigate`. It is maintained by hand — update it whenever
you change fork-specific behavior.

- **Upstream base:** `dev` (merge-base `416a9b76`)
- **Active branch:** `v17.1-update-with-adaptive`
- **Branch commits:**
  - `4228f609` feat: Add support for dual retention for single camera
  - `87d52ed7` test: attempt 2
  - `b1ffe9c4` fix: hls updates
  - `f1795069` fix: Update recordings/summary endpoint response times
  - *(working tree)* fix: per-variant retention window (`min`→`max`) + cleanup regression test

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
- The **cleanup retention fix** and **`test_record_cleanup.py`** are uncommitted in
  the working tree at the time of writing.
- `ra-docs/` holds working notes; only `dual-stream-api-changes.md` is tracked, the
  rest are local.
