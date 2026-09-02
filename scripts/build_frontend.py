#!/usr/bin/env python3
"""Builds the React/Vite frontend and stages it as bundled package data.

Run this before building a wheel (`python -m build`) so the compiled GUI
is actually inside the distribution -- `python -m build` does not run
`npm` itself. Vite's own `build.outDir` (see frontend/vite.config.ts)
already points at `../app/web/dist`, so this script is a thin,
CI-friendly wrapper: `npm ci` (reproducible, lockfile-pinned install)
then `npm run build`.

Usage:
    python3 scripts/build_frontend.py

Safe to skip entirely for API-only usage: app/main.py serves a
plain API if app/web/dist/index.html is absent.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = REPO_ROOT / "frontend"
WEB_DIST = REPO_ROOT / "app" / "web" / "dist"


def main() -> int:
    npm = shutil.which("npm")
    if npm is None:
        print("error: npm not found on PATH -- install Node.js to build the frontend", file=sys.stderr)
        return 1

    print(f"[build_frontend] npm ci (in {FRONTEND_DIR})")
    subprocess.run([npm, "ci"], cwd=FRONTEND_DIR, check=True)

    print("[build_frontend] npm run build")
    subprocess.run([npm, "run", "build"], cwd=FRONTEND_DIR, check=True)

    index_html = WEB_DIST / "index.html"
    if not index_html.is_file():
        print(f"error: build finished but {index_html} is missing", file=sys.stderr)
        return 1

    print(f"[build_frontend] done -- {WEB_DIST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
