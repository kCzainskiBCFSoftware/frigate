# Proposal: document the fork baseline

## Why

The fork has moved 14 commits and ~60 files away from upstream Frigate v0.17.1
(`416a9b76`). The only record of that work is a code-level changelog
(`ra-docs/fork-changes.md`) and two client guides. Nothing states, in one
place and in testable terms, which behavior differs from upstream. This change
records the behavior that already ships on `v17.1-update-with-adaptive` as
baseline specs, so later changes start from an explicit contract and show up as
deltas against it.

This change adds no code. It specs what the branch does today.

## What Changes

Behavior the fork adds or changes on top of upstream v0.17.1:

- **Dual-stream recording.** A camera can record two inputs as separate
  variants, `main` (HD) and `sub` (SD). Each segment carries its variant label,
  codec and resolution, and is stored under its own directory on disk.
- **Per-variant retention.** A record input can set its own `retain_days`,
  which replaces the camera's continuous retention for that variant and is
  also a hard cap on how long that variant is kept.
- **Variant-aware media API.** Recording, VOD, clip, snapshot and export
  endpoints accept `variant`. **BREAKING** for external clients: on dual-stream
  cameras, playback and exports now default to SD (`sub`), and snapshots to HD
  (`main`). If the requested variant has no footage, the endpoint serves the
  other one.
- **Playback timeline API.** Three new, additive endpoints return merged
  footage ranges (one camera, or many cameras in one request) and hourly
  availability. They are backed by precomputed ranges and correct before
  precomputation finishes.
- **Playback serving.** Blocking playback handlers no longer stall the API
  event loop. Settled VOD mappings are cacheable. `/api/` JSON is
  gzip-compressed. Handlers can set their own `Cache-Control`. A per-request
  performance log is written next to the database.
- **Fork capability advertisement.** `GET /api/config` returns a `fork` key
  with the build version and an append-only list of feature flags.
- **Web UI navigation.** `/` redirects admins to the config editor and other
  users to `/live`. After login, users return to the page they first asked for.
  Restarting from the config editor reloads the config editor.
- **Playback variant preference (web UI).** The player has an Auto/Main/Sub
  selector that is saved per camera. Recording playback, event clips and
  exports all use the selected variant.

Not specced, because they change no product behavior:

- CI was reworked for the fork. Images go to a private ECR registry, and only
  the release branch moves `latest`. Python lint checks are disabled and the
  stale-issue bot is off.
- Database tuning: SQLite page-cache and mmap limits, `ANALYZE` at startup and
  every hour, an index-seekable overlap predicate, and migration `034`, which
  drops two redundant indexes.
- nginx memory sizing for the VOD caches, plus a larger open-file cache.
- Stray `.pyc` files removed from tracking.

## Capabilities

### New Capabilities

- `recording-variants`: capture and storage of more than one recorded stream per camera (`main`/`sub`): config, validation, on-disk layout, per-segment metadata, and how camera-level figures (storage rate, motion activity) treat variants.
- `variant-retention`: per-variant retention overrides, hard caps, removing footage of variants that are no longer configured, and how previews are kept across variants.
- `variant-media-api`: the `variant` selector on recording, VOD, clip, snapshot and export endpoints: defaults, allowed values, validation, path-form VOD URLs and fallback to the other variant.
- `playback-timeline-api`: merged footage ranges for one camera or many, hourly availability, the `gap` parameter, window limits, caching headers, and correctness while ranges are still being precomputed.
- `playback-serving`: playback requests run concurrently, settled VOD mappings are cacheable, `/api/` JSON is compressed, handlers can set `Cache-Control`, and each API request is logged with timings.
- `fork-capabilities`: the `fork` block on `GET /api/config` that lets clients detect fork-only features.
- `web-navigation`: where the web UI sends users at `/`, after login and after a restart from the config editor.
- `playback-variant-preference`: the per-camera Auto/Main/Sub stream selector in the web player and where the choice is applied.

### Modified Capabilities

None. No specs existed before this change.

## Impact

- **Code:** none. Specs only.
- **Upstream files the fork already touches:** heavily, `frigate/api/media.py`,
  `frigate/record/cleanup.py`, `frigate/record/maintainer.py` and
  `docker/main/rootfs/usr/local/nginx/conf/nginx.conf`. Lightly, `frigate/models.py`,
  `frigate/config/camera/{camera,ffmpeg}.py`, `frigate/storage.py`,
  `frigate/api/{app,export,review,fastapi_app}.py`, `frigate/app.py` and several
  `web/src` navigation and player components. Expect conflicts in these files
  when rebasing onto a newer upstream release.
- **Fork-only files:** `frigate/record/{variants,ranges}.py`, `frigate/api/perf.py`,
  migrations `033`–`036`, and `ra-docs/`.
- **Database:** migrations `033` (variant columns, backfilled to `main`), `034`
  (drops indexes), `035` (creates the two ranges tables, DDL only) and `036`
  (empties those tables so they rebuild).
