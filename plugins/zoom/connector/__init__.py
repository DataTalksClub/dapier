"""Zoom connector: meetings, past meetings, webinars and recordings
discovery, plus the health check.

Zoom connections come in two flavors: OAuth grants (refreshable tokens,
verified against ``users/me``) and webhook-only setups that store just a
signing secret. Both runners delegate to the shared provider layer; a
webhook-only connection reports a failed check with the refresh error
instead of pretending to be healthy.

The OAuth flavor also drives the ``zoom.recordings`` poll source: cloud
recordings fire ``recording.completed`` on a schedule with no Zoom app to
configure, the same event the webhook-only setups receive.

Submodules (import = registration, like every connector module):

- :mod:`.meetings` / :mod:`.webinars` — the action catalog entries;
- :mod:`.discovery` — connection-scoped listings, the health check and
  the trigger-discovery options;
- :mod:`.samples` — the documented per-event webhook samples;
- :mod:`.polling` — the ``zoom.recordings`` poll source and the
  poll-aware sample pull.
"""
from . import meetings, samples, webinars  # noqa: F401  (import = registration)
from . import discovery, polling  # noqa: F401
