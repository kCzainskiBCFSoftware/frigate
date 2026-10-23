# variant-media-api Specification

## Purpose
Let clients choose which recorded stream of a camera, `main` (HD) or `sub` (SD), they list, play back, export or take a snapshot from. Defaults favor cheap SD playback and sharp HD stills. Playback must not break when one variant has a gap.

## Requirements

### Requirement: Variant selector on listing and playback endpoints

These endpoints SHALL accept an optional `variant` parameter:

- `GET /api/{camera}/recordings` and `GET /api/{camera}/recordings/summary`
- the HLS VOD endpoints: `/vod/{camera}/start/{start}/end/{end}`, `/vod/{year_month}/{day}/{hour}/{camera}[/{tz}]`, `/vod/event/{event_id}` and `/vod/clip/{camera}/start/{start}/end/{end}`
- `GET /api/{camera}/start/{start}/end/{end}/clip.mp4`

#### Scenario: Explicit HD playback
- **WHEN** a client requests `/vod/{camera}/start/{s}/end/{e}/master.m3u8?variant=main` for a window where `main` has footage
- **THEN** the playlist is built only from `main` segments

#### Scenario: Request without a variant
- **WHEN** a client calls any of these endpoints without `variant`
- **THEN** the request succeeds with the default variant for that endpoint

### Requirement: Variant selector on snapshot and export endpoints

`GET /api/{camera}/recordings/{frame_time}/snapshot.{ext}`, `POST /api/{camera}/plus/{frame_time}` and export creation SHALL accept an optional `variant` parameter.

#### Scenario: SD snapshot on request
- **WHEN** a client requests a snapshot from a recording with `variant=sub`, and `sub` covers that time
- **THEN** the frame is taken from the `sub` segment

#### Scenario: HD export
- **WHEN** a client creates an export with `variant=main`
- **THEN** the export file is composed from `main` segments

### Requirement: Default variant per endpoint

Listing, summary, VOD, clip and export endpoints SHALL default to `sub`. The snapshot-from-recording and Frigate+ submission endpoints SHALL default to `main`. This is a **BREAKING** default for external clients: on dual-stream cameras, a client that sends no `variant` now gets SD playback and SD exports.

#### Scenario: Default playback is SD
- **WHEN** a client requests a VOD playlist for a dual-stream camera without `variant`
- **THEN** the playlist is built from `sub` segments, if `sub` has footage in the window

#### Scenario: Default snapshot is HD
- **WHEN** a client requests a snapshot from a recording without `variant`
- **THEN** the frame is taken from a `main` segment, if `main` covers that time

### Requirement: Allowed variant values

Listing and summary endpoints SHALL accept `main`, `sub` or `all`, where `all` means no variant filter. Playback, clip, snapshot, Frigate+ and export endpoints SHALL accept only `main` or `sub`. Any other value SHALL be rejected with HTTP 422.

#### Scenario: All variants in a listing
- **WHEN** a client lists recordings with `variant=all`
- **THEN** rows of every variant are returned, each with its own `variant` field

#### Scenario: Invalid value
- **WHEN** a client sends `variant=hd` or `variant=all` to a VOD endpoint
- **THEN** the server responds 422

### Requirement: Fallback to the other variant

When the requested variant (`main` or `sub`) has no recording overlapping the requested window or instant, the server SHALL serve the other variant instead of returning an empty result. If neither variant has footage, the endpoint SHALL behave as upstream does for missing footage. `variant=all` SHALL never fall back. Export SHALL decide the variant once, when the export is created, and use it for the whole export.

#### Scenario: Footage from before the dual-stream upgrade
- **WHEN** a client requests `sub` playback for a window recorded before the camera had a `sub` input
- **THEN** the playlist is built from `main` segments

#### Scenario: No footage at all
- **WHEN** neither variant has footage in the requested window
- **THEN** the response is the same as upstream gives for a window without recordings

### Requirement: Path-form variant on VOD URLs

Every VOD endpoint SHALL also accept the variant as a trailing path segment, `/vod/.../{variant}/master.m3u8` or `/vod/.../{variant}/index.m3u8`. This form SHALL be treated exactly like `?variant=`. It is the canonical form, used by the web UI and by export composition, because it survives the nginx VOD module and its mapping cache.

#### Scenario: Path form
- **WHEN** a client requests `/vod/{camera}/start/{s}/end/{e}/main/index.m3u8`
- **THEN** the result is the same as `/vod/{camera}/start/{s}/end/{e}/index.m3u8?variant=main`

### Requirement: Variant-independent endpoints are unchanged

These endpoints SHALL NOT filter by variant: camera-wide endpoints such as `GET /api/recordings/summary` (no camera), `GET /api/recordings/unavailable` and `GET /api/recordings/storage`; event thumbnails and event snapshots; preview videos and frames; live and latest-frame endpoints. Each SHALL behave as upstream does and consider all variants.

#### Scenario: Day availability across variants
- **WHEN** a camera has only `sub` footage on a given day
- **THEN** `GET /api/recordings/summary` reports that day as having recordings
