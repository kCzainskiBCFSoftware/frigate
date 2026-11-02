# Spec Delta

## MODIFIED Requirements

### Requirement: Correct before precomputation completes

Ranges MAY come from precomputed data, but answers SHALL be the same as merging the raw segments with the requested `gap`. Any part of a window that has not been precomputed yet SHALL be merged live. A freshly upgraded device SHALL therefore answer correctly from the first request, with nothing to trigger and nothing to wait for. "No footage" SHALL never be confused with "not precomputed yet".

#### Scenario: Freshly upgraded device
- **WHEN** a client requests last week's ranges a minute after the upgrade, before any history has been precomputed
- **THEN** the response matches what a fully precomputed device would return

#### Scenario: Late-arriving segment
- **WHEN** a segment is written for a window after part of that window was already precomputed
- **THEN** later responses for that window include the segment's footage

#### Scenario: Row written long after its segment
- **WHEN** a segment's row is written 30 minutes after the segment was recorded, because the database was locked or because recovery after an unclean shutdown added it
- **THEN** once the next rollup tick has run, ranges for that window include the segment's footage, the same as if the row had been written on time
