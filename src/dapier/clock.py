"""Shared time helpers."""
from datetime import datetime, timezone


def now_iso(now=None):
    """An ISO-8601 UTC timestamp: ``now`` when given, the current time otherwise."""
    return (now or datetime.now(timezone.utc)).isoformat()


def parse_ts(value):
    """An aware UTC datetime for an ISO date/datetime, else None.

    Accepts date-only ("2026-09-25") and full timestamps; a trailing Z and
    a missing offset are read as UTC. Unparseable input returns None so the
    caller can fall back to string comparison.
    """
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)
