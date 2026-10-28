"""Shared time helpers."""
from datetime import datetime, timezone


def now_iso(now=None):
    """An ISO-8601 UTC timestamp: ``now`` when given, the current time otherwise."""
    return (now or datetime.now(timezone.utc)).isoformat()
