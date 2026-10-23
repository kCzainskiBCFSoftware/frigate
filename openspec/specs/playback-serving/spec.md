# playback-serving Specification

## Purpose
Serve recording playback on NAS-class hardware (4 cores, limited RAM, ~32 recording cameras) with up to four concurrent VOD players and a timeline, without one slow request stalling the rest of the API.

## Requirements

### Requirement: Playback requests do not block the API

Blocking work done while serving recordings SHALL NOT run on the API's event loop. This covers recording listings and summaries, snapshots from recordings, VOD mappings and progressive clips. A slow playback request SHALL NOT delay unrelated API requests, such as live view, config or events.

#### Scenario: Four players and a timeline
- **WHEN** four VOD playlists and a timeline request arrive at the same time
- **THEN** they are processed concurrently, and a request to an unrelated endpoint is not queued behind them

### Requirement: Bounded worker pool for blocking handlers

Blocking API handlers SHALL run on a worker pool of 4 threads by default. The size SHALL be overridable without a rebuild through the `FRIGATE_API_THREAD_POOL_SIZE` environment variable. A value that is not an integer, or is less than 1, SHALL be ignored with a warning, and the default SHALL be used.

#### Scenario: Override
- **WHEN** the container starts with `FRIGATE_API_THREAD_POOL_SIZE=6`
- **THEN** up to 6 blocking API requests run at the same time

#### Scenario: Invalid override
- **WHEN** the container starts with `FRIGATE_API_THREAD_POOL_SIZE=0`
- **THEN** a warning is logged and the pool size is 4

### Requirement: Settled VOD mappings are cacheable

A VOD mapping SHALL be marked cacheable when its window ended more than one maximum segment duration (10 minutes) ago, and not cacheable otherwise. Only the end of the window decides this, not its start, so a short window inside the last hour is cacheable once it can no longer gain segments.

#### Scenario: Window that ended 20 minutes ago
- **WHEN** a player requests a 5-minute VOD window that ended 20 minutes ago
- **THEN** the mapping is marked cacheable, and nginx reuses it for that window's segment requests

#### Scenario: Window touching now
- **WHEN** a player requests a VOD window that ends less than 10 minutes ago
- **THEN** the mapping is marked not cacheable

### Requirement: API JSON and playlists are compressed

nginx SHALL gzip-compress `/api/` JSON responses and HLS playlists larger than 1 KB when the client accepts gzip. This SHALL apply whatever caching headers the upstream handler sends. Video segments SHALL NOT be gzip-compressed.

#### Scenario: Large recordings listing
- **WHEN** a browser that sends `Accept-Encoding: gzip` requests a camera's recordings listing
- **THEN** the response is gzip-encoded

#### Scenario: Video segment
- **WHEN** a player fetches a video segment
- **THEN** the segment is not gzip-encoded

### Requirement: API handlers control their own Cache-Control

A `/api/` response SHALL carry the `Cache-Control` value its handler set, exactly once. A response whose handler sets no `Cache-Control` SHALL get `Cache-Control: no-store`, as upstream does.

#### Scenario: Handler opts in to caching
- **WHEN** a timeline handler returns `Cache-Control: public, max-age=60, stale-while-revalidate=300`
- **THEN** the client receives that one header and no `no-store`

#### Scenario: Handler sets nothing
- **WHEN** a handler returns no `Cache-Control`
- **THEN** the client receives `Cache-Control: no-store`

### Requirement: Per-request playback performance log

Each VOD mapping and progressive clip request SHALL write one JSON line to `perf-logs/api_perf.log`, in the directory that holds the database. The line SHALL carry a timestamp and timing fields in milliseconds, such as variant resolution, SQL, keyframe probe, first byte and total. It SHALL also record whether the mapping was cacheable. The file SHALL rotate at 10 MB and keep 3 backups. These lines SHALL NOT appear in the container log. A failure to write the log SHALL NOT affect the request.

#### Scenario: Reading playback latency
- **WHEN** an operator plays back a recording and then reads `perf-logs/api_perf.log`
- **THEN** the log has a JSON line for that request with its timings and its cacheable flag

#### Scenario: Log directory not writable
- **WHEN** the perf log directory cannot be created
- **THEN** a warning is logged once, and playback works without perf logging
