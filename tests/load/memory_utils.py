"""Memory-measurement utilities for load tests and benchmarks (v2.2).

Peak RSS is measured by running the target function in an ISOLATED CHILD
PROCESS and reading that child's own `resource.getrusage(RUSAGE_SELF).
ru_maxrss` after it exits. This is deliberate, not incidental:
`ru_maxrss` is a running historical maximum for the process's entire
lifetime, so measuring it in the pytest process itself would be
contaminated by whatever every previous test already allocated — a
strictly increasing counter across the whole test session, useless for
"how much memory did THIS operation use." A fresh subprocess starts that
counter at (near) zero.

`fn` must be a module-level, picklable callable (never a lambda or
closure) — the child process uses `multiprocessing`'s "spawn" start
method, which re-imports and re-pickles the target rather than forking
the parent's memory image; forking would defeat the entire point of
measuring a clean process.

Not used by the normal (non-`load`-marked) test suite — see
docs/DETAILED_GUIDE.md#load-test-methodology.
"""

from __future__ import annotations

import multiprocessing as mp
import resource
import sys
import time
import traceback
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class MeasuredRun:
    peak_rss_bytes: int
    wall_seconds: float
    result: Any


def _rss_bytes_from_ru_maxrss(ru_maxrss: int) -> int:
    # POSIX leaves the unit unspecified; in practice: macOS reports bytes,
    # Linux reports kibibytes. Normalize to bytes.
    if sys.platform == "darwin":
        return ru_maxrss
    return ru_maxrss * 1024


def _child_entry(fn: Callable, args: tuple, kwargs: dict, result_queue, error_queue) -> None:
    try:
        start = time.monotonic()
        result = fn(*args, **kwargs)
        elapsed = time.monotonic() - start
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        result_queue.put((_rss_bytes_from_ru_maxrss(peak), elapsed, result))
    except Exception:
        error_queue.put(traceback.format_exc())


def measure_peak_rss(fn: Callable, *args, timeout: float = 900.0, **kwargs) -> MeasuredRun:
    """Runs `fn(*args, **kwargs)` in a fresh subprocess; returns its peak
    RSS (bytes), wall-clock duration (seconds), and return value.
    Re-raises as RuntimeError (with the child's traceback text) if the
    child raised, timed out, or produced no result."""
    ctx = mp.get_context("spawn")
    result_queue: mp.Queue = ctx.Queue()
    error_queue: mp.Queue = ctx.Queue()
    proc = ctx.Process(target=_child_entry, args=(fn, args, kwargs, result_queue, error_queue))
    proc.start()
    proc.join(timeout=timeout)

    if proc.is_alive():
        proc.kill()
        proc.join(timeout=5)
        raise RuntimeError(f"measure_peak_rss: child process exceeded timeout={timeout}s and was killed")

    if not error_queue.empty():
        raise RuntimeError(f"measure_peak_rss: child process raised:\n{error_queue.get()}")
    if result_queue.empty():
        raise RuntimeError(f"measure_peak_rss: child process produced no result (exitcode={proc.exitcode})")

    peak_rss_bytes, elapsed, result = result_queue.get()
    return MeasuredRun(peak_rss_bytes=peak_rss_bytes, wall_seconds=elapsed, result=result)


def format_bytes(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024.0:
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TB"
