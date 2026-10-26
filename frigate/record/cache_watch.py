"""Watching the recording cache (fork-only).

The recorders write segments into ``CACHE_DIR`` (a tmpfs) and the mover takes
them out. Two things here look at that directory directly instead of at the
mover's progress:

- the mover's free-space reserve, which keeps the tmpfs from filling while a
  backlog is kept rather than discarded;
- the camera watchdog's recorder freshness, which judges each recorder (camera
  and variant) by the files it writes, so a stalled mover no longer looks like
  a dead recorder.
"""

import datetime
import os
from dataclasses import dataclass
from typing import Optional

from frigate.const import CACHE_DIR, CACHE_SEGMENT_FORMAT

DEFAULT_RECORD_VARIANT = "main"

# Longest startup grace after a recorder starts, is restarted or is enabled.
RECORDER_GRACE = 90.0


def parse_cache_name(name: str) -> Optional[tuple[str, str, float]]:
    """(camera, variant, start time) from a cache segment file name.

    Accepts ``camera@variant@date.mp4`` and the legacy ``camera@date.mp4``.
    """
    if not name.endswith(".mp4"):
        return None

    parts = name[:-4].rsplit("@", maxsplit=2)

    if len(parts) == 3:
        camera, variant, date = parts
    elif len(parts) == 2:
        camera, date = parts
        variant = DEFAULT_RECORD_VARIANT
    else:
        return None

    try:
        start = datetime.datetime.strptime(date, CACHE_SEGMENT_FORMAT).timestamp()
    except ValueError:
        return None

    return camera, variant, start


@dataclass(frozen=True)
class CachedSegment:
    camera: str
    variant: str
    start_time: float
    path: str


def cache_space(path: str) -> tuple[int, int]:
    """(free bytes, total bytes) of the filesystem holding ``path``."""
    stats = os.statvfs(path)
    return stats.f_bavail * stats.f_frsize, stats.f_blocks * stats.f_frsize


def reserve_discards(
    segments: list[CachedSegment],
    free_bytes: int,
    total_bytes: int,
    reserve_percent: float,
) -> list[CachedSegment]:
    """Oldest closed segments to discard to bring free space up to the reserve.

    Empty when the reserve is off or already met. Sizes are read as the
    segments are picked; a segment that has gone already frees nothing.
    """
    if reserve_percent <= 0 or total_bytes <= 0:
        return []

    needed = reserve_percent / 100 * total_bytes - free_bytes

    if needed <= 0:
        return []

    picked: list[CachedSegment] = []
    freed = 0

    for segment in sorted(segments, key=lambda s: s.start_time):
        if freed >= needed:
            break

        try:
            freed += os.path.getsize(segment.path)
        except OSError:
            continue

        picked.append(segment)

    return picked


class RecorderWatch:
    """Health of one camera's recorders, judged by their own cache output.

    Upstream's watchdog reads the mover's messages as recorder health and keeps
    them per camera. A stalled mover then looks like dead recorders, and a hung
    ``sub`` recorder is masked while ``main`` keeps writing. Here:

    - freshness comes from the newest file each recorder (variant) has written
      to the cache: its start time from the name, or its mtime while it grows;
    - the mover's ``latest`` messages, which every pass publishes for every
      camera, are only a heartbeat for the mover itself;
    - valid/invalid messages are kept per variant and judged only while the
      heartbeat is fresh, measured from no earlier than when it last resumed,
      so a mover clearing a backlog does not get recorders restarted.
    """

    def __init__(self, camera: str, now: float, cache_dir: str = CACHE_DIR) -> None:
        self.camera = camera
        self.cache_dir = cache_dir
        self.reset(now)

    def reset(self, now: float) -> None:
        """Forget everything, e.g. when the camera is enabled again."""
        self.started = now
        # variant -> newest start time or mtime seen for it
        self.last_output: dict[str, float] = {}
        # variant -> start time of its newest valid / invalid segment
        self.valid: dict[str, float] = {}
        self.invalid: dict[str, float] = {}
        # variant -> when its recorder was last (re)started
        self.restarted: dict[str, float] = {}
        self.heartbeat: Optional[float] = None
        self.resumed = now
        self.stall_logged = 0.0

    def scan(self) -> None:
        """One pass over the cache; stats only each variant's newest file."""
        prefix = f"{self.camera}@"
        newest: dict[str, tuple[float, str]] = {}

        try:
            entries = list(os.scandir(self.cache_dir))
        except OSError:
            return

        for entry in entries:
            if not entry.name.startswith(prefix):
                continue

            parsed = parse_cache_name(entry.name)

            if parsed is None or parsed[0] != self.camera:
                continue

            _, variant, start = parsed

            if variant not in newest or start > newest[variant][0]:
                newest[variant] = (start, entry.path)

        for variant, (start, path) in newest.items():
            seen = start

            try:
                seen = max(seen, os.stat(path).st_mtime)
            except OSError:
                pass

            self.last_output[variant] = max(self.last_output.get(variant, 0), seen)

    def on_latest(self, now: float, timeout: float) -> None:
        if self.heartbeat is None or now - self.heartbeat > timeout:
            self.resumed = now

        self.heartbeat = now

    def on_valid(self, cache_path: Optional[str], segment_time: float) -> None:
        variant = self._variant(cache_path)
        self.valid[variant] = max(self.valid.get(variant, 0), segment_time)

    def on_invalid(self, cache_path: Optional[str], segment_time: float) -> None:
        variant = self._variant(cache_path)
        self.invalid[variant] = max(self.invalid.get(variant, 0), segment_time)

    def on_restart(self, variant: str, now: float) -> None:
        self.restarted[variant] = now

    def mover_fresh(self, now: float, timeout: float) -> bool:
        return self.heartbeat is not None and now - self.heartbeat <= timeout

    def mover_stalled_for(self, now: float, timeout: float) -> Optional[float]:
        """Seconds without a mover heartbeat, when longer than the timeout."""
        since = self.heartbeat if self.heartbeat is not None else self.started
        return now - since if now - since > timeout else None

    def stale_reason(
        self,
        variant: str,
        now: float,
        timeout: float,
        enabled_at: Optional[float],
    ) -> Optional[str]:
        """Why this recorder should be restarted, or None if it is healthy."""
        grace = min(RECORDER_GRACE, timeout)
        started = max(enabled_at or self.started, self.restarted.get(variant, 0))

        if now - started < grace:
            return None

        if now - self.last_output.get(variant, started) > timeout:
            return "No new recording segments were created"

        # validity is the mover's verdict: only judge it while the mover runs,
        # and never from before it last resumed
        if not self.mover_fresh(now, timeout):
            return None

        since = max(self.resumed, started)
        valid = self.valid.get(variant, 0)

        if now - max(valid, since) > timeout:
            return "No new valid recording segments were created"

        invalid = self.invalid.get(variant)

        if (
            invalid is not None
            and invalid >= valid
            and now - max(invalid, since) > timeout
        ):
            return "No valid segments created since last invalid segment"

        return None

    def _variant(self, cache_path: Optional[str]) -> str:
        parsed = parse_cache_name(os.path.basename(cache_path or ""))
        return parsed[1] if parsed is not None else DEFAULT_RECORD_VARIANT
