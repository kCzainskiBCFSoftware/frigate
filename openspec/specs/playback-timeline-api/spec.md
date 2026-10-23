# playback-timeline-api Specification

## Purpose
Answer "which parts of this window have footage" directly, as merged time ranges or hourly flags. A multi-camera timeline then needs one small request instead of downloading every ~10-second segment row for every camera.

## Requirements

### Requirement: Merged ranges for one camera

`GET /api/{camera}/recordings/ranges` SHALL return a JSON array of `{start, end}` objects, in unix seconds, ordered by `start`. Ranges within the response SHALL NOT overlap. A window without footage SHALL return `200` with `[]`. The caller SHALL need access to the camera.

#### Scenario: Jitter is merged, outages stay visible
- **WHEN** segments cover 0–10, 10.4–20.4 and 20.4–30.4, then nothing until 300, and the client asks with `gap=3`
- **THEN** the response contains a range 0–30.4 and a separate range starting at 300

#### Scenario: No footage
- **WHEN** the camera has no footage in the requested window
- **THEN** the response is `200` with `[]`

### Requirement: Range request parameters

The range endpoints SHALL accept these parameters:

- `after`: window start, in unix seconds. Default: one hour ago.
- `before`: window end. Default: now.
- `variant`: `main`, `sub` or `all`. Default: `sub`.
- `gap`: in seconds. Default: `3.0`.

#### Scenario: Defaults
- **WHEN** a client calls a range endpoint with no parameters
- **THEN** the response covers the last hour of `sub` footage, merged with a 3-second gap

### Requirement: Served variant is reported

The single-camera range endpoint SHALL return the variant actually served, after fallback, in the `X-Recording-Variant` response header.

#### Scenario: Served variant is reported
- **WHEN** a client asks for `variant=sub` on a window that only has `main` footage
- **THEN** the ranges describe the `main` footage and `X-Recording-Variant` is `main`

### Requirement: Merged ranges for many cameras in one request

`GET /api/recordings/ranges` SHALL take the same parameters as the single-camera form plus `cameras`, which is a comma-separated list or `all` (the default). It SHALL return `{"ranges": {camera: [...]}, "variants": {camera: variant}}`. The cameras SHALL be the requested ones that the caller may access. Every such camera SHALL appear in `ranges`, with an empty list when it has no footage. Variant fallback SHALL be decided per camera.

#### Scenario: Mixed fallback across cameras
- **WHEN** a day view asks for `variant=sub` and one camera has only `main` footage in the window
- **THEN** `variants` reports `main` for that camera and `sub` for the cameras that have SD footage

#### Scenario: Camera the caller may not see
- **WHEN** a restricted user requests `cameras=front,back` and may only access `front`
- **THEN** the response contains only `front`, and no error is returned for `back`

### Requirement: Hourly availability

`GET /api/recordings/hours` SHALL take `cameras`, `after`, `before`, `variant` and `timezone`. `timezone` is an IANA name; the default is `utc`. The endpoint SHALL return `{camera: {"YYYY-MM-DD": [24 values]}}`, where index `i` is set when footage starts in local hour `i` of that local day. Daylight-saving transitions SHALL be handled. Days without footage SHALL be omitted.

#### Scenario: Calendar paint
- **WHEN** a client asks for a month with `timezone=Europe/Warsaw`
- **THEN** each camera gets one 24-value array for each local day that has footage, and no entry for days without footage

### Requirement: Range semantics

Ranges SHALL be merged on the running maximum end time, so overlapping segments never shorten a range. A range that crosses the edge of the requested window SHALL be returned whole, not clipped to the window. `gap` SHALL be honored as the caller's threshold: holes no longer than `gap` are merged, and longer holes stay visible.

#### Scenario: Overlapping segments
- **WHEN** a long segment is followed by a shorter one that starts inside it and ends earlier
- **THEN** the merged range ends at the long segment's end, not the short one's

#### Scenario: Range crossing midnight
- **WHEN** a client asks for a window that starts at midnight and continuous footage began at 23:59:52 the previous day
- **THEN** the first range starts at 23:59:52 the previous day

### Requirement: Window limit and validation

The range and hours endpoints SHALL reject a window longer than 8 days with HTTP 422 and a JSON body `{"success": false, "message": ...}`. An unknown `variant` value SHALL also be rejected with HTTP 422.

#### Scenario: Month of ranges
- **WHEN** a client requests ranges for a 30-day window
- **THEN** the server responds 422, and the client should request a day at a time or use the hours endpoint

### Requirement: Cache headers on timeline responses

Timeline responses SHALL set `Cache-Control: public, max-age=60, stale-while-revalidate=300` when the window ended more than 10 minutes ago, and `Cache-Control: public, max-age=5` otherwise. They SHALL NOT be marked immutable, because retention can delete footage from a past window.

#### Scenario: Yesterday versus today
- **WHEN** one client requests yesterday's ranges and another requests today's
- **THEN** yesterday's response may be cached for 60 seconds and today's for 5 seconds

### Requirement: Correct before precomputation completes

Ranges MAY come from precomputed data, but answers SHALL be the same as merging the raw segments with the requested `gap`. Any part of a window that has not been precomputed yet SHALL be merged live. A freshly upgraded device SHALL therefore answer correctly from the first request, with nothing to trigger and nothing to wait for. "No footage" SHALL never be confused with "not precomputed yet".

#### Scenario: Freshly upgraded device
- **WHEN** a client requests last week's ranges a minute after the upgrade, before any history has been precomputed
- **THEN** the response matches what a fully precomputed device would return

#### Scenario: Late-arriving segment
- **WHEN** a segment is written for a window after part of that window was already precomputed
- **THEN** later responses for that window include the segment's footage
