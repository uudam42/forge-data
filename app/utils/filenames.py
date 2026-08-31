"""Filename sanitization.

Client-supplied filenames are untrusted input. They must never be used
directly to build filesystem paths (path traversal, absolute paths, null
bytes, reserved names, etc.).
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import PurePosixPath

_SAFE_CHARS_RE = re.compile(r"[^A-Za-z0-9._-]+")
_DEFAULT_NAME = "upload"


def sanitize_filename(raw_filename: str | None) -> str:
    """Return a filesystem-safe basename derived from client input.

    Strips directory components (path traversal), normalizes unicode,
    collapses unsafe characters, and guarantees a non-empty result.
    """
    if not raw_filename:
        return _DEFAULT_NAME

    # Drop any directory components the client tried to sneak in, whether
    # posix- or windows-style, and strip null bytes.
    cleaned = raw_filename.replace("\x00", "")
    cleaned = cleaned.replace("\\", "/")
    basename = PurePosixPath(cleaned).name

    normalized = unicodedata.normalize("NFKD", basename)
    normalized = normalized.encode("ascii", "ignore").decode("ascii")

    safe = _SAFE_CHARS_RE.sub("_", normalized).strip("._")

    if not safe or safe in {".", ".."}:
        return _DEFAULT_NAME

    return safe


def extension_of(filename: str) -> str:
    """Return the lowercase extension (including the dot), or '' if none."""
    return PurePosixPath(filename).suffix.lower()
