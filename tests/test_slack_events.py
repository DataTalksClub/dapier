"""Slack Events trigger: signing-secret setup, intake, sample, surfaces."""

import hashlib
import hmac
import json
import time

from src.dapier.connections import importing
from src.dapier.connections import records as connections
from src.dapier.triggers.intake import slack_events
from src.dapier.api.admin import routes as admin_routes
from src.dapier.api import router as ingress
from src.dapier.connectors import trigger_discovery
from dapier_cli import commands


class Table:
    def __init__(self):
        self.items = {}

    def get_item(self, Key):
        return {"Item": self.items[Key["connection_id"]]} if Key["connection_id"] in self.items else {}

    def put_item(self, Item):
        self.items[Item["connection_id"]] = dict(Item)


def signed(message, secret="faslack-signing-secret-000", timestamp=None):
    body = json.dumps(message, separators=(",", ":")).encode()
    stamp = str(timestamp or int(time.time()))
    digest = hmac.new(secret.encode(), b"v0:" + stamp.encode() + b":" + body,
                      hashlib.sha256).hexdigest()
    return body, {"X-Slack-Request-Timestamp": stamp, "X-Slack-Signature": f"v0={digest}"}


def setup_connection(table, connection_id="slack-conn", **extra):
    item = {
        "connection_id": connection_id,
        "provider": "slack",
        "credential_id": connections.credential_id_for(connection_id),
        "status": connections.STATUS_CONNECTED,
        "verified_account_id": "TWORKSPACE",
        "version": 1,
        **extra,
    }
    table.items[connection_id] = item
    return item


def slack_secrets(monkeypatch, stored):
    monkeypatch.setattr(slack_events.credentials, "get_credential", lambda key: stored[key])
    monkeypatch.setattr(importing.credentials, "get_credential", lambda key: stored[key])
    monkeypatch.setattr(importing.credentials, "put_credential",
                        lambda key, value, **_: stored.__setitem__(key, value))
    return stored


def import_slack(body, table, monkeypatch, stored):
    slack_secrets(monkeypatch, stored)
    monkeypatch.setattr(importing.slack_tokens, "validate_token", lambda token: token)
    monkeypatch.setattr(importing.slack_tokens, "verify_account",
                        lambda token: ("TWORKSPACE", "DataTalks Slack"))
    monkeypatch.setattr(importing.audit, "emit", lambda *args, **kwargs: None)
    return importing.import_core(body, operator_subject="operator", connections_table=table)


def test_import_stores_token_and_signing_secret_together(monkeypatch):
    table, stored = Table(), {}
    status, payload = import_slack(
        {"connection_id": "slack-conn", "provider": "slack", "token": "xoxb-" + "t" * 20,
         "signing_secret": "faslack-signing-secret-000"}, table, monkeypatch, stored)
    assert status == 200
    assert stored["oauth#slack-conn"] == {"token": "xoxb-" + "t" * 20,
                                          "signing_secret": "faslack-signing-secret-000"}
    assert "faslack" not in str(payload)
    # A token-only edit keeps the stored signing secret; a new one rotates it.
    status, _ = import_slack({"connection_id": "slack-conn", "provider": "slack",
                              "token": "xoxb-" + "n" * 20}, table, monkeypatch, stored)
    assert status == 200
    assert stored["oauth#slack-conn"]["signing_secret"] == "faslack-signing-secret-000"
    status, _ = import_slack({"connection_id": "slack-conn", "provider": "slack",
                              "token": "xoxb-" + "n" * 20,
                              "signing_secret": "frotated-signing-secret-x"}, table, monkeypatch, stored)
    assert status == 200
    assert stored["oauth#slack-conn"]["signing_secret"] == "frotated-signing-secret-x"
    # Telegram saves never carry a signing secret; garbage lengths fail closed.
    status, payload = import_slack({"connection_id": "slack-conn", "provider": "slack",
                                    "token": "xoxb-" + "n" * 20, "signing_secret": "short"},
                                   table, monkeypatch, stored)
    assert status == 400 and "16-256" in payload["error"]


def test_signature_and_challenge_handshake(monkeypatch):
    table, stored = Table(), {}
    setup_connection(table)
    slack_secrets(monkeypatch, stored)
    stored["oauth#slack-conn"] = {"token": "xoxb-" + "t" * 20,
                                  "signing_secret": "faslack-signing-secret-000"}
    publish = lambda *args, **kwargs: None
    body, headers = signed({"type": "url_verification", "challenge": "chALLENGe-123"})
    assert slack_events.handle("slack-conn", {}, body, connections_table=table,
                               publish=publish)[0] == 401
    assert slack_events.handle("slack-conn", headers, body, connections_table=table,
                               publish=publish, now=time.time() + 600)[0] == 401
    assert slack_events.handle("slack-conn", headers, body, connections_table=table,
                               publish=publish) == (200, {"challenge": "chALLENGe-123"})
    # No stored signing secret: the hook fails closed with a setup hint.
    stored["oauth#slack-conn"] = {"token": "xoxb-" + "t" * 20}
    assert slack_events.handle("slack-conn", headers, body, connections_table=table,
                               publish=publish) == (
        503, {"error": "Slack Events signing secret is not configured"})
    assert slack_events.handle("nope", headers, body, connections_table=table,
                               publish=publish)[0] == 404


def test_message_events_publish_delivery_shaped_envelope(monkeypatch):
    table, stored = Table(), {}
    setup_connection(table)
    slack_secrets(monkeypatch, stored)
    stored["oauth#slack-conn"] = {"token": "xoxb-" + "t" * 20,
                                  "signing_secret": "faslack-signing-secret-000"}
    published = []
    payload = {"type": "event_callback", "team_id": "TWORKSPACE", "event_id": "Ev123",
               "event_time": 1758900000,
               "event": {"type": "message.channels", "channel": "C01BQC114P2",
                         "user": "U02PFU1LS", "text": "ship it", "ts": "1758900012.000300"}}
    body, headers = signed(payload)
    publish = lambda *args, **kwargs: published.append((args, kwargs))
    status, answer = slack_events.handle("slack-conn", headers, body, connections_table=table,
                                         publish=publish)
    assert status == 200 and answer == {"accepted": True}
    assert published[0][0][:2] == ("slack", "message.received")
    assert published[0][1]["source"] == "slack-conn"
    data = published[0][0][2]
    assert data["channel_id"] == "C01BQC114P2" and data["user_id"] == "U02PFU1LS"
    assert data["text"] == "ship it" and data["ts"] == "1758900012.000300"
    assert data["event_id"] == "Ev123" and data["event"] == payload["event"]
    # Slack retries on timeout: the same delivery publishes a stable event id.
    slack_events.handle("slack-conn", headers, body, connections_table=table, publish=publish)
    assert published[0][1]["event_id"] == published[1][1]["event_id"]
    expected = "slack:" + hashlib.sha256(b"slack-conn:Ev123").hexdigest()[:32]
    assert published[0][1]["event_id"] == expected


def test_bot_posts_and_unsubscribed_events_never_publish(monkeypatch):
    table, stored = Table(), {}
    setup_connection(table)
    slack_secrets(monkeypatch, stored)
    stored["oauth#slack-conn"] = {"token": "xoxb-" + "t" * 20,
                                  "signing_secret": "faslack-signing-secret-000"}
    published = []
    publish = lambda *args, **kwargs: published.append(args)
    bot, bot_headers = signed({"type": "event_callback", "team_id": "TWORKSPACE", "event_id": "EvB", "event": {
        "type": "message.channels", "channel": "C1", "bot_id": "B0T", "text": "loop?"}})
    assert slack_events.handle("slack-conn", bot_headers, bot, connections_table=table,
                               publish=publish)[1] == {"accepted": False}
    other, other_headers = signed({"type": "event_callback", "team_id": "TWORKSPACE", "event_id": "EvR", "event": {
        "type": "reaction_added", "channel": "C1", "user": "U1", "reaction": "tada"}})
    assert slack_events.handle("slack-conn", other_headers, other, connections_table=table,
                               publish=publish)[1] == {"accepted": False}
    assert published == []
    # app_mention is in the message family and publishes.
    mention, mention_headers = signed({"type": "event_callback", "team_id": "TWORKSPACE", "event_id": "EvM", "event": {
        "type": "app_mention", "channel": "C1", "user": "U1", "text": "<@U0> hi"}})
    assert slack_events.handle("slack-conn", mention_headers, mention, connections_table=table,
                               publish=publish)[1] == {"accepted": True}
    assert len(published) == 1


def test_workspace_binding_is_enforced(monkeypatch):
    table, stored = Table(), {}
    setup_connection(table, expected_account_id="TOTHER")
    slack_secrets(monkeypatch, stored)
    stored["oauth#slack-conn"] = {"token": "xoxb-" + "t" * 20,
                                  "signing_secret": "faslack-signing-secret-000"}
    body, headers = signed({"type": "event_callback", "team_id": "TWORKSPACE", "event_id": "Ev1",
                            "event": {"type": "message", "channel": "C1", "text": "hi"}})
    assert slack_events.handle("slack-conn", headers, body, connections_table=table,
                               publish=lambda *a, **k: None) == (
        403, {"error": "Slack workspace does not match connection"})


def test_console_and_agent_apis_share_signing_secret(monkeypatch):
    table, stored = Table(), {}
    slack_secrets(monkeypatch, stored)
    monkeypatch.setenv("CONNECTIONS_TABLE", "connections")
    monkeypatch.setattr(admin_routes.boto3, "resource", lambda _: type(
        "Resource", (), {"Table": lambda self, name: table})())
    monkeypatch.setattr(admin_routes.session, "_session_subject", lambda _: "operator")
    monkeypatch.setattr(admin_routes.session, "_audit_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(admin_routes.importing.slack_tokens, "validate_token", lambda token: token)
    monkeypatch.setattr(admin_routes.importing.slack_tokens, "verify_account",
                        lambda token: ("TWORKSPACE", "DataTalks Slack"))
    event = {"body": json.dumps({"connection_id": "slack-conn", "provider": "slack",
                                 "token": "xoxb-" + "t" * 20,
                                 "signing_secret": "faslack-signing-secret-000"})}
    response = admin_routes.save_connection(event)
    assert response["statusCode"] == 200
    assert stored["oauth#slack-conn"]["signing_secret"] == "faslack-signing-secret-000"
    assert "faslack" not in response["body"]
    status, payload = import_slack(
        {"connection_id": "slack-2", "provider": "slack", "token": "xoxb-" + "z" * 20,
         "signing_secret": "fagent-side-signing-secret"}, table, monkeypatch, stored)
    assert status == 200
    assert stored["oauth#slack-2"]["signing_secret"] == "fagent-side-signing-secret"


def test_slack_hook_route_dispatches_signed_challenge(monkeypatch):
    table, stored = Table(), {}
    setup_connection(table)
    slack_secrets(monkeypatch, stored)
    stored["oauth#slack-conn"] = {"token": "xoxb-" + "t" * 20,
                                  "signing_secret": "faslack-signing-secret-000"}
    monkeypatch.setenv("CONNECTIONS_TABLE", "connections")
    monkeypatch.setattr(ingress.boto3, "resource", lambda _: type(
        "Resource", (), {"Table": lambda self, name: table})())
    body, headers = signed({"type": "url_verification", "challenge": "chALLENGe-123"})
    response = ingress.handler({
        "requestContext": {"http": {"method": "POST", "path": "/hooks/slack/slack-conn"}},
        "headers": headers, "body": body.decode(),
    }, None)
    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"challenge": "chALLENGe-123"}


def test_cli_slack_import_passes_signing_secret_and_prints_hook_url(monkeypatch, tmp_path, capsys):
    token_file = tmp_path / "slack-token"
    token_file.write_text("xoxb-" + "t" * 20 + "\n")
    secret_file = tmp_path / "slack-signing"
    secret_file.write_text("faslack-signing-secret-000\n")
    calls = []
    monkeypatch.setattr(commands.api, "call", lambda *args, **kwargs: (
        calls.append((args, kwargs)) or {"connection_id": "slack-conn"}))
    assert commands.connections_import(
        "https://dapier.example.test", "slack-conn", "slack", None, None, None,
        token_path=str(token_file), signing_secret_path=str(secret_file)) == 0
    assert calls[0][0][1:3] == ("POST", "/api/agent/connections/import")
    assert calls[0][0][3]["signing_secret"] == "faslack-signing-secret-000"
    assert "/hooks/slack/slack-conn" in capsys.readouterr().out


def test_slack_trigger_sample_is_delivery_shaped(monkeypatch):
    connection = {"connection_id": "slack-conn", "provider": "slack",
                  "status": connections.STATUS_CONNECTED, "credential_id": "oauth#slack-conn"}
    monkeypatch.setattr(trigger_discovery, "connected_connection", lambda *a, **k: connection)
    from src.dapier.connectors import slack as slack_connector

    def fake_discover(_connection, resource, params, *, transport=None):
        if resource == "channels":
            return [{"id": "C01BQC114P2", "name": "general", "type": "channel"}]
        assert params["channel"] == "C01BQC114P2"
        return [{"id": "1758900012.000300", "name": "ship it", "user": "U02PFU1LS",
                 "ts": "1758900012.000300"}]

    monkeypatch.setattr(slack_connector.provider, "discover", fake_discover)
    status, payload = trigger_discovery.api_discover({"connector": "slack"})
    assert status == 200
    assert payload["source"] == "live"
    sample = payload["sample"]
    assert sample["connector"] == "slack" and sample["event"] == "message.received"
    assert sample["source"] == "slack-conn"
    assert sample["data"]["channel_id"] == "C01BQC114P2"
    assert sample["data"]["user_id"] == "U02PFU1LS" and sample["data"]["text"] == "ship it"
    assert sample["data"]["event"]["type"] == "message"


def test_slack_trigger_sample_falls_back_to_synthetic(monkeypatch):
    def missing(*args, **kwargs):
        raise trigger_discovery.DiscoveryNotFound("no connection")

    monkeypatch.setattr(trigger_discovery, "connected_connection", missing)
    monkeypatch.setattr(trigger_discovery, "history_sample", lambda connector: None)
    status, payload = trigger_discovery.api_discover({"connector": "slack"})
    assert status == 200
    assert payload["source"] == "synthetic"
    data = payload["sample"]["data"]
    assert payload["sample"]["event"] == "message.received"
    assert data["channel_id"] and data["text"] and data["event"]["type"] == "message"
