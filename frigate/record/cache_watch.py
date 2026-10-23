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

import os
from dataclasses import dataclass


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
