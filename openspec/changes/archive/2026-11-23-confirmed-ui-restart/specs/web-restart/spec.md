# Spec Delta

## Purpose

Restart Frigate from the web UI in a way that cannot be silently lost: the UI
confirms that Frigate really stopped and came back before it reloads, and says
so when a restart did not happen.

## ADDED Requirements

### Requirement: Restart is requested over HTTP

The web UI SHALL request a restart with `POST /api/restart`, from the menu and from Save & Restart in the config editor, whatever the state of the WebSocket connection. The UI SHALL NOT send these restarts over the WebSocket.

#### Scenario: WebSocket is not connected
- **WHEN** the UI's WebSocket is disconnected and an admin confirms Restart
- **THEN** the UI sends `POST /api/restart` and Frigate restarts

#### Scenario: Save and restart from the config editor
- **WHEN** an admin saves the config in the config editor and confirms the restart
- **THEN** the UI sends `POST /api/restart` after the save has succeeded

### Requirement: A rejected restart request is shown as an error

When `POST /api/restart` answers 401 or 403, the UI SHALL say that the user is not allowed to restart Frigate. When it answers with any other 4xx status or with 500, the UI SHALL say that Frigate could not restart. In both cases the UI SHALL NOT wait for a restart and SHALL NOT reload the page.

#### Scenario: Not allowed
- **WHEN** the restart request is answered with 403
- **THEN** the UI says the user is not allowed to restart Frigate and does not reload

#### Scenario: Server error
- **WHEN** the restart request is answered with 500
- **THEN** the UI says Frigate could not restart and does not reload

### Requirement: An unclear reply counts as a possible restart

When `POST /api/restart` fails with a network error, gets no reply within 10 s, or is answered with 502, 503 or 504, the UI SHALL treat the restart as possibly started and SHALL go on to confirm it.

#### Scenario: Connection dropped by the restart
- **WHEN** the connection carrying the restart request is closed before a reply arrives
- **THEN** the UI does not show an error and starts confirming the restart

### Requirement: The page reloads only after a confirmed restart

After the request, the UI SHALL probe `GET /api/version` about once a second. A probe fails on a network error, a reply other than 200, or no reply within 5 s. The UI SHALL reload the page only after two failed probes in a row followed by a successful one. While it waits, it SHALL show what it is waiting for and the elapsed time, not a countdown, and SHALL offer a button that reloads at once.

#### Scenario: Normal restart
- **WHEN** the restart is accepted, Frigate stops answering, and later answers `GET /api/version` with 200
- **THEN** the UI reloads the page right after that answer

#### Scenario: Slow shutdown
- **WHEN** Frigate stops answering at once but takes 3 minutes to stop and start again
- **THEN** the UI keeps waiting and reloads once Frigate answers, with no reload before that

#### Scenario: A single failed probe
- **WHEN** one probe fails while Frigate keeps running and the next probe succeeds
- **THEN** the UI does not count Frigate as stopped

#### Scenario: Reload at once
- **WHEN** the user presses the reload button while the UI is waiting
- **THEN** the page reloads at once

### Requirement: A restart that did not happen is reported

If Frigate has not been seen to stop within 60 s of the restart request, the UI SHALL stop waiting, say that Frigate did not restart, and offer Retry. Retry SHALL send a new `POST /api/restart` and start the confirmation again.

#### Scenario: Frigate keeps running
- **WHEN** every probe succeeds for 60 s after the restart request
- **THEN** the UI says Frigate did not restart, offers Retry, and does not reload

#### Scenario: Retry
- **WHEN** the user presses Retry after a restart that did not happen
- **THEN** the UI sends a new `POST /api/restart` and confirms that restart from the start

### Requirement: A slow start is reported while the UI keeps waiting

If Frigate has been seen to stop but has not answered again within 6 minutes of stopping, the UI SHALL say that Frigate is taking longer than expected to start, SHALL keep probing, and SHALL reload the page as soon as Frigate answers.

#### Scenario: Frigate takes 8 minutes to come back
- **WHEN** Frigate stops answering and answers again 8 minutes later
- **THEN** after 6 minutes the UI says the start is taking longer than expected, and it reloads once Frigate answers

### Requirement: The restart endpoint replies before it restarts

`POST /api/restart` SHALL send its reply before Frigate starts to shut down. When the restart is accepted, it SHALL reply 200 with the same body as before and start the shutdown about 1 s later. It SHALL reply 500 only when the restart cannot be scheduled. It stays admin-only.

#### Scenario: Admin restarts through the API
- **WHEN** an admin sends `POST /api/restart`
- **THEN** the client receives 200, and Frigate starts to shut down about 1 s later

#### Scenario: Viewer tries to restart
- **WHEN** a user with the viewer role sends `POST /api/restart`
- **THEN** the reply is 403 and Frigate keeps running
