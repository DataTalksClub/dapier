"""Google connector: Sheets, Drive, Calendar, Gmail and YouTube
registrations — the actions' catalog entries, the connection-scoped
discovery resources, the Google and YouTube identity health checks, the
trigger-sample/options discovery, and the poll sources — moved verbatim
from ``src/dapier/connectors/``.

Submodules (import = registration, like every connector module):
sheets, drive, calendar, gmail, youtube.
"""
from . import calendar, drive, gmail, sheets, youtube  # noqa: F401  (import = registration)
