"""Tests for frigate.util.media — the ffprobe keyframe probe and its cache."""

import subprocess as sp
import threading
from unittest import TestCase, main
from unittest.mock import patch

import frigate.util.media as media
from frigate.util.media import get_keyframe_before


def _ffprobe_output(keyframe_times_s: list[float]) -> "sp.CompletedProcess":
    """Build a fake ffprobe CompletedProcess emitting the given keyframe times.

    A non-keyframe packet is interleaved to prove it is ignored.
    """
    lines = []
    for ts in keyframe_times_s:
        lines.append(f"{ts:.6f},K_")
        lines.append(f"{ts + 0.5:.6f},__")  # non-keyframe, must be skipped
    stdout = ("\n".join(lines) + "\n").encode()
    return sp.CompletedProcess(
        args=["ffprobe"], returncode=0, stdout=stdout, stderr=b""
    )


class TestGetKeyframeBefore(TestCase):
    def setUp(self) -> None:
        # the cache is module-global; start each test from a clean state
        media._keyframe_cache.clear()
        media._keyframe_inflight_locks.clear()

    def test_snaps_to_keyframe_at_or_before_offset(self) -> None:
        with patch.object(
            media.sp, "run", return_value=_ffprobe_output([0.0, 1.0, 2.0, 3.0])
        ):
            self.assertEqual(get_keyframe_before("a.mp4", 2500), 2000)
            self.assertEqual(get_keyframe_before("a.mp4", 2000), 2000)  # inclusive
            self.assertEqual(get_keyframe_before("a.mp4", 999), 0)
            self.assertIsNone(get_keyframe_before("a.mp4", -1))

    def test_return_type_is_int(self) -> None:
        with patch.object(media.sp, "run", return_value=_ffprobe_output([0.0, 2.0])):
            result = get_keyframe_before("a.mp4", 5000)
            self.assertIsInstance(result, int)
            self.assertEqual(result, 2000)

    def test_sub_millisecond_boundary_matches_float_semantics(self) -> None:
        # 29.97fps-style GOP keyframe at 2.002667s. An offset of exactly 2002ms is
        # BEFORE it (2.002667 > 2.002), so it must NOT be selected — the previous
        # keyframe (0) wins, matching the original float-seconds comparison.
        with patch.object(
            media.sp, "run", return_value=_ffprobe_output([0.0, 2.002667, 4.005333])
        ):
            self.assertEqual(get_keyframe_before("a.mp4", 2002), 0)
            self.assertEqual(get_keyframe_before("a.mp4", 2003), 2002)

    def test_result_is_cached_per_file(self) -> None:
        with patch.object(
            media.sp, "run", return_value=_ffprobe_output([0.0, 1.0, 2.0])
        ) as mock_run:
            for offset in (500, 1500, 2500, 900):
                get_keyframe_before("a.mp4", offset)
            self.assertEqual(mock_run.call_count, 1)  # one probe for four calls

    def test_failed_probe_is_cached(self) -> None:
        with patch.object(
            media.sp, "run", side_effect=sp.TimeoutExpired(cmd="ffprobe", timeout=5)
        ) as mock_run:
            self.assertIsNone(get_keyframe_before("bad.mp4", 100))
            self.assertIsNone(get_keyframe_before("bad.mp4", 100))
            # a failing probe (5s timeout) must not be re-run on every segment
            self.assertEqual(mock_run.call_count, 1)

    def test_nonzero_returncode_returns_none(self) -> None:
        failed = sp.CompletedProcess(
            args=["ffprobe"], returncode=1, stdout=b"", stderr=b"boom"
        )
        with patch.object(media.sp, "run", return_value=failed):
            self.assertIsNone(get_keyframe_before("a.mp4", 1000))

    def test_single_flight_coalesces_concurrent_misses(self) -> None:
        probe_started = threading.Event()

        def slow_run(*_args, **_kwargs):
            # hold the probe open so all threads pile up on the same cold path
            probe_started.set()
            threading.Event().wait(0.05)
            return _ffprobe_output([0.0, 1.0, 2.0, 3.0])

        with patch.object(media.sp, "run", side_effect=slow_run) as mock_run:
            results: list = []
            results_lock = threading.Lock()

            def worker():
                r = get_keyframe_before("c.mp4", 2500)
                with results_lock:
                    results.append(r)

            threads = [threading.Thread(target=worker) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(results, [2000] * 8)
            self.assertEqual(mock_run.call_count, 1)  # coalesced to a single probe

    def test_cache_is_bounded(self) -> None:
        with patch.object(media.sp, "run", return_value=_ffprobe_output([0.0, 1.0])):
            for i in range(media._KEYFRAME_CACHE_MAX + 25):
                get_keyframe_before(f"f{i}.mp4", 500)
            self.assertEqual(len(media._keyframe_cache), media._KEYFRAME_CACHE_MAX)


if __name__ == "__main__":
    main(verbosity=2)
