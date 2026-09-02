"""Packaged home for the built frontend (React/Vite production output).

`app/web/dist/` is populated by `scripts/build_frontend.py` (or manually via
`cd frontend && npm run build && cp -r dist/* ../app/web/dist/`) before a
wheel is built — it is never committed to the repository, matching every
other build artifact. `app.main` serves it via `importlib.resources`, so
this resolves identically from a source checkout and from an installed
wheel. If `dist/` is empty (frontend never built), the backend still runs
fine as an API-only server -- see `app.main`'s static-mount guard.
"""
