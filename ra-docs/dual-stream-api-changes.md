# Dual-Stream Recording: API Changes & Client Migration Guide

This document describes everything that changed in the Frigate HTTP API after
the dual-stream recording feature was added, and what (if anything) external
clients need to adjust.

> **TL;DR**
> - **Recordings/VOD/clip/snapshot endpoints** gained a new optional `?variant=` query parameter (`main`, `sub`, or `all`).
> - **Defaults**: playback-oriented endpoints default to `sub` (SD). Snapshot extraction defaults to `main` (HD).
> - **Backward-compatible**: clients that don't pass `?variant=` will keep working, but on dual-stream cameras they will silently start receiving the SD stream for playback. Clients that need HD must explicitly pass `?variant=main`.
> - **Built-in fallback**: VOD/clip endpoints automatically fall back to the other variant if the requested one has no overlapping recordings. Playback never breaks just because a variant has a gap.
> - **Live streams, event thumbnails, scrubbing previews**: unaffected. Nothing to change for those.

---

## 1. Quick conceptual model

Before the change, one Frigate camera = one recording stream. Now one Frigate
camera can have **N recording variants** (typically `main` for HD and `sub` for
SD), each identified by a label set in the camera's input config:

```yaml
cameras:
  3-Cam-3:
    ffmpeg:
      inputs:
        - path: rtsp://.../3-Cam-3        # HD
          roles: [record]
          record_variant: main            # <— label
        - path: rtsp://.../3-Cam-3-SD     # SD
          roles: [detect, record]
          record_variant: sub             # <— label
          retain_days: 30                 # <— per-variant retention override
```

The `Recordings` DB table gained a `variant` column (and `codec_name`, `width`,
`height`, `bitrate`, `transcoded_from_main` for completeness). Every recording
row now belongs to a `(camera, variant)` pair.

On disk:
- **New** recordings: `recordings/{date}/{hour}/{camera}/{variant}/{MM.SS.mp4}`
- **Legacy** recordings (pre-upgrade) stay at `recordings/{date}/{hour}/{camera}/{MM.SS.mp4}` and are treated as `variant=main` (backfilled by migration 033).

---

## 2. Endpoint-by-endpoint changes

In every signature below, `variant` accepts:
- `main` — HD stream
- `sub` — SD stream (DEFAULT for playback)
- `all` — no filter (returns/serves rows from all variants)

If you pass any other value (e.g. an unknown variant name), the endpoint will
filter by that literal string — so on dual-stream cameras the result will simply
be empty unless that variant exists.

### 2.1 Recording listing & summary

| Endpoint | Default `variant` | Notes |
|---|---|---|
| `GET /api/{camera}/recordings?after=&before=&variant=` | `sub` | Response rows now include `variant`, `codec_name`, `width`, `height`, `bitrate`. |
| `GET /api/{camera}/recordings/summary?timezone=&variant=` | `sub` | Hourly motion/objects/duration aggregated per variant. |
| `GET /api/recordings/summary?cameras=&...` | **not filtered** | "Has any recording for day X" across all cameras and all variants. Behavior unchanged. |
| `GET /api/recordings/unavailable` | **not filtered** | Same as before. |
| `GET /api/recordings/storage` | **not filtered** | Per-camera totals only. (Per-variant breakdown is a planned UI enhancement.) |

**Response shape addition** for `GET /api/{camera}/recordings`:
```json
[
  {
    "id": "1717245720.5-abc123",
    "start_time": 1717245720.5,
    "end_time": 1717245730.5,
    "segment_size": 0.92,
    "motion": 42,
    "objects": 1,
    "duration": 10.0,
    "variant": "sub",
    "codec_name": "h264",
    "width": 704,
    "height": 480,
    "bitrate": 614400
  }
]
```
The first 7 fields are unchanged. The last 5 are new and may be `null` for
legacy recordings.

### 2.2 VOD / HLS playback

| Endpoint | Default `variant` | Fallback |
|---|---|---|
| `GET /vod/{camera}/start/{start}/end/{end}?variant=` | `sub` | If no segments in range, returns the OTHER variant instead of a 404. |
| `GET /vod/{year_month}/{day}/{hour}/{camera}?variant=` | `sub` | Same fallback. |
| `GET /vod/{year_month}/{day}/{hour}/{camera}/{tz}?variant=` | `sub` | Same fallback. |
| `GET /vod/event/{event_id}?variant=&padding=` | `sub` | Same fallback. |
| `GET /vod/clip/{camera}/start/{start}/end/{end}?variant=` | `sub` | Same fallback. Used by export composition. |

These are the master/HLS playlist endpoints. The browser appends
`/master.m3u8` or `/index.m3u8` to them; both forms accept the `?variant=`
query parameter unchanged.

### 2.3 Clip download / export

| Endpoint | Default `variant` |
|---|---|
| `GET /api/{camera}/start/{start_ts}/end/{end_ts}/clip.mp4?variant=` | `sub` |

This is the progressive MP4 endpoint used by the Export dialog. To export a
clip in HD, pass `?variant=main`. The endpoint also uses the same fallback
logic — if the requested variant has no segments, it serves segments from the
other one rather than returning empty.

### 2.4 Snapshot from a recording

| Endpoint | Default `variant` |
|---|---|
| `GET /api/{camera}/recordings/{frame_time}/snapshot.{jpg\|png}?height=&variant=` | **`main`** |
| `POST /api/{camera}/plus/{frame_time}?variant=` | **`main`** |

Snapshots default to **HD** (`main`) because typical use cases for these
endpoints — Frigate+ submissions, History detail thumbnails, full-resolution
saves — want the higher quality image. They also fall back to the other
variant if `main` has no segment covering `frame_time`.

### 2.5 Endpoints that did NOT change

These are explicitly **not** variant-aware. Their behavior is the same as
before because the underlying data was never per-variant:

- **Event thumbnails**: `GET /api/events/{event_id}/thumbnail.{ext}` — thumbnail bytes are stored in the `Event` row, not extracted from a recording variant.
- **Event snapshots**: `GET /api/events/{event_id}/snapshot.jpg` — comes from the detect pipeline (a tracked-object best image), not from a recording. Resolution = detect stream resolution.
- **Preview videos**: `GET /api/{camera}/start/{start}/end/{end}/preview.{ext}` and `GET /api/preview/{camera}/start/{start}/end/{end}/frames` — previews are a separate `Previews` table generated from the detect frames. They are camera-level, not variant-level. Retained until no variant in the same time window has a kept recording.
- **MJPEG / latest frame / grid / live**: `/api/{camera}` (MJPEG), `/api/{camera}/latest.{ext}`, `/api/{camera}/grid.jpg`, `/api/{camera}/probe`, the websocket live endpoints — all read from the live detect stream, not recordings. Unaffected.
- **Camera-list `/api/recordings/summary`** (no camera name): returns "has any recording on day X" per camera; not filtered by variant by design (returns true if either variant has data).
- **`/api/recordings/unavailable`**: time ranges with NO recordings, across all cameras and all variants — not variant-specific.
- **Review/alerts/detections endpoints** (`/api/review/*`): operate on review segments, which are camera-level. The motion activity sub-endpoint (`/api/review/activity/motion`) internally filters to `variant="main"` to avoid double-counting; no client change needed.
- **Exports table** (`/api/export/*`): the export *file* is produced by composing recording segments via `/vod/clip/...`, so it inherits the variant chosen there. The export DB rows themselves carry no variant.
- **Config and admin endpoints**: unchanged.

---

## 3. Impact on the things you mentioned

> *"I use both export, live playback view, thumbnails, previews etc."*

Here is the breakdown for each.

### 3.1 Live playback view
**Unaffected.** Live view uses go2rtc / jsmpeg / MSE-LL, none of which touch
the recordings table or the VOD endpoints. The go2rtc `streams` block already
maps both `N-Cam-N` (HD) and `N-Cam-N-SD` (SD) for every physical camera. You
can keep using either URL directly. The Frigate UI's live view will use go2rtc
restream named the same as the Frigate camera (`N-Cam-N`), which is the HD
feed.

If you want the live tile to play the SD feed instead (lower bandwidth), point
the camera's `live` config at the SD go2rtc stream:

```yaml
cameras:
  3-Cam-3:
    live:
      stream_name: 3-Cam-3-SD
```

This is independent of recording variants.

### 3.2 Recording playback (timeline, history, scrubbing)
**Default behavior changes** for dual-stream cameras: the player now requests
SD by default. The new in-player Auto/Main/Sub dropdown (top-right of the
video element) lets the user override per camera and the choice is persisted.

If you have a **custom client** (mobile app, Home Assistant integration,
external dashboard) that hits the recordings/VOD endpoints directly:
- **No code change required** to keep working — endpoints stay backward-compatible.
- On dual-stream cameras the client will silently start receiving SD playback.
- To keep HD playback in that custom client: append `?variant=main` to:
  - `/api/{camera}/recordings`
  - `/api/{camera}/recordings/summary`
  - `/vod/{camera}/start/{start}/end/{end}/master.m3u8`
  - `/vod/{year_month}/{day}/{hour}/{camera}/master.m3u8`
  - `/vod/event/{event_id}/master.m3u8`
  - `/vod/clip/{camera}/start/{start}/end/{end}/master.m3u8`

### 3.3 Exports
**Default behavior changes**: the Export dialog and the export compositor pull
segments from the SD variant by default (smaller files, faster download). If
you want to export the HD clip, pass `?variant=main` to:
- `GET /api/{camera}/start/{start_ts}/end/{end_ts}/clip.mp4?variant=main`
- `GET /vod/clip/{camera}/start/{start}/end/{end}/master.m3u8?variant=main`

If you previously had bookmarks, scripts, or HA service calls that download
clips, those will keep working but will produce SD-resolution output unless
you add `?variant=main`.

### 3.4 Thumbnails
**Unaffected.** Two cases:

1. **Event/object thumbnails** — stored in the `Event` row, never depended on
   the recording stream. Same endpoint, same image.
2. **Snapshot extracted from a recording** (e.g. when the History detail view
   asks for a still at a specific timestamp): now defaults to MAIN (HD), so
   the image is sharper. If you want SD specifically, pass `?variant=sub`. No
   change needed if you want the default behavior.

### 3.5 Previews
**Unaffected.** Previews come from a separate `Previews` table built from
detect-frame downscaled video. There is no per-variant preview. The cleanup
logic was updated to keep a preview if **any** recording variant in the same
time window survives, so previews don't get deleted prematurely on dual-stream
cameras.

### 3.6 Snapshots
**Unaffected for live snapshots** (`/api/{camera}/latest.jpg` etc.). The
"snapshot from a recording" endpoint defaults to HD now, as described in 3.4.

---

## 4. Migration checklist for external clients

If you build any of the following, here is what to consider:

### Custom mobile/web app showing recordings
- [ ] Decide your default: SD (fast load) or HD (quality). The server default
      is SD for playback.
- [ ] If you want HD, append `?variant=main` to `/api/{camera}/recordings` and
      `/vod/.../master.m3u8`.
- [ ] If you want to give users a choice, add a UI toggle and pass the chosen
      value through to all recording-related calls for that camera/session.
- [ ] Read the new `variant`, `codec_name`, `width`, `height`, `bitrate`
      fields from `/api/{camera}/recordings` if you display stream metadata.

### Custom export / clip download
- [ ] Add `?variant=main` to keep getting HD .mp4 exports.
- [ ] Without it, you'll get the SD variant — much smaller files, fine for
      "send the clip to someone" use cases.

### Home Assistant integration / scripts
- [ ] If you use `frigate.notifications` or call the API directly, decide
      per-purpose: clip-in-notification → likely SD; manual download → likely
      HD.

### Live integrations (WebRTC / RTSP)
- [ ] **No changes required.** Continue to use the existing go2rtc URLs.

### Snapshot integrations
- [ ] **Event snapshots**: no change. They never came from recordings.
- [ ] **Snapshot from recording at timestamp**: defaults to HD now. If your
      client was relying on the previous (only) variant being implicitly
      whatever the camera recorded, the result will be the HD variant on
      dual-stream cameras. Usually that's an improvement; if you specifically
      need SD, pass `?variant=sub`.

---

## 5. Endpoint quick-reference table

| Use case | Endpoint | New default | Pass `variant=main` for HD? |
|---|---|---|---|
| List recordings for a camera | `GET /api/{camera}/recordings` | `sub` | Yes |
| Per-camera hourly summary | `GET /api/{camera}/recordings/summary` | `sub` | Yes |
| HLS playback | `GET /vod/{camera}/start/{s}/end/{e}/master.m3u8` | `sub` | Yes |
| HLS for an event | `GET /vod/event/{event_id}/master.m3u8` | `sub` | Yes |
| Progressive MP4 clip | `GET /api/{camera}/start/{s}/end/{e}/clip.mp4` | `sub` | Yes |
| Snapshot from recording | `GET /api/{camera}/recordings/{t}/snapshot.jpg` | **`main`** | No (already HD) |
| Frigate+ submission | `POST /api/{camera}/plus/{t}` | **`main`** | No |
| Event thumbnail | `GET /api/events/{event_id}/thumbnail.{ext}` | n/a | n/a |
| Event snapshot (from detect) | `GET /api/events/{event_id}/snapshot.jpg` | n/a | n/a |
| Camera preview frames | `GET /api/preview/{camera}/...` | n/a | n/a |
| Live MJPEG | `GET /api/{camera}` | n/a | n/a |
| Latest frame | `GET /api/{camera}/latest.{ext}` | n/a | n/a |
| Daily availability summary | `GET /api/recordings/summary` (no camera) | n/a (not filtered) | n/a |

---

## 6. Fallback behavior in detail

For VOD and clip endpoints, the server runs a **single fallback check** before
serving:

1. Client requests `variant=X`.
2. Server checks if camera has ANY recording with `variant=X` overlapping the
   requested time range.
3. If yes → serves variant X.
4. If no, server checks the OTHER variant. If it has overlapping recordings →
   serves the other one (and logs at DEBUG level).
5. If neither → returns empty / 404 (HLS playlists return empty `clips`).

This means a missing sub-stream window won't break event playback — the player
will silently get main coverage for that window. For dual-stream sites this is
mostly invisible.

If you want to **disable** the fallback in your client (e.g. you want to know
that no SD exists), pass `?variant=all` and inspect each row's `variant`
field, or query the camera's variant inventory via `/api/{camera}/recordings/summary`
with explicit variant values.

---

## 7. Disk-layout consequences (only relevant if you read recordings directly)

If you have tooling that walks the disk (e.g. an archive job or backup
script) and parses recording paths:

- **New paths** include the variant subdirectory:
  `recordings/2026-06-05/14/3-Cam-3/main/22.34.mp4`
  `recordings/2026-06-05/14/3-Cam-3/sub/22.34.mp4`
- **Legacy paths** (pre-upgrade) have no variant subdirectory and are still
  served as variant=main:
  `recordings/2026-06-05/14/3-Cam-3/22.34.mp4`

Frigate's own sync (`sync_recordings`) handles both layouts transparently —
no code change there. External tooling that filters by path depth should be
made tolerant of the extra level.

---

## 8. DB schema additions (only relevant if you query the DB directly)

The `recordings` table gained these columns (migration 033):

| Column | Type | Nullable | Notes |
|---|---|---|---|
| `variant` | VARCHAR(20) | NO | Default `"main"`. Backfilled to `"main"` for pre-existing rows. |
| `codec_name` | VARCHAR(50) | YES | e.g. `h264`, `hevc`. May be null for legacy rows. |
| `width` | INTEGER | YES | Pixels. May be null for legacy rows. |
| `height` | INTEGER | YES | Pixels. May be null for legacy rows. |
| `bitrate` | INTEGER | YES | Bits per second. May be null for legacy rows. |
| `transcoded_from_main` | INTEGER | NO | Default `0`. Reserved for an optional Phase-2 transcoding feature; always `0` today. |

New indexes:
- `recordings_variant (variant)`
- `recordings_camera_variant_start_time_end_time (camera, variant, start_time DESC, end_time DESC)`

If you have external SQL queries against `recordings`, decide whether you
want to add `AND variant = 'main'` (or `'sub'`) to them. Without the filter,
dual-stream cameras will return roughly 2× the row count compared to before
for the same time range.

---

## 9. Examples

### 9.1 Get the last hour of HD recordings for one camera
```sh
curl 'http://frigate.example/api/3-Cam-3/recordings?variant=main'
```

### 9.2 Get all variants for the last hour
```sh
curl 'http://frigate.example/api/3-Cam-3/recordings?variant=all'
```
Then filter client-side on the `variant` field of each row.

### 9.3 Build an HD HLS playlist for an event
```sh
curl 'http://frigate.example/vod/event/1717245720.5-abc/master.m3u8?variant=main'
```

### 9.4 Export the HD clip
```sh
curl -o clip.mp4 'http://frigate.example/api/3-Cam-3/start/1717245700/end/1717245800/clip.mp4?variant=main'
```

### 9.5 Extract an HD snapshot at a specific timestamp
```sh
curl -o snap.jpg 'http://frigate.example/api/3-Cam-3/recordings/1717245725.0/snapshot.jpg'
# variant=main is already the default for snapshots
```

### 9.6 Live WebRTC URL (unchanged)
```
ws://frigate.example:8555/api/ws?src=3-Cam-3        # HD
ws://frigate.example:8555/api/ws?src=3-Cam-3-SD     # SD
```

---

## 10. Decision matrix: when do I need to change my client code?

| You do this today... | After the change... |
|---|---|
| Embed the live MJPEG / WebRTC / RTSP stream | **No change.** |
| Show the event card thumbnail | **No change.** |
| Show the event "best snapshot" | **No change.** |
| Show camera preview scrubbing | **No change.** |
| Show timeline playback in your own player using `/vod/...` | Decide if you want default SD (no change) or HD (`?variant=main`). |
| Build an event-clip player via `/vod/event/{id}/...` | Same decision as above. |
| Download a clip via `/api/{cam}/start/{s}/end/{e}/clip.mp4` | If you need HD output, add `?variant=main`. Default is now SD. |
| Extract a thumbnail from a specific recording timestamp | **No change** (already defaults to HD). |
| List recordings for a calendar/summary UI | If you want HD-only or both, add `?variant=main` or `?variant=all`. |
| Query the DB directly | Add `AND variant=...` to queries; consume the new columns. |
