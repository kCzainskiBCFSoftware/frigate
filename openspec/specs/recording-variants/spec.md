# recording-variants Specification

## Purpose
Record more than one stream per camera as separate recording variants, `main` (HD) and `sub` (SD). One camera can then keep cheap long-term SD footage beside short-term HD footage. Upstream Frigate records exactly one stream per camera.

## Requirements

### Requirement: Record inputs carry a variant label

Each camera input with the `record` role SHALL have a `record_variant` of `main` or `sub`. The default is `main`. A camera with a single record input SHALL behave as upstream Frigate does, recording it as `main`.

#### Scenario: Single-stream camera keeps working unchanged
- **WHEN** a camera has one input with the `record` role and no `record_variant` set
- **THEN** the config is valid and that input records as variant `main`

#### Scenario: Dual-stream camera
- **WHEN** a camera has two record inputs, one with `record_variant: main` and one with `record_variant: sub`
- **THEN** the config is valid and both streams record at the same time as separate variants of the same camera

### Requirement: Variant configuration is validated

The config SHALL be rejected when a camera uses the same `record_variant` on more than one record input. It SHALL also be rejected when a camera has record inputs but none of them is `main`, because `main` holds legacy footage and is the fallback target. A role other than `record` SHALL still be allowed on only one input per camera.

#### Scenario: Duplicate variant rejected
- **WHEN** two record inputs of one camera both use `record_variant: sub`
- **THEN** config validation fails with an error saying each `record_variant` may be used only once per camera

#### Scenario: Missing main variant rejected
- **WHEN** a camera's only record input uses `record_variant: sub`
- **THEN** config validation fails with an error saying one record input must use `main`

#### Scenario: Unknown variant label rejected
- **WHEN** a record input sets `record_variant` to a value other than `main` or `sub`
- **THEN** config validation fails

### Requirement: Variants are stored separately on disk

Each variant's segments SHALL be written under their own directory, `recordings/{date}/{hour}/{camera}/{variant}/{MM.SS}.mp4`. Recordings made before the upgrade, stored as `recordings/{date}/{hour}/{camera}/{MM.SS}.mp4`, SHALL stay readable and count as variant `main`.

#### Scenario: New segments go to variant directories
- **WHEN** a dual-stream camera records a segment that starts at 14:22:34 on 2026-06-05
- **THEN** the HD segment is written to `recordings/2026-06-05/14/{camera}/main/22.34.mp4` and the SD segment to `recordings/2026-06-05/14/{camera}/sub/22.34.mp4`

#### Scenario: Legacy layout is still served
- **WHEN** a recording exists only at the legacy path, with no variant directory
- **THEN** it is listed, played back and expired as variant `main`

### Requirement: Recordings expose variant and stream metadata

Every recording segment SHALL record its variant. It SHALL also record the stream's codec name, width, height and bitrate when they are known. Segments recorded before the upgrade SHALL count as variant `main`, with null codec, size and bitrate.

#### Scenario: Recording listing includes metadata
- **WHEN** a client lists a camera's recordings
- **THEN** each row includes `variant`, `codec_name`, `width`, `height` and `bitrate` beside the upstream fields

#### Scenario: Pre-upgrade rows
- **WHEN** the database is migrated from an upstream build
- **THEN** every existing recording row gets variant `main`, and its codec, size and bitrate stay null

### Requirement: Camera-level figures account for variants

A camera's reported storage write rate SHALL be the sum of the write rates of all its variants. Motion activity on the review timeline SHALL be computed from the `main` variant only, so that motion is not counted twice on dual-stream cameras.

#### Scenario: Storage rate of a dual-stream camera
- **WHEN** a camera writes 400 MB/h on `main` and 60 MB/h on `sub`
- **THEN** the storage statistics report about 460 MB/h for that camera

#### Scenario: Motion activity is not doubled
- **WHEN** motion is recorded on both variants of a camera for the same period
- **THEN** the review motion-activity data counts that motion once
