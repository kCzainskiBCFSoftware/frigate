# Spec Delta

## MODIFIED Requirements

### Requirement: Restart from the config editor returns to the config editor

After a restart started from the config editor, the UI SHALL reload the config editor, both on the automatic reload once Frigate is back and on a forced reload. A restart started anywhere else SHALL reload the app root, as upstream does.

#### Scenario: Save and restart
- **WHEN** an admin saves the config and restarts from the config editor
- **THEN** once Frigate is back, the browser shows `/config`

#### Scenario: Forced reload from the config editor
- **WHEN** an admin restarts from the config editor and presses the reload button while the UI is still waiting
- **THEN** the browser loads `/config`

#### Scenario: Restart from the menu
- **WHEN** an admin restarts from the menu
- **THEN** once Frigate is back, the browser shows the app root
