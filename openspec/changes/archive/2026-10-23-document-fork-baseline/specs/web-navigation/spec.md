# Spec Delta

## Purpose

Send web UI users to the right page: admins land in the config editor, other users land on live view, a login returns users to the page they first asked for, and a restart from the config editor brings the admin back to it.

## ADDED Requirements

### Requirement: Role-based landing page

Opening the web UI root `/` SHALL redirect admins to `/config` and other signed-in users to `/live`. When authentication is disabled, the user SHALL be treated as an admin. Live view SHALL be served at `/live`, and the sidebar logo and the Live menu entry SHALL link there.

#### Scenario: Admin opens the root
- **WHEN** a signed-in admin opens `/`
- **THEN** the browser is redirected to `/config`

#### Scenario: Viewer opens the root
- **WHEN** a signed-in user with the viewer role opens `/`
- **THEN** the browser is redirected to `/live`

### Requirement: Return to the requested page after login

When an unauthenticated user opens a protected page, the UI SHALL redirect to `/login?redirect=<path and query>`. After a successful login, the UI SHALL navigate to that path, but only when it is a same-origin relative path: it starts with `/`, does not start with `//`, and contains no `://`. In every other case the UI SHALL go to the app root.

#### Scenario: Deep link survives login
- **WHEN** a signed-out user opens `/review?camera=front` and then logs in
- **THEN** the browser ends up at `/review?camera=front`

#### Scenario: Open-redirect attempt
- **WHEN** a user logs in from `/login?redirect=//evil.example`
- **THEN** the browser goes to the app root, not to the external site

### Requirement: Restart from the config editor returns to the config editor

After a restart started from the config editor, the UI SHALL reload the config editor, both on the automatic reload at the end of the countdown and on a forced reload. A restart started anywhere else SHALL reload the app root, as upstream does.

#### Scenario: Save and restart
- **WHEN** an admin saves the config and restarts from the config editor
- **THEN** once Frigate is back, the browser shows `/config`
