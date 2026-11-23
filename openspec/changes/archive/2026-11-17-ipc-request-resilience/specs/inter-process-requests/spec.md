# Spec Delta

## Purpose

Keep the main process answering requests from Frigate's other processes when one request fails or hangs. Every process that writes to the database or asks the main process for state, including the recording process, depends on these answers. Upstream Frigate stops answering all of them after a single handler error, with nothing more in the log.

## ADDED Requirements

### Requirement: Every request is answered

The main process SHALL answer every inter-process request. A request whose handler raises an exception, or whose result cannot be sent, SHALL get an empty result. Handling requests SHALL stop only when Frigate stops.

#### Scenario: Handler raises
- **WHEN** a generated description arrives for a tracked object whose event was deleted, and its handler raises
- **THEN** the sending process gets an empty result
- **AND** a camera activity update sent afterwards by another process is answered and applied

#### Scenario: Result cannot be sent
- **WHEN** a handler returns a value that cannot be encoded as JSON
- **THEN** the sending process gets an empty result, and the next request is answered

### Requirement: A failed region-grid refresh keeps the current grid

When a camera's daily region-grid request gets an empty result or no reply, the camera SHALL keep detecting with the region grid it already has, and SHALL request a new one at the next scheduled refresh.

#### Scenario: Region-grid handler fails
- **WHEN** the main process answers a camera's region-grid request with an empty result
- **THEN** the camera keeps detecting objects with its previous region grid

### Requirement: Failed requests are logged by topic

Each request that fails as described in "Every request is answered" SHALL be logged as an error with its topic. For each topic, at most one traceback SHALL be logged every 5 minutes. When the next traceback for that topic is logged, it SHALL include the number of failures since the last one.

#### Scenario: Repeated failures of one topic
- **WHEN** the same topic fails 50 times within 5 minutes
- **THEN** one error with a traceback is logged for that topic in that period
- **AND** the next logged error for that topic reports how many failures were not logged

### Requirement: Timed requests recover from a missing reply

A process sending a request with a timeout SHALL get the "no reply" result when no reply arrives in time, or when the request fails on its connection. Its next request SHALL be sent on a new connection to the main process and answered normally. Each sender SHALL log such a failure as a warning with its topic at most once every 5 minutes, and the next warning SHALL include the number of failures since the last one. A request sent without a timeout SHALL wait for its reply, as upstream does.

#### Scenario: Main process busy past the timeout
- **WHEN** a request with a 30 s timeout gets no reply for 30 s
- **THEN** the sender gets the "no reply" result
- **AND** once the main process answers again, the sender's next request on the same requester is answered

#### Scenario: Repeated timeouts
- **WHEN** a sender's requests get no reply 10 times within 5 minutes
- **THEN** one warning with the topic is logged for that sender in that period

### Requirement: A stopped request thread restarts Frigate

The main process SHALL check the thread that answers inter-process requests every 10 seconds. If that thread has stopped while Frigate is running, Frigate SHALL log an error that names the inter-process request thread, then restart.

#### Scenario: Request thread ends
- **WHEN** the inter-process request thread has ended while Frigate is running
- **THEN** within 20 seconds an error naming that thread is logged and Frigate restarts

### Requirement: A restart completes while the request thread is dead or stuck

While Frigate restarts or stops, until it has stopped the inter-process request thread itself, it SHALL watch whether that thread has stopped or one request has run longer than the busy timeout. If either still holds 60 seconds after it was first seen, Frigate SHALL exit at once, so that its supervisor or the container's restart policy can start it again. This SHALL hold for every restart, including one from the UI.

#### Scenario: Shutdown waits on the dead request thread
- **WHEN** the request thread has ended while another main-process thread waits for its reply, and Frigate restarts
- **THEN** Frigate exits within 60 seconds of the restart, with an error saying it did not stop in time
- **AND** with a container restart policy, Frigate is back and recording within 3 minutes of the thread ending

#### Scenario: UI restart while a request is stuck
- **WHEN** one request has run for 75 s on a device with 2 enabled cameras (busy timeout 60 s) while another main-process thread waits for a reply, and the user restarts Frigate from the UI
- **THEN** Frigate exits within 60 seconds of starting to stop, with an error saying it did not stop in time

#### Scenario: Request gets stuck during the stop
- **WHEN** one request has run for 20 s on a device with 2 enabled cameras (busy timeout 60 s) and never finishes, another main-process thread waits for a reply, and the user restarts Frigate from the UI
- **THEN** Frigate exits within 60 seconds of that request passing the busy timeout, with an error saying it did not stop in time

### Requirement: A healthy restart is not cut short

A restart or stop during which the request thread neither stops nor runs one request past the busy timeout SHALL NOT be cut short. Neither SHALL one whose request thread answers again before the 60 seconds end.

#### Scenario: Slow request finishes during the stop
- **WHEN** one request has run for 70 s on the same device when the user restarts Frigate, and it finishes 5 s later
- **THEN** Frigate stops as it does upstream, and nothing ends it early

#### Scenario: Healthy restart
- **WHEN** the request thread is idle, or its current request finishes within the busy timeout, and the user restarts Frigate
- **THEN** Frigate stops as it does upstream, and nothing ends it early

### Requirement: A stuck request restarts Frigate

If a single request has been handled for longer than `max(600, 2 × busy timeout)` seconds, Frigate SHALL log an error with the request's topic and how long it has run, then restart. The busy timeout is the main process's database busy timeout: `max(60, 10 × cameras enabled in the config)` seconds. A request handled within that time SHALL NOT cause a restart.

#### Scenario: Insert blocked on a 32-camera device
- **WHEN** a segment-row insert on a device with 32 enabled cameras (busy timeout 320 s) has been running for 640 s
- **THEN** an error naming the `insert_many_recordings` topic and its duration is logged, and Frigate restarts

#### Scenario: Long but finite lock
- **WHEN** a request on the same device takes 300 s because the database is locked, then completes
- **THEN** Frigate does not restart
