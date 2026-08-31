"""Structured-enough application logging setup.

Uses stdlib logging with a consistent format. Never log full payload bytes —
only metadata (ingestion_id, session_id, filenames, sizes, hashes).
"""

from __future__ import annotations

import logging
import sys


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    if root.handlers:
        # Already configured (e.g. re-imported in tests) — avoid duplicate handlers.
        root.setLevel(level)
        return

    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    handler.setFormatter(formatter)
    root.addHandler(handler)
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
