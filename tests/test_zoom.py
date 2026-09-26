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
