"""Stopwatch and memory-sampling helpers for the benchmark harness.

Deliberately outside data_loader/ -- nothing in the package imports this,
only bench/ scripts do.
"""
from __future__ import annotations

import resource
import sys
import time


class Stopwatch:
    """`with Stopwatch() as sw: ...` then `sw.seconds` holds elapsed wall time."""

    def __enter__(self):
        self._start = time.perf_counter()
        self.seconds = None
        return self

    def __exit__(self, *exc):
        self.seconds = time.perf_counter() - self._start
        return False


def peak_rss_bytes() -> int:
    """Peak resident set size of this process so far, in bytes.

    `ru_maxrss` units differ by platform: KiB on Linux, bytes on macOS. This
    number is only meaningful as a per-case figure because each benchmark
    case runs in its own subprocess (see run_benchmark.py) -- in a shared
    process it would just be a high-water mark left over from whichever
    earlier case used the most memory.
    """
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(rss if sys.platform == "darwin" else rss * 1024)
