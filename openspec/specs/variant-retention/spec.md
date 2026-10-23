# variant-retention Specification

## Purpose
Let each recording variant of a camera keep footage for its own length of time, for example SD for 30 days and HD for 7. Footage of every variant must still be cleaned up eventually, and previews must not disappear while any variant still covers them.

## Requirements

### Requirement: Per-variant retention override

A record input MAY set `retain_days`, a number of days, zero or more. For that variant, `retain_days` SHALL replace the camera's continuous retention. The motion-based outer window for that variant SHALL reach at least `retain_days`, so the override is honored even when the camera's base motion retention is shorter. Variants without `retain_days` SHALL follow the camera's `record` retention settings, as upstream does.

#### Scenario: SD kept longer than HD
- **WHEN** a camera has `record.continuous.days: 7`, a `main` input without `retain_days`, and a `sub` input with `retain_days: 30`
- **THEN** cleanup keeps `sub` segments for 30 days and `main` segments for 7 days

#### Scenario: Override is not cut short by a shorter base motion window
- **WHEN** the camera's base motion retention is 7 days and the `sub` input has `retain_days: 30`
- **THEN** `sub` segments between 7 and 30 days old are not deleted

### Requirement: Retention override is a hard cap

When a variant sets `retain_days`, its segments older than `retain_days` SHALL be deleted. This SHALL happen even if they overlap motion, alerts or detections that the camera's other retention settings would keep.

#### Scenario: Base retention longer than the override
- **WHEN** the camera keeps motion footage for 60 days and the `sub` input has `retain_days: 30`
- **THEN** `sub` segments older than 30 days are deleted, whatever motion or events they contain

### Requirement: Footage of removed variants is cleaned up

Recordings whose variant is no longer configured for the camera SHALL still expire. They SHALL be deleted, from disk and from the database, once they are older than the camera's base outer retention window.

#### Scenario: Camera switched back to single-stream
- **WHEN** a camera that recorded `main` and `sub` is reconfigured to record only `main`
- **THEN** the existing `sub` footage is deleted once it falls outside the camera's base retention window, rather than staying forever

### Requirement: Previews follow the longest-retained variant

Previews belong to the camera, not to a variant. A preview SHALL be kept while any variant of the camera still keeps a recording that overlaps it.

#### Scenario: HD expired, SD still kept
- **WHEN** a camera's `main` footage for a period has expired but its `sub` footage for that period is still kept
- **THEN** the preview for that period is kept

### Requirement: Precomputed timeline follows retention

Precomputed footage ranges SHALL be trimmed after recordings expire, so the timeline never reports footage that retention has deleted. A range that crosses the retention cutoff SHALL be clipped to the cutoff, not dropped.

#### Scenario: Day partly expired
- **WHEN** retention deletes the first hours of the oldest kept day
- **THEN** after the next trim, timeline ranges for that day start no earlier than the first footage still kept
