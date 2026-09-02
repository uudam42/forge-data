"""Forge Data local CLI (v2.7).

A thin client over the existing application/service layer -- see
`app.cli.main` for the entry point. No pipeline stage logic lives here;
every command calls into `app.runs`, `app.catalog`, `app.sensors`, or
`app.storage.recovery` exactly as the HTTP API does.
"""
