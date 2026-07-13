# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Frigate NVR — a local network video recorder with realtime AI object detection for IP cameras.
Python 3.13+ backend (FastAPI, OpenCV, TensorFlow/ONNX, Peewee/SQLite) plus a React + TypeScript +
Vite frontend in `web/`. This is a **fork** of `blakeblackshear/frigate`; the active branch adds
dual-stream recording (main/sub variants — see "Fork-specific work" below).

A coding-standards reference already exists at `.github/copilot-instructions.md` (Python/async/i18n
conventions, logging, error handling). Read it before writing code; this file does not repeat it.

## Commands

Backend (run from repo root):
```bash
python3 -u -m unittest                                  # all tests
python3 -u -m unittest frigate.test.test_ffmpeg_presets # a single test module
ruff check frigate/                                     # lint
ruff format frigate/                                    # format
python3 -m mypy --config-file frigate/mypy.ini frigate  # type check
```

Frontend (run from `web/`):
```bash
npm run dev            # dev server (do NOT start unless asked)
npm run build          # tsc + vite build
npm run lint           # eslint
npm run test           # vitest
```

Docker / running the full app (do NOT run unless asked — heavy, Linux-targeted):
```bash
make local             # build frigate:latest image
make run               # build + run, publishes :5000, mounts ./config
make run_tests         # run unittest + mypy inside the image
```

Note: the dev environment here is Windows, but Frigate runs as a Linux container and hardcodes Linux
paths (`/config`, `/media/frigate`, `/opt/frigate`, `/tmp/cache` — see `frigate/const.py`). Tests are
the practical way to verify backend changes locally; full runtime needs Docker or the devcontainer
(`.devcontainer/`).

## Architecture

**Multiprocess by design.** `frigate/__main__.py` uses the `forkserver` start method and launches
`FrigateApp` (`frigate/app.py`). `FrigateApp.start()` is the canonical map of the system — it spins up
each subsystem as its own process/thread in order, then runs the FastAPI app under uvicorn on
`127.0.0.1:5001` (nginx fronts it on :5000/:8971). When adding a subsystem, wire it into both
`start()` and `stop()`.

Major processes/threads started by `FrigateApp`:
- **CameraMaintainer** (`frigate/camera/`) — per-camera capture + decode, motion detection, feeds the
  detection queue and the detected-frames queue.
- **ObjectDetectProcess** (`frigate/object_detection/`, `frigate/detectors/`) — runs inference;
  communicates with cameras via shared memory (`UntrackedSharedMemory`) + a detection queue.
- **TrackedObjectProcessor** (`frigate/track/`) — consumes detections, tracks objects, drives events.
- **RecordProcess** (`frigate/record/`) and **ReviewProcess** (`frigate/review/`) — recording segments
  and review-segment generation.
- **OutputProcess** (`frigate/output/`) — live view / restream output.
- **EmbeddingProcess** (`frigate/embeddings/`, `frigate/data_processing/`) — semantic search, face
  recognition, LPR, GenAI, custom classification. Only does work when the relevant config is enabled.
- **AudioProcessor**, **TimelineProcessor**, **EventProcessor**, cleanup/storage maintainers, stats
  emitter, watchdog.

**Inter-process communication** (`frigate/comms/`): a ZMQ proxy plus typed publisher/updater pairs
(config, detections, events, recordings, review, embeddings). The `Dispatcher`
(`frigate/comms/dispatcher.py`) is the hub — it fans messages out to communicators (MQTT, WebSocket,
WebPush, inter-process) and handles inbound topic commands. Outbound app state generally goes through
`Dispatcher.publish(topic, payload)`.

**Config** (`frigate/config/`): Pydantic models, entrypoint `FrigateConfig.load()` in
`frigate/config/config.py`. Loaded once at startup; `--validate-config` validates and exits; invalid
config falls back to "safe mode". Camera config updates propagate at runtime via
`CameraConfigUpdatePublisher`.

**Database** (`frigate/models.py`, `frigate/db/`): Peewee ORM over SQLite (with sqlite-vec for
embeddings). Models: `Event`, `Recordings`, `ReviewSegment`, `Previews`, `Export`, `Timeline`,
`User`, `Trigger`, etc. Schema changes require a **peewee-migrate** migration in `migrations/`
(numbered sequentially, e.g. `033_add_recordings_variant.py`); migrations run automatically at startup
in `FrigateApp.init_database()` (which backs up the DB first).

**API** (`frigate/api/`): FastAPI routers (`event.py`, `media.py`, `review.py`, `export.py`,
`preview.py`, `camera.py`, `auth.py`, `notification.py`, `classification.py`). Assembled in
`fastapi_app.py`; route tags in `api/defs/`. Access config/db via `request.app.frigate_config` etc.

**Frontend** (`web/`): React + Vite + TailwindCSS + Radix UI. `pages/` (routes), `views/` (complex
views), `components/`, `hooks/`, `api/` (client), `types/`. All user-facing strings MUST go through
react-i18next (`t()`) with English source in `web/public/locales/en/` — enforced by a Cursor rule.

## Fork-specific work: dual-stream recording + dual retention

This branch adds recording of both main and sub camera streams as distinct "variants", each with its
own retention. **`fork-changes.md` (repo root) is the canonical changelog — read/update it for the full
list.** Key pieces:
- `frigate/record/variants.py` — variant constants (`main`/`sub`/`all`) and query helpers (incl.
  `resolve_playback_variant` fallback). Defaults: playback uses `sub`, snapshots use `main`.
  Time-window queries on `Recordings` MUST use `recordings_overlap_clause()` — the index-seekable
  overlap form; hand-written `BETWEEN`-OR overlap predicates regress to full-history scans
  (guarded by an `EXPLAIN QUERY PLAN` test in `frigate/test/http_api/test_http_media.py`).
- Config: `record_variant` and per-variant `retain_days` override on `CameraInput`
  (`frigate/config/camera/ffmpeg.py`); accessors `get_record_variants()` /
  `get_variant_retain_days()` on `CameraConfig` (`frigate/config/camera/camera.py`).
- `Recordings.variant` column + codec/width/height/bitrate (migration `033_add_recordings_variant.py`).
- The variant label is part of the recording cache filename (`camera@variant@date`) and the on-disk
  path (`recordings/{date}/{hour}/{camera}/{variant}/...`).
- **Per-variant retention** lives in `frigate/record/cleanup.py` (`expire_recordings`): a variant's
  `retain_days` override replaces the base continuous window and acts as a hard cap. This path is
  covered by `frigate/test/test_record_cleanup.py` — keep it green when touching retention.
- API behavior (`?variant=`, path-form `/vod/`, fallback) documented in
  `ra-docs/dual-stream-api-changes.md` (tracked); other `ra-docs/` files are local working notes.

When touching recording, media playback, exports, or the recordings API, account for the `variant`
dimension and use the helpers in `record/variants.py` rather than filtering on the column directly.
