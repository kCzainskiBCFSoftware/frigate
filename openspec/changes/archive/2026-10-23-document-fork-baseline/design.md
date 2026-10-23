# Design

## Context

This design records decisions that are already implemented on
`v17.1-update-with-adaptive`; nothing here is still to be built. The motivation
is in `proposal.md`, and the requirements are in `specs/`. For the
file-by-file description, see `ra-docs/fork-changes.md`.

Where each capability lives:

| Capability | Main code |
|---|---|
| `recording-variants` | `frigate/config/camera/{ffmpeg,camera}.py`, `frigate/record/maintainer.py`, `frigate/models.py` + migration `033`, `frigate/storage.py`, `frigate/api/review.py` |
| `variant-retention` | `frigate/record/cleanup.py` (`expire_recordings`) |
| `variant-media-api` | `frigate/record/variants.py`, `frigate/api/media.py`, `frigate/api/export.py`, `frigate/record/export.py`, nginx `vod_upstream_extra_args` |
| `playback-timeline-api` | `frigate/record/ranges.py`, `frigate/api/media.py`, migrations `035`/`036`, the ranges rollup on `RecordingCleanup`'s tick |
| `playback-serving` | `frigate/api/fastapi_app.py` (thread pool), handler signatures in `frigate/api/media.py`, `frigate/api/perf.py`, `nginx.conf` |
| `fork-capabilities` | `frigate/api/app.py`, `FORK_FEATURES` in `frigate/const.py` |
| `web-navigation` | `web/src/App.tsx`, `components/navigation/IndexRedirect.tsx`, `components/auth/*`, `RestartDialog.tsx`, `pages/ConfigEditor.tsx` |
| `playback-variant-preference` | `web/src/hooks/use-recording-playback-preference.ts`, `components/player/RecordingPlaybackPreferenceSelect.tsx`, `DynamicVideoPlayer.tsx`, `ExportDialog.tsx`, `TrackingDetails.tsx` |

## Goals / Non-Goals

**Goals:**
- Make the fork's divergence from upstream reviewable as behavior, separate
  from the code.
- Give future fork changes a baseline to write deltas against.

**Non-Goals:**
- Specifying upstream Frigate behavior the fork does not change.
- Specifying internal tuning (SQLite pragmas, nginx cache sizes, indexes) that
  has no externally visible contract. That stays in `ra-docs/fork-changes.md`.

## Decisions

- **Variants are rows of the same camera, not separate cameras.** Detection,
  events, review and previews stay camera-level, and only recordings carry a
  variant. *Alternative:* configure the SD stream as a second camera. That
  duplicates detection and splits events and history across two names.
- **`main` is mandatory and is the legacy variant.** Pre-upgrade rows are
  backfilled to `main`, so `main` always exists as a fallback target and legacy
  footage keeps expiring.
- **Playback defaults to `sub`, snapshots to `main`.** On a 32-camera NAS, SD
  playback is what makes four concurrent players affordable, and stills
  benefit from HD. *Alternative:* default to `main` to stay upstream-compatible.
  Rejected, because clients that omit `variant` would then get the expensive
  stream. That is why the spec marks this default **BREAKING**.
- **Fallback is per request and per camera, never with `all`.** History from
  before a camera's upgrade only has `main`. Without a fallback, the SD default
  would show an empty timeline for that period.
- **Path-form variant on VOD URLs is canonical.** The nginx VOD module keys its
  mapping cache on the path and drops query arguments when it builds the
  upstream request. `vod_upstream_extra_args` restores them, but the path form
  is the one that is safe to cache.
- **A retention override replaces continuous retention and is also a hard
  cap.** The outer window is `max(base, override)`. The earlier `min(...)`
  silently cut SD retention back to the base value (bug fixed in `648fc516`).
- **Timeline ranges are precomputed at a 2.0 s gap and merged again for larger
  caller gaps.** Merging again from a smaller stored gap gives the same result
  as merging raw segments at the larger gap. A smaller caller gap falls through
  to a live merge. Coverage is tracked separately, so an uncovered window is
  merged live and never reported as "no footage".
- **Merge on the running `max(end)`, and return ranges unclipped.** Segments
  overlap in the field. Merging on the previous row's end loses footage, and
  clipping at the window edge leaves a visible hole at midnight.
- **Blocking handlers are plain `def` on a 4-thread pool.** uvicorn has one
  event loop, and a blocking `async` handler stalls everything. anyio's default
  of 40 threads would mean 40 SQLite connections, each with its own cache, on a
  RAM-limited box that is also recording every camera.
- **Feature discovery goes through `GET /api/config`.** Clients already fetch
  it, so feature detection costs no extra request. Probing for 404s does not
  tell an old build apart from a misconfigured proxy.

## Risks / Trade-offs

- [Upstream rebase conflicts in `media.py`, `cleanup.py`, `maintainer.py` and `nginx.conf`] → Each proposal states its upstream merge impact (see `openspec/config.yaml`), and the capability-to-code map above shows where to look.
- [External clients silently switch to SD] → Documented in `ra-docs/dual-stream-api-changes.md`. Clients that need HD pass `variant=main`.
- [Precomputed ranges drift from the recordings table] → Ranges are trimmed after retention runs, a coverage invariant test guards the replace-window logic, and migration `036` resets the tables after the under-reporting bug (see `ra-docs/ranges-underreporting-postmortem.md`).
- [Specs drift from code, because this baseline was written after the fact] → Later fork changes go through OpenSpec change proposals, and archiving them updates these specs.
