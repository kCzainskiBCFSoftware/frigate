# recorder-watchdog Specification

## Purpose
Restart a camera's ffmpeg recorder only when that recorder is broken. Upstream Frigate judges recorders by messages from the segment mover, so a stalled mover gets every healthy recorder restarted. It also judges a dual-stream camera's recorders together, so one hung recorder can be masked by the other.

## Requirements

### Requirement: Recorder health comes from its own output

With `record.pipeline.recorder_watchdog: cache`, which is the default, the watchdog SHALL judge each recorder by the newest segment that recorder has written to the cache, using that segment's start time and its last modification time. There is one recorder per camera and record variant. The value `mover` SHALL restore the upstream signals.

#### Scenario: Mover paused
- **WHEN** the recording manager process is paused for 3 minutes while every recorder keeps writing segments
- **THEN** no recorder is restarted

### Requirement: Only the stale recorder is restarted

Outside the startup grace period, the watchdog SHALL restart a recorder when neither the start time nor the modification time of its newest cached segment is within the last `record.pipeline.recorder_stale_timeout` seconds. It SHALL restart only that recorder, not the camera's other recorders.

#### Scenario: Recorder stops writing
- **WHEN** a recorder's ffmpeg process stays running but writes nothing for longer than `recorder_stale_timeout`
- **THEN** that recorder is restarted, and the error message states the configured timeout

#### Scenario: Hung sub recorder on a dual-stream camera
- **WHEN** a camera's `sub` recorder stops writing while its `main` recorder continues
- **THEN** the `sub` recorder is restarted and the `main` recorder is not

### Requirement: Segment validity checks apply only while the mover keeps up

The watchdog SHALL restart a recorder for producing no valid segments, or only invalid ones, only while the segment mover is reporting progress. It SHALL measure that staleness from no earlier than the moment the mover last resumed reporting. An invalid segment SHALL count only against the recorder that wrote it.

#### Scenario: Recorder writes only invalid segments
- **WHEN** a recorder writes only segments that the mover rejects as invalid, for longer than `recorder_stale_timeout`, while the mover is running
- **THEN** that recorder is restarted

#### Scenario: Invalid segments on one variant
- **WHEN** a camera's `main` segments are invalid and its `sub` segments are valid
- **THEN** only the `main` recorder is restarted

#### Scenario: Mover clears a backlog
- **WHEN** the mover resumes after a 3-minute stall and spends the next passes clearing the backlog
- **THEN** no recorder is restarted for stale or invalid segments

### Requirement: A stalled mover is reported

When the segment mover has reported no progress for longer than `recorder_stale_timeout`, the watchdog SHALL log a warning that names the recording mover, not a recorder, as stalled. Each camera's watchdog SHALL log it at most once per 5 minutes. The watchdog SHALL NOT restart recorders because the mover is stalled.

#### Scenario: Mover blocked
- **WHEN** the mover makes no progress for 4 minutes while the recorders keep writing
- **THEN** each camera's watchdog logs one warning naming the recording mover, and no recorder is restarted

### Requirement: The stale timeout is configurable

`record.pipeline.recorder_stale_timeout` SHALL default to 120 seconds. It SHALL be settable globally and per camera. Restart messages SHALL state the configured value instead of a fixed "120s". The startup grace period after a recorder starts or is enabled SHALL be the smaller of 90 seconds and the timeout.

#### Scenario: Longer timeout on one camera
- **WHEN** one camera sets `record.pipeline.recorder_stale_timeout: 300`
- **THEN** that camera's recorders are restarted only after 300 seconds without output, and the other cameras keep 120 seconds
