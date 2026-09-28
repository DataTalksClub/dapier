"""Zoom webhook connection and recording trigger behavior."""

import hashlib
import hmac
import json
import time

from src.dapier.connections import zoom
from src.dapier.connections import importing
from src.dapier.triggers.intake import zoom_webhooks
from src.dapier.api.admin import routes as admin_routes
from src.dapier.api import router as ingress
from dapier_cli import commands


class Table:
    def __init__(self):
        self.items = {}

    def get_item(self, Key):
        return {"Item": self.items[Key["connection_id"]]} if Key["connection_id"] in self.items else {}

    def put_item(self, Item):
        self.items[Item["connection_id"]] = dict(Item)


def signed(message, secret="zoom-secret-123456", timestamp=None):
    body = json.dumps(message, separators=(",", ":")).encode()
    stamp = str(timestamp or int(time.time()))
    digest = hmac.new(secret.encode(), b"v0:" + stamp.encode() + b":" + body,
                      hashlib.sha256).hexdigest()
    return body, {"X-Zm-Request-Timestamp": stamp, "X-Zm-Signature": f"v0={digest}"}


def setup(monkeypatch):
    table = Table()
    secrets = {}
    monkeypatch.setattr(zoom.credentials, "get_credential",
                        lambda key: secrets[key])
    monkeypatch.setattr(zoom.credentials, "put_credential",
                        lambda key, value, **_: secrets.__setitem__(key, value))
    status, public = zoom.save(
        {"connection_id": "zoom", "provider": "zoom", "display_name": "Zoom",
         "token": "zoom-secret-123456"},
        operator_subject="operator", connections_table=table)
    assert status == 200
    assert "zoom-secret" not in str(public)
    assert table.items["zoom"]["status"] == "ready"
    return table, secrets


def test_zoom_connection_is_write_only_and_rotation_requires_validation(monkeypatch):
    table, secrets = setup(monkeypatch)
    body, headers = signed({"event": "endpoint.url_validation", "payload": {"plainToken": "challenge"}})
    status, answer = zoom_webhooks.handle("zoom", headers, body, connections_table=table,
                                         publish=lambda *_args, **_kwargs: None)
    assert status == 200
    assert answer == {"plainToken": "challenge", "encryptedToken": hmac.new(
        secrets["oauth#zoom"]["webhook_secret"].encode(), b"challenge", hashlib.sha256).hexdigest()}
    assert table.items["zoom"]["status"] == "connected"
    status, _ = zoom.save({"connection_id": "zoom", "provider": "zoom", "token": "new-secret-123456"},
                          operator_subject="operator", connections_table=table)
    assert status == 200
    assert table.items["zoom"]["status"] == "ready"
    assert secrets["oauth#zoom"] == {"webhook_secret": "new-secret-123456"}


def test_zoom_signature_and_account_binding(monkeypatch):
    table, _ = setup(monkeypatch)
    published = []
    payload = {"event": "recording.completed", "event_ts": 1000,
               "download_token": "private-download-token",
               "payload": {"account_id": "account-1", "object": {
                   "id": 123, "uuid": "meeting-uuid", "topic": "Course",
                   "recording_files": [
                       {"id": "video", "file_type": "MP4", "play_url": "https://zoom.test/play"},
                       {"id": "transcript", "file_type": "TRANSCRIPT"},
                   ]}}}
    body, headers = signed(payload)
    publish = lambda *args, **kwargs: published.append((args, kwargs))
    assert zoom_webhooks.handle("zoom", {}, body, connections_table=table, publish=publish)[0] == 401
    assert zoom_webhooks.handle("zoom", headers, body, connections_table=table,
                                publish=publish, now=time.time() + 600)[0] == 401
    assert zoom_webhooks.handle("zoom", headers, body, connections_table=table, publish=publish)[0] == 200
    assert published[0][0][:2] == ("zoom", "recording.completed")
    assert published[0][1]["source"] == "zoom"
    assert published[0][0][2]["video_files"] == [
        {"id": "video", "file_type": "MP4", "recording_type": None,
         "file_size": None, "play_url": "https://zoom.test/play", "download_url": None}]
    assert "private-download-token" not in str(published)
    assert table.items["zoom"]["verified_account_id"] == "account-1"
    assert zoom_webhooks.handle("zoom", headers, body, connections_table=table, publish=publish)[1]["accepted"]
    assert published[0][1]["event_id"] == published[1][1]["event_id"]
    other = {**payload, "payload": {**payload["payload"], "account_id": "account-2"}}
    other_body, other_headers = signed(other)
    assert zoom_webhooks.handle("zoom", other_headers, other_body,
                                connections_table=table, publish=publish)[0] == 403
    assert len(published) == 2


def test_zoom_ignores_non_video_recordings(monkeypatch):
    table, _ = setup(monkeypatch)
    body, headers = signed({"event": "recording.completed", "payload": {"account_id": "a",
                            "object": {"uuid": "u", "recording_files": [{"file_type": "M4A"}]}}})
    published = []
    assert zoom_webhooks.handle("zoom", headers, body, connections_table=table,
                                publish=lambda *args, **kwargs: published.append(args))[1] == {"accepted": False}
    assert published == []


def test_zoom_transcript_event_publishes_the_transcript_file(monkeypatch):
    table, _ = setup(monkeypatch)
    body, headers = signed({"event": "recording.transcript_completed", "event_ts": 2000,
                            "payload": {"account_id": "account-1", "object": {
                                "id": 123, "uuid": "meeting-uuid", "topic": "Course",
                                "recording_files": [
                                    {"id": "video", "file_type": "MP4",
                                     "play_url": "https://zoom.test/play"},
                                    {"id": "vtt", "file_type": "TRANSCRIPT",
                                     "play_url": "https://zoom.test/vtt"},
                                ]}}})
    published = []
    status, answer = zoom_webhooks.handle("zoom", headers, body, connections_table=table,
                                          publish=lambda *a, **k: published.append((a, k)))
    assert status == 200
    assert answer == {"accepted": True}
    assert published[0][0][:2] == ("zoom", "recording.transcript_completed")
    data = published[0][0][2]
    assert [f["file_type"] for f in data["video_files"]] == ["MP4", "TRANSCRIPT"]
    assert "object" not in json.dumps(data)


def test_zoom_meeting_lifecycle_events_publish_metadata_only(monkeypatch):
    table, _ = setup(monkeypatch)
    meeting = {"account_id": "account-1", "object": {
        "id": 123, "uuid": "meeting-uuid", "topic": "Course",
        "host_id": "host-1", "start_time": "2026-09-28T09:00:00Z",
        "duration": 45, "timezone": "Europe/Berlin",
        "participant": {"user_name": "Private Person"},
        "settings": {"approval_type": 2}}}
    started_body, started_headers = signed(
        {"event": "meeting.started", "event_ts": 3000, "payload": dict(meeting)})
    ended_body, ended_headers = signed(
        {"event": "meeting.ended", "event_ts": 3060000,
         "payload": {**meeting, "object": {**meeting["object"],
                                           "end_time": "2026-09-28T09:45:00Z"}}})
    published = []
    for body, headers in ((started_body, started_headers), (ended_body, ended_headers)):
        status, answer = zoom_webhooks.handle("zoom", headers, body, connections_table=table,
                                              publish=lambda *a, **k: published.append((a, k)))
        assert status == 200
        assert answer == {"accepted": True}
    assert [args[1] for args, _ in published] == ["meeting.started", "meeting.ended"]
    started_data, ended_data = published[0][0][2], published[1][0][2]
    assert started_data["topic"] == "Course" and started_data["duration"] == 45
    assert "end_time" not in started_data and ended_data["end_time"] == "2026-09-28T09:45:00Z"
    dumped = json.dumps([started_data, ended_data])
    assert "Private Person" not in dumped and "approval_type" not in dumped
    assert "object" not in dumped and "download_token" not in dumped
    # distinct lifecycle moments never share a dedup id
    assert published[0][1]["event_id"] != published[1][1]["event_id"]


def test_zoom_drops_unsubscribed_events(monkeypatch):
    table, _ = setup(monkeypatch)
    body, headers = signed({"event": "meeting.participant_joined", "event_ts": 4000,
                            "payload": {"account_id": "account-1",
                                        "object": {"id": 123, "uuid": "meeting-uuid"}}})
    published = []
    status, answer = zoom_webhooks.handle("zoom", headers, body, connections_table=table,
                                          publish=lambda *a, **k: published.append((a, k)))
    assert status == 200
    assert answer == {"accepted": False}
    assert published == []


def test_zoom_meeting_events_reject_incomplete_deliveries(monkeypatch):
    table, _ = setup(monkeypatch)
    meeting_object = {"id": 123, "uuid": "meeting-uuid", "topic": "Course",
                      "host_id": "host-1", "start_time": "2026-09-28T09:00:00Z",
                      "duration": 45, "timezone": "Europe/Berlin"}
    incomplete = [
        {"event": "meeting.started", "event_ts": 5000,
         "payload": {"account_id": "account-1",
                     "object": {**meeting_object, "uuid": None}}},
        {"event": "meeting.started", "event_ts": 5001,
         "payload": {"account_id": "account-1",
                     "object": {**meeting_object, "id": None}}},
        {"event": "meeting.started",
         "payload": {"account_id": "account-1", "object": meeting_object}},
        {"event": "meeting.ended", "event_ts": 5002,
         "payload": {"object": meeting_object}},
        {"event": "meeting.started", "event_ts": 5003,
         "payload": {"account_id": "account-1"}},
    ]
    published = []
    for message in incomplete:
        body, headers = signed(message)
        assert zoom_webhooks.handle("zoom", headers, body, connections_table=table,
                                    publish=lambda *a, **k: published.append((a, k))) == \
            (400, {"error": "incomplete Zoom meeting event"}), message
    assert published == []
    assert not table.items["zoom"].get("verified_account_id")


def test_zoom_transcript_event_rejects_incomplete_deliveries(monkeypatch):
    table, _ = setup(monkeypatch)
    recording_object = {"id": 123, "uuid": "meeting-uuid", "topic": "Course",
                        "recording_files": [{"id": "video", "file_type": "MP4"}]}
    incomplete = [
        {"event": "recording.transcript_completed",
         "payload": {"account_id": "account-1", "object": recording_object}},
        {"event": "recording.transcript_completed", "event_ts": 6000,
         "payload": {"object": {**recording_object, "uuid": None}}},
        {"event": "recording.transcript_completed", "event_ts": 6001,
         "payload": {"object": recording_object}},
    ]
    published = []
    for message in incomplete:
        body, headers = signed(message)
        assert zoom_webhooks.handle("zoom", headers, body, connections_table=table,
                                    publish=lambda *a, **k: published.append((a, k))) == \
            (400, {"error": "incomplete Zoom recording event"}), message
    assert published == []


def test_zoom_new_events_keep_stable_dedup_identities(monkeypatch):
    table, _ = setup(monkeypatch)
    published = []
    publish = lambda *args, **kwargs: published.append((args, kwargs))
    meeting = {"account_id": "account-1", "object": {
        "id": 123, "uuid": "meeting-uuid", "topic": "Course",
        "start_time": "2026-09-28T09:00:00Z"}}
    body, headers = signed({"event": "meeting.started", "event_ts": 7000,
                            "payload": meeting})
    for _ in range(2):
        assert zoom_webhooks.handle("zoom", headers, body, connections_table=table,
                                    publish=publish) == (200, {"accepted": True})
    started_id = published[0][1]["event_id"]
    assert started_id == published[1][1]["event_id"]
    assert published[0][0][2] == {"connection_id": "zoom", "account_id": "account-1",
                                  "uuid": "meeting-uuid", "id": 123, "topic": "Course",
                                  "host_id": None, "start_time": "2026-09-28T09:00:00Z",
                                  "duration": None, "timezone": None}
    ended_body, ended_headers = signed({"event": "meeting.ended", "event_ts": 7000,
                                        "payload": meeting})
    zoom_webhooks.handle("zoom", ended_headers, ended_body,
                         connections_table=table, publish=publish)
    # started and ended at the same timestamp never share a dedup identity
    assert published[2][1]["event_id"] != started_id

    recording = {"account_id": "account-1", "object": {
        "id": 123, "uuid": "meeting-uuid", "topic": "Course",
        "recording_files": [{"id": "video", "file_type": "MP4",
                             "play_url": "https://zoom.test/play"}]}}
    done_body, done_headers = signed({"event": "recording.completed", "event_ts": 7000,
                                      "payload": recording})
    vtt_body, vtt_headers = signed({"event": "recording.transcript_completed",
                                    "event_ts": 7000, "payload": recording})
    zoom_webhooks.handle("zoom", done_headers, done_body,
                         connections_table=table, publish=publish)
    zoom_webhooks.handle("zoom", vtt_headers, vtt_body,
                         connections_table=table, publish=publish)
    # completed and transcript_completed at the same timestamp never share one either
    assert published[3][0][2]["video_files"] == published[4][0][2]["video_files"]
    assert published[4][1]["event_id"] != published[3][1]["event_id"]
    assert len({entry[1]["event_id"] for entry in published}) == 4


def test_zoom_registration_created_publishes_the_registrant(monkeypatch):
    table, _ = setup(monkeypatch)
    body, headers = signed({
        "event": "meeting.registration_created", "event_ts": 8000,
        "payload": {"account_id": "account-1", "object": {
            "id": 123, "uuid": "meeting-uuid", "topic": "Course",
            "start_time": "2026-09-28T09:00:00Z", "timezone": "Europe/Berlin",
            "settings": {"approval_type": 2},
            "registrant": {"id": "registrant-1", "email": "ada@example.test",
                           "first_name": "Ada", "last_name": "Lovelace",
                           "status": "approved"}}}})
    published = []
    publish = lambda *args, **kwargs: published.append((args, kwargs))
    assert zoom_webhooks.handle("zoom", headers, body, connections_table=table,
                                publish=publish) == (200, {"accepted": True})
    assert published[0][0][:2] == ("zoom", "meeting.registration_created")
    data = published[0][0][2]
    assert data == {"connection_id": "zoom", "account_id": "account-1",
                    "meeting_id": 123, "meeting_uuid": "meeting-uuid",
                    "topic": "Course", "start_time": "2026-09-28T09:00:00Z",
                    "timezone": "Europe/Berlin", "registrant_id": "registrant-1",
                    "email": "ada@example.test", "first_name": "Ada",
                    "last_name": "Lovelace", "status": "approved"}
    # meeting-wide settings and the registrant's join credential stay out
    dumped = json.dumps(data)
    assert "approval_type" not in dumped and "join_url" not in dumped
    assert "object" not in dumped
    # a retry of the same registration delivery keeps one dedup identity
    assert zoom_webhooks.handle("zoom", headers, body, connections_table=table,
                                publish=publish) == (200, {"accepted": True})
    assert published[1][1]["event_id"] == published[0][1]["event_id"]


def test_zoom_registration_rejects_incomplete_deliveries(monkeypatch):
    table, _ = setup(monkeypatch)
    registrant = {"id": "registrant-1", "email": "ada@example.test",
                  "first_name": "Ada", "last_name": "Lovelace", "status": "approved"}
    meeting_object = {"id": 123, "uuid": "meeting-uuid", "topic": "Course",
                      "registrant": registrant}
    incomplete = [
        {"event": "meeting.registration_created",
         "payload": {"account_id": "account-1", "object": {"id": 123, "uuid": "u"}}},
        {"event": "meeting.registration_created", "event_ts": 9000,
         "payload": {"object": meeting_object}},
        {"event": "meeting.registration_created", "event_ts": 9001,
         "payload": {"account_id": "account-1",
                     "object": {**meeting_object,
                                "registrant": {**registrant, "id": None}}}},
        {"event": "meeting.registration_created", "event_ts": 9002,
         "payload": {"account_id": "account-1"}},
    ]
    published = []
    for message in incomplete:
        body, headers = signed(message)
        assert zoom_webhooks.handle("zoom", headers, body, connections_table=table,
                                    publish=lambda *a, **k: published.append((a, k))) == \
            (400, {"error": "incomplete Zoom registration event"}), message
    assert published == []


def test_cli_zoom_import_uses_agent_connection_api(monkeypatch, tmp_path, capsys):
    token_file = tmp_path / "zoom-secret"
    token_file.write_text("zoom-secret-123456\n")
    calls = []
    monkeypatch.setattr(commands.api, "call", lambda *args, **kwargs: (
        calls.append((args, kwargs)) or {"connection_id": "zoom"}))
    assert commands.connections_import(
        "https://dapier.example.test", "zoom", "zoom", None, None, None,
        token_path=str(token_file)) == 0
    assert calls[0][0][1:3] == ("POST", "/api/agent/connections/import")
    assert calls[0][0][3]["token"] == "zoom-secret-123456"
    assert "/hooks/zoom/zoom" in capsys.readouterr().out


def test_console_and_agent_apis_share_zoom_setup(monkeypatch):
    table, secrets = setup(monkeypatch)
    monkeypatch.setenv("CONNECTIONS_TABLE", "connections")
    monkeypatch.setattr(admin_routes.boto3, "resource", lambda _: type(
        "Resource", (), {"Table": lambda self, name: table})())
    monkeypatch.setattr(admin_routes.session, "_session_subject", lambda _: "operator")
    monkeypatch.setattr(admin_routes.session, "_audit_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(importing.audit, "emit", lambda *args, **kwargs: None)
    event = {"body": json.dumps({"connection_id": "zoom", "provider": "zoom",
                                 "display_name": "Course recordings"})}
    response = admin_routes.save_connection(event)
    assert response["statusCode"] == 200
    assert secrets["oauth#zoom"]["webhook_secret"] == "zoom-secret-123456"
    assert "zoom-secret" not in response["body"]
    status, payload = importing.import_core(
        {"connection_id": "zoom-2", "provider": "zoom", "token": "another-secret-123456"},
        operator_subject="operator", connections_table=table)
    assert status == 200
    assert payload["status"] == "ready"
    assert secrets["oauth#zoom-2"] == {"webhook_secret": "another-secret-123456"}


def test_zoom_hook_route_dispatches_signed_challenge(monkeypatch):
    table, _ = setup(monkeypatch)
    monkeypatch.setenv("CONNECTIONS_TABLE", "connections")
    monkeypatch.setattr(ingress.boto3, "resource", lambda _: type(
        "Resource", (), {"Table": lambda self, name: table})())
    body, headers = signed({"event": "endpoint.url_validation", "payload": {"plainToken": "challenge"}})
    response = ingress.handler({
        "requestContext": {"http": {"method": "POST", "path": "/hooks/zoom/zoom"}},
        "headers": headers, "body": body.decode(),
    }, None)
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["plainToken"] == "challenge"
    assert table.items["zoom"]["status"] == "connected"
