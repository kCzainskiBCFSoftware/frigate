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

A process sending a request with a timeout SHALL get the "no reply" result when no reply arrives in time. Its next request SHALL be sent on a new connection to the main process and answered normally. A request sent without a timeout SHALL wait for its reply, as upstream does.

#### Scenario: Main process busy past the timeout
- **WHEN** a request with a 30 s timeout gets no reply for 30 s
- **THEN** the sender gets the "no reply" result
- **AND** once the main process answers again, the sender's next request on the same requester is answered

### Requirement: A stopped request thread restarts Frigate

The main process SHALL check the thread that answers inter-process requests every 10 seconds. If that thread has stopped while Frigate is running, Frigate SHALL log an error that names the inter-process request thread, then restart.

#### Scenario: Request thread ends
- **WHEN** the inter-process request thread has ended while Frigate is running
- **THEN** within 20 seconds an error naming that thread is logged and Frigate restarts

### Requirement: A stuck request restarts Frigate

If a single request has been handled for longer than `max(600, 2 × busy timeout)` seconds, Frigate SHALL log an error with the request's topic and how long it has run, then restart. The busy timeout is the main process's database busy timeout: `max(60, 10 × cameras enabled in the config)` seconds. A request handled within that time SHALL NOT cause a restart.

#### Scenario: Insert blocked on a 32-camera device
- **WHEN** a segment-row insert on a device with 32 enabled cameras (busy timeout 320 s) has been running for 640 s
- **THEN** an error naming the `insert_many_recordings` topic and its duration is logged, and Frigate restarts

#### Scenario: Long but finite lock
- **WHEN** a request on the same device takes 300 s because the database is locked, then completes
- **THEN** Frigate does not restart
