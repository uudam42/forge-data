"""Bundled, read-only application resources (schema definitions, etc.).

Distinct from user workspace data: everything under this package is
installed alongside the application code and looked up via
`importlib.resources`, so it works identically whether Forge Data is
run from a source checkout or an installed wheel. See
`app.core.config._default_schema_dir`.
"""
