"""Single authoritative version string for Forge Data.

Read by: `pyproject.toml` (via setuptools dynamic version), the FastAPI
app's own `version=` field (`app/main.py`), and `forge --version`
(`app/cli/main.py`). Never hardcode the version string a second time
anywhere else.

Still pre-release relative to the v2.0.0 stable target: v2.1-v2.7 are
development milestones on the way there, not the release itself. Bump
the `.devN` suffix per milestone; drop it only at the actual v2.0.0
release-hardening/RC boundary.
"""

__version__ = "2.0.0.dev7"
