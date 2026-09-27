"""Schedule trigger connector: a synthetic next-fire sample.

Schedule fires are synthesized by the worker when EventBridge invokes it,
so discovery has nothing live to fetch and no traffic of its own to have
been recorded. The sample mirrors the worker's envelope
(``engine.worker._schedule_event``) with the current UTC timestamp, and
``event`` names the schedule whose id lands in the sample data — so a
workflow filtered on ``schedule`` matches the sample when the name is real.
"""

from datetime import datetime, timezone

from .trigger_discovery import (
    DEFAULT_LIMIT,
    TriggerDiscovery,
    register_trigger_discovery,
)

# Mirrors schedule_triggers.SCHEDULE_EVENT; inlined to keep this module
# import-light (the triggers package pulls in the persistence layer).
SCHEDULE_EVENT = "schedule.triggered"


def _fetch_sample(event=None, connection_id=None, limit=DEFAULT_LIMIT):
    schedule_id = str(event or "").strip().lower() or "discover"
    fired_at = datetime.now(timezone.utc).isoformat()
    return {
        "sample": {
            "connector": "schedule",
            "event": SCHEDULE_EVENT,
            "data": {"schedule": schedule_id, "utc_time": fired_at},
            "id": f"discover-{schedule_id}",
            "source": schedule_id,
            "occurred_at": fired_at,
        },
        "source": "synthetic",
        "connection_id": connection_id,
    }


register_trigger_discovery(TriggerDiscovery(
    connector="schedule", label="Schedule", kind="sample", resource="",
    fetch=_fetch_sample))
