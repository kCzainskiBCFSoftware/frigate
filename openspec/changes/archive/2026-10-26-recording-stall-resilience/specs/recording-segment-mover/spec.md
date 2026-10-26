# Spec Delta

## Purpose

Move recorded segments from the cache into recordings, without slow frame processing or a temporary backlog costing footage. Upstream Frigate holds continuous-retention segments until frame processing catches up. It can discard a segment for "no activity" before that segment's frames are processed. It discards backlog beyond a fixed count before trying to move it.

## ADDED Requirements

### Requirement: Continuous segments do not wait indefinitely for frame processing

For a camera with continuous retention (`record.continuous.days` above 0), the mover SHALL move a closed segment into recordings once frame processing has passed the segment's end, or once `record.pipeline.processing_timeout` seconds (default 30) have passed since that end, whichever is first. A timeout of 0 SHALL restore the upstream wait without a limit. Cameras without continuous retention SHALL keep waiting for frame processing, as upstream does.

#### Scenario: Frame processing paused
- **WHEN** a dual-stream camera with continuous retention has its frame-processing process paused for 3 minutes
- **THEN** each of its `main` and `sub` segments is in recordings within `processing_timeout` plus one mover pass after the segment closed
- **AND** no "Unable to keep up" or "Too many unprocessed" warning is logged for that camera

#### Scenario: Timeout disabled
- **WHEN** `processing_timeout` is 0 and a camera's frame processing is paused
- **THEN** that camera's segments wait in the cache for frame processing, as upstream does

#### Scenario: Camera without continuous retention
- **WHEN** a camera has `record.continuous.days: 0` and `record.motion.days: 7`, and its frame processing is paused
- **THEN** its segments are not moved on the timeout

### Requirement: Segments moved on the timeout have unknown statistics

A segment moved on the processing timeout SHALL be stored with `motion`, `objects`, `regions` and `dBFS` set to null. A segment moved after its frames were processed SHALL be stored with its computed statistics, as upstream does.

#### Scenario: Rows for a paused window
- **WHEN** a camera's segments are moved on the timeout while its frame processing is paused
- **THEN** the rows for that window have null `motion`, `objects`, `regions` and `dBFS`

#### Scenario: Processing keeps up
- **WHEN** a camera's frame processing is less than 2 seconds behind
- **THEN** its segments are moved with their computed statistics, and none of them is null

### Requirement: Lagging frame processing is reported

When the mover moves a camera's segments on the processing timeout, it SHALL log a warning that names the camera and says how many seconds its frame processing is behind. It SHALL log this at most once per camera per 5 minutes.

#### Scenario: Sustained lag
- **WHEN** a camera's segments are moved on the timeout for 12 minutes in a row
- **THEN** at most 3 such warnings are logged for that camera

### Requirement: Activity-based discard uses complete statistics

The mover SHALL NOT discard a segment for lack of motion, objects or audio while the camera's frame processing has not yet passed the segment's end. Such a segment SHALL stay in the cache until one of these happens: its statistics are complete, the processing timeout applies, or the cache backlog limit applies.

#### Scenario: Alert in progress during a processing stall
- **WHEN** an alert is in progress on a camera with continuous retention, and its frame processing has not yet reached the end of a closed segment
- **THEN** that segment is not deleted, and it is moved once its statistics are complete or the processing timeout has passed

#### Scenario: Camera with motion retention only
- **WHEN** a segment of a camera without continuous retention overlaps a detection in progress, and the segment's frames are not processed yet
- **THEN** the segment stays in the cache, and the keep-or-discard decision is made once its frames are processed

### Requirement: Unknown statistics are kept, not expired early

Retention SHALL treat a segment with null activity statistics as possibly having activity. The continuous-only "no motion" rule SHALL NOT expire such a segment, and neither SHALL the `motion` or `active_objects` retain modes of alerts and detections. The segment SHALL expire at the outer retention window of its camera and variant. A variant's `retain_days` hard cap SHALL still apply to it.

#### Scenario: Kept until the outer window
- **WHEN** a camera has `continuous.days: 1` and `motion.days: 7`, and a segment with null statistics is 3 days old
- **THEN** cleanup keeps the segment, and it deletes it once it is more than 7 days old

#### Scenario: Hard cap still applies
- **WHEN** the `sub` variant has `retain_days: 30` and a `sub` segment with null statistics is 31 days old
- **THEN** cleanup deletes it

### Requirement: Unknown statistics are reported as null

Recording listings and per-hour recording summaries SHALL report unknown statistics as `null`, never as `0`. A per-hour total SHALL be `null` only when every segment in that hour has unknown statistics. Otherwise it SHALL be the total of the known values. The motion-activity endpoint SHALL leave out segments with unknown motion, as it leaves out segments without motion. This is **BREAKING** for external clients that treat `motion` or `objects` as always numeric.

#### Scenario: Listing a window moved on the timeout
- **WHEN** a client lists a camera's recordings for a window whose segments were moved on the processing timeout
- **THEN** those rows have `"motion": null` and `"objects": null`

#### Scenario: Hourly summary
- **WHEN** some segments in an hour have unknown statistics and others have known statistics
- **THEN** that hour's `motion` and `objects` are the totals of the known values

#### Scenario: Timeline still renders
- **WHEN** the web UI shows a day that contains segments with unknown statistics
- **THEN** the footage appears on the timeline and plays, and the motion graph shows no activity for that span

### Requirement: Mover work is bounded

The mover SHALL run at most `record.pipeline.move_concurrency` segment probes and copies at the same time, across all cameras. The default SHALL be 8. A value of 0 SHALL mean no limit, as upstream.

#### Scenario: Backlog after a stall
- **WHEN** 600 closed segments are waiting at the start of a pass, with `move_concurrency: 8`
- **THEN** at no moment do more than 8 ffmpeg or ffprobe processes started by the mover run
- **AND** all 600 segments are moved

### Requirement: The backlog limit applies per recorder, after the moves

The mover SHALL apply its cache backlog limit only to segments that are still in the cache after the pass has made its moves. The limit is `record.pipeline.max_cached_segments` closed segments per camera and variant, with a default of 6. A segment the pass moved SHALL never be discarded for exceeding the limit. A discard SHALL log the existing "Unable to keep up with recording segments in cache" or "Too many unprocessed recording segments in cache" warning.

#### Scenario: Stall the cache can absorb
- **WHEN** the mover resumes after a 3-minute stall with about 18 segments waiting per variant, and the cache filesystem has room for them
- **THEN** the first pass moves all of them, and no segment is discarded

#### Scenario: Mover cannot keep up
- **WHEN** a recorder still has more closed segments in the cache than `max_cached_segments` after a pass
- **THEN** its oldest segments beyond the limit are discarded with the warning

#### Scenario: One variant lags
- **WHEN** after a pass a camera has 9 closed `main` segments and 3 closed `sub` segments in the cache, with `max_cached_segments: 6`
- **THEN** only the 3 oldest `main` segments are discarded

### Requirement: The cache keeps a free-space reserve

At the start and at the end of each pass, the mover SHALL check free space on the cache filesystem. When it is below `record.pipeline.cache_free_reserve` percent, the mover SHALL discard the oldest closed segments across all cameras until the reserve is restored. It SHALL log an error saying how many segments it discarded from which cameras. The default SHALL be 10. A value of 0 SHALL disable the reserve.

#### Scenario: Cache nearly full
- **WHEN** the cache filesystem is 92% full, with `cache_free_reserve: 10`
- **THEN** the oldest closed segments are discarded until at least 10% is free, and an error lists the cameras affected

### Requirement: Pipeline settings are validated

The config SHALL be rejected when `processing_timeout + 10` is not less than `max_cached_segments × 10`, because segments would be discarded before the processing wait could end. It SHALL also be rejected when any `record.pipeline` count or duration is negative.

#### Scenario: Wait longer than the backlog limit
- **WHEN** a config sets `processing_timeout: 60` and `max_cached_segments: 6`
- **THEN** config validation fails, with an error saying the processing timeout must be shorter than the cache backlog window
