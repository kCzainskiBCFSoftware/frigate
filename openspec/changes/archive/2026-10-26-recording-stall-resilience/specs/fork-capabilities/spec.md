# Spec Delta

## MODIFIED Requirements

### Requirement: Fork block on the config endpoint

`GET /api/config` SHALL include a top-level `fork` object with `version`, the build version string, and `features`, a sorted list of feature flags. No existing config key SHALL be changed by adding it. The flags and their meaning are listed under "Feature flags and their meaning".

#### Scenario: Client detects the timeline API
- **WHEN** a client reads `GET /api/config` and finds `recordings_ranges` in `fork.features`
- **THEN** it can call the ranges endpoints instead of merging segment listings itself

#### Scenario: Older build
- **WHEN** a client reads `GET /api/config` from a build without the `fork` key
- **THEN** the client can tell that none of these fork features are present

## ADDED Requirements

### Requirement: Feature flags and their meaning

A flag in `fork.features` SHALL mean that the build supports the behavior it names:

- `recording_variants`: the `variant` selector on recording endpoints is supported.
- `recordings_ranges`: `GET /api/recordings/ranges` and `GET /api/{camera}/recordings/ranges` exist.
- `recordings_hours`: `GET /api/recordings/hours` exists.
- `recordings_unknown_stats`: `motion` and `objects` in recording listings and summaries can be `null`, meaning unknown, which is not the same as zero.

#### Scenario: Client learns that statistics can be unknown
- **WHEN** a client finds `recordings_unknown_stats` in `fork.features`
- **THEN** it treats a `null` `motion` or `objects` value as unknown, not as "no activity"
