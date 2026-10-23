# playback-variant-preference Specification

## Purpose
Let a web UI user choose, per camera, whether recorded footage plays back in HD (`main`) or SD (`sub`). Recording playback, event clips and exports all follow that choice.

## Requirements

### Requirement: Per-camera stream selector in the player

The recording player SHALL show an Auto/Main/Sub selector in its top-right corner, hidden while the user is scrubbing. The choice SHALL be saved in the browser per user and per camera, and restored the next time that camera's recordings are opened. The default SHALL be Auto. All labels SHALL come from translation files.

#### Scenario: Choice is remembered
- **WHEN** a user picks Main for camera `front` and later reopens `front`'s recordings
- **THEN** the selector shows Main and playback uses `main`

#### Scenario: Cameras are independent
- **WHEN** a user picks Main for `front`
- **THEN** the selector for `back` keeps its own setting

### Requirement: Preference maps to the API variant

Main SHALL request variant `main`. Sub and Auto SHALL request variant `sub`, and the server falls back to `main` when SD footage is missing. Requests SHALL use the path form of the VOD URL, `/vod/.../{variant}/...`.

#### Scenario: Auto on a camera without SD history
- **WHEN** the preference is Auto and the selected window only has `main` footage
- **THEN** the player requests `sub`, and the server serves the `main` footage

### Requirement: Preference applies to all playback surfaces

The camera's preference SHALL be used for recording playback in the history view, for event clips in the tracking and search detail dialogs, and for exports created from the export dialog.

#### Scenario: Export in HD
- **WHEN** a user whose preference for `front` is Main exports a time range of `front`
- **THEN** the export is created with variant `main`
