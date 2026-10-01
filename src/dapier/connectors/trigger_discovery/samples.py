"""Sample-envelope helpers and the sample-fetch builders connectors use.

Every sample fetch answers ``{"sample", "source", "connection_id"}`` with
``source`` naming where the payload came from: ``live`` (fetched now),
``history`` (the newest recorded run for the connector), or ``synthetic``
(a documented realistic example for connectors whose traffic was never
recorded — or never flows inbound, like schedule).
"""
import uuid

from .base import DEFAULT_LIMIT, _now_iso


def _through_package(name):
    """Resolve ``name`` on the package at call time, so tests patching
    ``trigger_discovery.<name>`` are honored by every submodule's fetches."""
    import importlib

    return getattr(importlib.import_module(__package__), name)


def as_sample(envelope):
    """Project any dapier event envelope to the six discovery-sample keys."""
    data = envelope.get("data")
    sample = {
        "connector": str(envelope.get("connector") or ""),
        "event": str(envelope.get("event") or ""),
        "data": data if isinstance(data, dict) else {},
        "id": envelope.get("id"),
        "source": envelope.get("source"),
        "occurred_at": envelope.get("occurred_at"),
    }
    if not sample["id"]:
        sample["id"] = f"discover-{uuid.uuid4().hex[:12]}"
    if not sample["occurred_at"]:
        sample["occurred_at"] = _now_iso()
    return sample


def synthetic_sample(connector, event, data):
    """A documented realistic sample for connectors with no recorded traffic."""
    return {
        "connector": connector,
        "event": str(event or ""),
        "data": data,
        "id": f"discover-{uuid.uuid4().hex[:12]}",
        "source": connector,
        "occurred_at": _now_iso(),
    }


def history_sample(connector, event=None):
    """The newest recorded run for ``connector``, as a sample — or None.

    Run history stores one run per workflow handling of a trigger event; the
    same envelope rebuild the replay button uses (``runs.replay_event``)
    turns the newest run's recorded input back into a sample event. With
    ``event``, only runs whose recorded envelope carries that event qualify —
    a multi-event connector never answers a ``file.deleted`` ask with a
    renamed ``file.created`` run.
    """
    from ...api import runs

    summaries = [
        summary for summary in runs.recent(limit=50)
        if summary.get("connector") == connector
    ]
    for summary in summaries:  # recent() is newest-first
        status, payload = runs.api_get(summary["run_id"])
        if status != 200:
            continue
        envelope, error = runs.replay_event(summary["run_id"], payload.get("steps") or [])
        if error or envelope is None:
            continue
        if event and envelope.get("event") != event:
            continue
        return as_sample(envelope)
    return None


def history_or_synthetic_fetch(connector, default_event, synthetic_data):
    """A sample fetch with the common fallback chain: history, then example.

    ``synthetic_data`` is the documented realistic payload used when the
    connector has no recorded runs; ``event`` renames the envelope's event
    on either path.
    """
    def fetch(event=None, connection_id=None, limit=DEFAULT_LIMIT):
        found = _through_package("history_sample")(connector)
        if found is not None:
            if event:
                found["event"] = event
            return {"sample": found, "source": "history", "connection_id": connection_id}
        return {
            "sample": synthetic_sample(connector, event or default_event, dict(synthetic_data)),
            "source": "synthetic",
            "connection_id": connection_id,
        }
    return fetch


def per_event_sample_fetch(connector, default_event, data_by_event):
    """A sample fetch that serves a different documented payload per event.

    Multi-event connectors (Dropbox file events, Zoom meeting and recording
    events) register one sample whose ``event`` request field picks the
    payload: ``data_by_event`` maps each declared event name to its
    realistic synthetic example, drawn from the provider's own webhook
    schema. An unknown event falls back to ``default_event``. Recorded
    history only fills the sample when its replayed envelope carries the
    requested event — see :func:`history_sample`.
    """
    def fetch(event=None, connection_id=None, limit=DEFAULT_LIMIT):
        wanted = event or default_event
        found = _through_package("history_sample")(connector, event=wanted)
        if found is not None:
            return {"sample": found, "source": "history", "connection_id": connection_id}
        data = data_by_event.get(wanted)
        if data is None:
            wanted = default_event
            data = data_by_event[default_event]
        return {
            "sample": synthetic_sample(connector, wanted, dict(data)),
            "source": "synthetic",
            "connection_id": connection_id,
        }
    return fetch
