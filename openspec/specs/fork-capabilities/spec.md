# fork-capabilities Specification

## Purpose
Let clients on a mixed-version fleet find out which fork-only features a device supports, without probing endpoints for 404s.

## Requirements

### Requirement: Fork block on the config endpoint

`GET /api/config` SHALL include a top-level `fork` object with `version`, the build version string, and `features`, a sorted list of feature flags. No existing config key SHALL be changed by adding it. The current flags are:

- `recording_variants`: the `variant` selector on recording endpoints is supported.
- `recordings_ranges`: `GET /api/recordings/ranges` and `GET /api/{camera}/recordings/ranges` exist.
- `recordings_hours`: `GET /api/recordings/hours` exists.

#### Scenario: Client detects the timeline API
- **WHEN** a client reads `GET /api/config` and finds `recordings_ranges` in `fork.features`
- **THEN** it can call the ranges endpoints instead of merging segment listings itself

#### Scenario: Older build
- **WHEN** a client reads `GET /api/config` from a build without the `fork` key
- **THEN** the client can tell that none of these fork features are present

### Requirement: Feature flags are append-only

A feature flag that has shipped SHALL NOT be renamed or removed. A new client-facing fork capability SHALL add a new flag. Flags SHALL be generic feature names, with no vendor, customer or product name.

#### Scenario: Adding a feature
- **WHEN** a new fork-only endpoint is released
- **THEN** a new flag is appended to `fork.features`, and all earlier flags are still present
