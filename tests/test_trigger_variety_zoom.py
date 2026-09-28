"""Zoom trigger variety: one documented sample per declared event.

The catalog declares recording.completed / recording.transcript_completed /
meeting.started / meeting.ended and the webhook intake publishes all four
(see triggers.intake.zoom_webhooks), so the sample pull — the shared
api_discover dispatch behind `dapier triggers sample` and the designer's
test panel — must serve each event its own realistic, metadata-only
payload, and recorded history must never answer one event's ask with
another event's run.
"""
import json

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
        {"connector": "zoom", **kwargs})
    assert status == 200, payload
    return payload


def test_every_declared_event_serves_its_own_sample():
    events = CONNECTORS["zoom"].events
    assert events == ("recording.completed", "recording.transcript_completed",
                      "meeting.started", "meeting.ended")
    for event in events:
        payload = _sample(event=event)
        assert payload["event"] == event, event
        assert payload["sample"]["event"] == event, event
        assert payload["source"] == "synthetic", event
        dumped = json.dumps(payload["sample"]["data"])
        assert "download_token" not in dumped and "object" not in dumped, event


def test_recording_events_carry_files_and_meetings_the_schedule():
    recording = _sample(event="recording.completed")["sample"]["data"]
    transcript = _sample(event="recording.transcript_completed")["sample"]["data"]
    started = _sample(event="meeting.started")["sample"]["data"]
    ended = _sample(event="meeting.ended")["sample"]["data"]
    assert [f["file_type"] for f in recording["video_files"]] == ["MP4"]
    assert sorted(f["file_type"] for f in transcript["video_files"]) == \
        ["MP4", "TRANSCRIPT"]
    for state in (recording, transcript):
        assert state["share_url"] and state["video_files"][0]["play_url"]
    assert started["start_time"] and started["duration"]
    assert "end_time" not in started and ended["end_time"]
    for state in (started, ended):
        assert "video_files" not in state and state["timezone"]


def test_default_and_unknown_events_fall_back_to_recording_completed():
    default = _sample()
    assert default["sample"]["event"] == "recording.completed"
    unknown = _sample(event="meeting.participant_joined")
    assert unknown["sample"]["event"] == "recording.completed"
    assert unknown["sample"]["data"] == default["sample"]["data"]


def test_history_never_answers_one_event_with_another_events_run(monkeypatch):
    recorded = {
        "connector": "zoom", "event": "meeting.started",
        "data": {"account_id": "real-account", "meeting_id": "1",
                 "meeting_uuid": "real-uuid", "topic": "Real one",
                 "duration": 30, "timezone": "UTC"},
        "id": "zoom:real", "source": "zoom-main",
        "occurred_at": "2026-09-28T09:00:00Z",
    }

    def fake_history(connector, event=None):
        return dict(recorded) if event == "meeting.started" else None

    monkeypatch.setattr(trigger_discovery, "history_sample", fake_history)
    hit = _sample(event="meeting.started")
    assert hit["source"] == "history"
    assert hit["sample"]["data"]["topic"] == "Real one"
    miss = _sample(event="recording.completed")
    assert miss["source"] == "synthetic"
    assert miss["sample"]["event"] == "recording.completed"
    assert miss["sample"]["data"]["video_files"]
