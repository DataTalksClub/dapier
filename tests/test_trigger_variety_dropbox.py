"""Dropbox trigger variety: one documented sample per declared file event.

The catalog declares file.created / file.updated / file.deleted and the
resolver publishes all three (see triggers.intake.dropbox_resolver.
process_entry), so the sample pull — the shared api_discover dispatch behind
`dapier triggers sample` and the designer's test panel — must serve each
event its own realistic payload, and recorded history must never answer one
event's ask with another event's run.
"""
import pytest

from src.dapier.connectors import trigger_discovery
from src.dapier.connectors.registry import CONNECTORS

import src.dapier.connectors  # noqa: F401  (import = registration)


@pytest.fixture(autouse=True)
def no_recorded_history(monkeypatch):
    """Pin the fallback chain to synthetic: history depends on run tables."""
    monkeypatch.setattr(
        trigger_discovery, "history_sample",
        lambda connector, event=None: None)


def _sample(**kwargs):
    status, payload = trigger_discovery.api_discover(
        {"connector": "dropbox", **kwargs})
    assert status == 200, payload
    return payload


def test_every_declared_event_serves_its_own_sample():
    events = CONNECTORS["dropbox"].events
    assert events == ("file.created", "file.updated", "file.deleted")
    for event in events:
        payload = _sample(event=event)
        assert payload["event"] == event, event
        assert payload["sample"]["event"] == event, event
        assert payload["source"] == "synthetic", event
        data = payload["sample"]["data"]
        assert data["account_id"].startswith("dbid:"), event
        assert data["path"] and data["path_lower"] == data["path"].lower(), event


def test_created_and_updated_carry_file_state_deleted_only_the_path():
    created = _sample(event="file.created")["sample"]["data"]
    updated = _sample(event="file.updated")["sample"]["data"]
    deleted = _sample(event="file.deleted")["sample"]["data"]
    assert created["rev"] != updated["rev"]
    assert created["size"] != updated["size"]
    for state in (created, updated):
        assert state["file_id"] and "rev" in state and "size" in state
    assert deleted == {
        "account_id": created["account_id"],
        "path": created["path"],
        "path_lower": created["path_lower"],
    }


def test_default_and_unknown_events_fall_back_to_file_created():
    default = _sample()
    assert default["sample"]["event"] == "file.created"
    assert default["sample"]["data"]["rev"] == "discover1"
    unknown = _sample(event="file.renamed")
    assert unknown["sample"]["event"] == "file.created"
    assert unknown["sample"]["data"] == default["sample"]["data"]


def test_history_never_answers_one_event_with_another_events_run(monkeypatch):
    recorded = {
        "connector": "dropbox", "event": "file.created",
        "data": {"account_id": "dbid:real", "path": "/Real.txt",
                 "path_lower": "/real.txt", "file_id": "id:real",
                 "rev": "real1", "content_hash": None, "size": 42},
        "id": "dropbox:dbid:real:id:real:real1",
        "source": "dbid:real", "occurred_at": "2026-09-28T10:00:00Z",
    }

    def fake_history(connector, event=None):
        return dict(recorded) if event == "file.created" else None

    monkeypatch.setattr(trigger_discovery, "history_sample", fake_history)
    hit = _sample(event="file.created")
    assert hit["source"] == "history"
    assert hit["sample"]["data"]["path"] == "/Real.txt"
    miss = _sample(event="file.deleted")
    assert miss["source"] == "synthetic"
    assert miss["sample"]["event"] == "file.deleted"
    assert "rev" not in miss["sample"]["data"]
