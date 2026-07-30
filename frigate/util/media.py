"""Utilities for media file inspection."""

import subprocess as sp
import threading
from collections import OrderedDict

from frigate.const import DEFAULT_FFMPEG_VERSION

FFPROBE_PATH = (
    f"/usr/lib/ffmpeg/{DEFAULT_FFMPEG_VERSION}/bin/ffprobe"
    if DEFAULT_FFMPEG_VERSION
    else "ffprobe"
)

# The keyframe index of a recording file never changes once the file is closed,
# yet get_keyframe_before is called repeatedly for the same file during playback
# (nginx-vod-module re-requests the VOD mapping per segment for non-cacheable
# recent windows, and each mapping request re-probes the boundary clip). Reading
# the full packet index with ffprobe is a subprocess spawn each time, so cache the
# keyframe list per file. The cost is a function of the file (path) only — the
# offset just selects within the already-read list — so path is the cache key.
_KEYFRAME_CACHE_MAX = 512  # number of files to remember (~a few hours of segments)
# "path in cache" distinguishes "not probed yet" from "probed, ffprobe failed"
# (stored as None), so a failed probe is not repeated on every segment request.
_keyframe_cache: "OrderedDict[str, tuple[float, ...] | None]" = OrderedDict()
_keyframe_cache_lock = threading.Lock()  # guards the cache map + inflight map
_keyframe_inflight_locks: dict[str, threading.Lock] = {}  # single-flight per path


def _run_ffprobe_keyframes(path: str) -> "tuple[float, ...] | None":
    """Read all keyframe timestamps (ms) from the file, in ffprobe packet order.

    Timestamps are kept as floats (sub-ms precision preserved) so the boundary
    comparison in get_keyframe_before matches the original float-seconds logic
    exactly. Returns None if ffprobe fails, so the caller can distinguish failure
    from an empty keyframe list.
    """
    try:
        result = sp.run(
            [
                FFPROBE_PATH,
                "-select_streams",
                "v:0",
                "-show_entries",
                "packet=pts_time,flags",
                "-of",
                "csv=p=0",
                "-loglevel",
                "error",
                path,
            ],
            capture_output=True,
            timeout=5,
        )
    except (sp.TimeoutExpired, FileNotFoundError):
        return None

    if result.returncode != 0:
        return None

    keyframes: list[float] = []
    for line in result.stdout.decode().strip().splitlines():
        parts = line.strip().split(",")
        if len(parts) != 2:
            continue
        ts_str, flags = parts
        if "K" not in flags:
            continue
        try:
            ts = float(ts_str)
        except ValueError:
            continue
        keyframes.append(ts * 1000)

    return tuple(keyframes)


def _get_keyframes(path: str) -> "tuple[float, ...] | None":
    """Return the cached keyframe list for a file, probing (once) on a miss.

    Thread-safe with single-flight: concurrent misses for the same path wait on a
    per-path lock so ffprobe runs only once, not once per caller.
    """
    with _keyframe_cache_lock:
        if path in _keyframe_cache:
            _keyframe_cache.move_to_end(path)
            return _keyframe_cache[path]
        # ensure a single lock instance per path for concurrent callers to share
        path_lock = _keyframe_inflight_locks.setdefault(path, threading.Lock())

    with path_lock:
        # another thread may have populated the cache while we waited for the lock
        with _keyframe_cache_lock:
            if path in _keyframe_cache:
                _keyframe_cache.move_to_end(path)
                return _keyframe_cache[path]

        keyframes = _run_ffprobe_keyframes(path)

        with _keyframe_cache_lock:
            _keyframe_cache[path] = keyframes
            _keyframe_cache.move_to_end(path)
            while len(_keyframe_cache) > _KEYFRAME_CACHE_MAX:
                _keyframe_cache.popitem(last=False)
            # cache is populated; drop the inflight lock so it can be GC'd
            _keyframe_inflight_locks.pop(path, None)

    return keyframes


def get_keyframe_before(path: str, offset_ms: int) -> int | None:
    """Get the timestamp (ms) of the last keyframe at or before offset_ms.

    Uses ffprobe packet index to read keyframe positions from the mp4 file
    (cached per file). Returns None if ffprobe fails or no keyframe is found
    before the offset.
    """
    keyframes = _get_keyframes(path)

    if keyframes is None:
        return None

    best_ms = None
    for ts_ms in keyframes:
        if ts_ms <= offset_ms:
            best_ms = ts_ms
        else:
            break

    return int(best_ms) if best_ms is not None else None
