"""Trigger sample pull: the domain dispatch, both API surfaces, ownership.

Zapier's 'pull in sample data' for every trigger connector: live where the
connector can fetch, else the newest recorded run, else a documented
example. The CLI (`dapier triggers sample`) reaches it over
/api/agent/discover and the designer's test panel over /api/admin/discover;
both share connectors.trigger_discovery.api_discover, so a pulled sample is
exactly what a real delivery publishes. Ownership: samples for connectors
with a module live there (email.py, telegram.py, schedule.py, poll.py, ...);
triggers.py carries only the chips without one.
"""
import json
import time

import pytest

from src.dapier.api import admin
from src.dapier.api import agent as agent_api
from src.dapier.auth import session
from src.dapier.connectors import trigger_discovery

import src.dapier.connectors  # noqa: F401  (import = registration)


@pytest.fixture(autouse=True)
def no_recorded_history(monkeypatch):
    """Pin the fallback chain to synthetic: history depends on run tables."""
    monkeypatch.setattr(
        trigger_discovery, "history_sample",
        lambda connector, event=None: None)


# --- domain: the catalog and the synthetic samples ---


def test_every_trigger_chip_has_a_sample():
    cat = trigger_discovery.trigger_discovery_catalog()
    for connector in ("email", "webhook", "telegram", "dropbox", "dataops",
                      "renderer", "schedule", "youtube", "zoom", "poll", "custom",
                      "slack", "mailchimp"):
        assert connector in cat["sample"], connector
    for connector in ("dropbox", "s3", "slack", "telegram", "zoom",
                      "google-sheets", "google-drive", "mailchimp", "youtube"):
        assert connector in cat["options"], connector


def test_every_sample_connector_is_a_palette_chip():
    """A connector the palette can't offer is undiscoverable in the designer.

    Every entry of the discovery-sample catalog must have a trigger chip in
    GET /api/catalog, and each chip's declared events must include the event
    its sample publishes (telegram's hook delivers message.received).
    """
    from src.dapier.connectors import registry

    cat = trigger_discovery.trigger_discovery_catalog()
    chips = {entry["name"]: entry for entry in registry.catalog()["connectors"]}
    # dataops is action-side intake, not a user-selectable source; the
    # webhook ingress kind surfaces in the palette as the Custom chip.
    internal = {"dataops"}
    aliases = {"webhook": "custom"}
    for connector in cat["sample"]:
        chip = aliases.get(connector, connector)
        if chip not in internal:
            assert chip in chips, connector
    assert chips["telegram"]["events"] == ["message.received", "channel_post.received",
                                           "callback_query.received"]
    assert chips["telegram"]["label"] == "Telegram"
    assert chips["custom"]["events"] == []


def _sample(connector, **kwargs):
    status, payload = trigger_discovery.api_discover(
        {"connector": connector, **kwargs})
    assert status == 200, payload
    return payload


def test_schedule_sample_names_the_schedule_from_event():
    payload = _sample("schedule", event="weekdaily-digest")
    assert payload["source"] == "synthetic"
    assert payload["sample"]["event"] == "schedule.triggered"
    assert payload["sample"]["data"]["schedule"] == "weekdaily-digest"
    assert payload["sample"]["data"]["utc_time"]


def test_renderer_sample_carries_the_renderer_contract():
    data = _sample("renderer")["sample"]["data"]
    assert _sample("renderer")["sample"]["event"] == "job.completed"
    assert data["job_id"] and data["content_type"] == "application/pdf"
    assert data["output"]["bucket"] and data["output"]["key"]


def test_email_sample_publishes_the_decoded_body():
    """New workflows template against what a delivery carries: the decoded
    text/html bodies the intake publishes, not just headers and pointers."""
    sample = _sample("email")["sample"]
    assert sample["event"] == "message.received"
    assert sample["data"]["text"]
    assert sample["data"]["html"]


def test_youtube_sample_carries_the_pubsub_entry():
    sample = _sample("youtube")["sample"]
    assert sample["event"] == "video.published"
    assert sample["data"]["video_id"]
    assert sample["data"]["url"].startswith("https://www.youtube.com/watch?v=")


def test_mailchimp_sample_picks_the_webhook_type_payload():
    sample = _sample("mailchimp", event="upemail")["sample"]
    assert sample["event"] == "upemail"
    assert sample["data"]["type"] == "upemail"
    assert sample["data"]["data"]["new_email"]
    # an unknown type falls back to the default payload (subscribe)
    fallback = _sample("mailchimp", event="bogus")["sample"]
    assert fallback["event"] == "subscribe"
    assert fallback["data"]["data"]["email"]


def test_zoom_sample_is_metadata_only():
    data = _sample("zoom")["sample"]["data"]
    assert data["topic"] and data["video_files"][0]["play_url"]
    dumped = json.dumps(data)
    assert "download_token" not in dumped and "object" not in dumped


def test_custom_sample_is_a_placeholder_to_paste_over():
    sample = _sample("custom")["sample"]
    assert sample["event"] == "received"
    assert "Replace this object" in json.dumps(sample["data"])


def _hook_stub(hook_id="orders", kind="webhook", token="tok-123", enabled=True):
    item = {"hook_id": hook_id, "kind": kind, "url": f"u-{hook_id}", "token": token,
            "actions": [], "enabled": enabled}
    return type("T", (), {
        "scan": lambda self, Limit=200: {"Items": [dict(item)]},
        "get_item": lambda self, Key: {"Item": dict(item)} if Key["hook_id"] == hook_id else {},
        "put_item": lambda self, Item: None,
        "delete_item": lambda self, Key: None,
    })()


def _post(path, body=b'{"hello":"world"}', headers=None):
    from src.dapier.api import router as ingress

    return ingress.handler({
        "requestContext": {"http": {"method": "POST", "path": path}},
        "headers": headers or {},
        "body": body.decode(),
    }, None)


def test_custom_chip_samples_name_events_that_actually_fire(monkeypatch):
    """The Custom chip's samples must name the events its fire paths publish.

    A filter copied from a pulled sample goes into a stored workflow verbatim,
    and matching is exact on connector+event — a sample naming an event no
    fire path publishes builds filters that never match. Both backings of the
    chip are covered: the custom ingress (/hooks/custom/{source}) and the
    webhook trigger kind (/hooks/webhook/{name}, aliased to the chip).
    """
    from src.dapier.api import router as ingress

    sent = []
    monkeypatch.setenv("EVENT_QUEUE_URL", "https://sqs.example.test/events")
    monkeypatch.setattr(ingress.queue, "send_message", lambda **kwargs: sent.append(kwargs))

    # Fire path 1: the custom ingress publishes custom/received.
    response = _post("/hooks/custom/checkout", body=b'{"ok": true}')
    assert response["statusCode"] == 202
    fired = json.loads(sent[-1]["MessageBody"])
    custom_sample = _sample("custom")["sample"]
    assert custom_sample["connector"] == fired["connector"]
    assert custom_sample["event"] == fired["event"]

    # Fire path 2: the webhook trigger kind publishes webhook/request.received.
    monkeypatch.setenv("HOOK_TRIGGERS_TABLE", "hooks")
    monkeypatch.setattr(
        "src.dapier.triggers.hook_triggers.get_table",
        lambda *a, **k: _hook_stub())
    response = _post("/hooks/webhook/orders",
                     headers={"authorization": "Bearer tok-123"})
    assert response["statusCode"] == 202
    fired = json.loads(sent[-1]["MessageBody"])
    webhook_sample = _sample("webhook")["sample"]
    assert webhook_sample["connector"] == fired["connector"]
    assert webhook_sample["event"] == fired["event"]


def test_history_sample_wins_over_synthetic(monkeypatch):
    # the event kwarg: email's sample is per-event, so history is queried
    # with the wanted event (bounce.received history never answers a
    # message.received ask — see test_history_sample... breadth coverage)
    monkeypatch.setattr(trigger_discovery, "history_sample", lambda connector, event=None: {
        "connector": "email", "event": "message.received",
        "data": {"subject": "Real one"}, "id": "run-1",
        "source": "todo@dtcdev.click", "occurred_at": "2026-09-26T03:00:00Z"})
    payload = _sample("email")
    assert payload["source"] == "history"
    assert payload["sample"]["data"]["subject"] == "Real one"


def test_unknown_connector_is_404_naming_the_discoverable_ones():
    status, payload = trigger_discovery.api_discover({"connector": "bogus"})
    assert status == 404
    assert "unknown discovery connector 'bogus'" in payload["error"]
    assert "email" in payload["error"]


# --- domain: telegram pulls a live update, else the standard chain ----------


def test_telegram_sample_without_a_connection_synthesizes(monkeypatch):
    """No connected bot: the documented example, shaped exactly like a real
    delivery (hook_triggers.update_data) — never a bare 404."""
    monkeypatch.delenv("CONNECTIONS_TABLE", raising=False)
    payload = _sample("telegram")
    assert payload["source"] == "synthetic"
    assert payload["sample"]["event"] == "message.received"
    data = payload["sample"]["data"]
    assert data["hook"] == "discover"
    assert data["text"] and data["chat_id"]
    assert data["update"]["message"]["text"] == data["text"]


def test_telegram_sample_falls_back_when_getUpdates_is_rejected(monkeypatch):
    """A webhook-mode bot (the normal dapier telegram state) rejects
    getUpdates with Telegram's conflict message; the chain still answers
    with a sample instead of 502ing."""
    from src.dapier.connections.providers import telegram_api

    def conflict(method, name, params, transport=None):
        raise telegram_api.TelegramApiError(
            "Conflict: terminated by other getUpdates request")

    monkeypatch.setattr(telegram_api, "call", conflict)
    monkeypatch.setattr(
        "src.dapier.connections.credentials.get_credential",
        lambda credential_id: {"token": "12:ABC"})
    configure_connections(monkeypatch, {
        "tg": {"connection_id": "tg", "provider": "telegram", "status": "connected",
               "credential_id": "oauth#tg"}})
    payload = _sample("telegram")
    assert payload["source"] == "synthetic"
    assert payload["sample"]["event"] == "message.received"


def test_telegram_sample_picks_a_live_callback_query_per_event(monkeypatch):
    """A pending button tap is served live under its own event name — and
    named callback_query.received even in the un-asked pull, never misfiled
    as a message."""
    from src.dapier.connections.providers import telegram_api

    pending = [
        {"update_id": 90127, "callback_query": {
            "id": "cq1", "from": {"id": 9, "first_name": "Ada", "username": "ada"},
            "message": {"message_id": 27, "text": "Pick a cohort:",
                        "chat": {"id": 555, "type": "private"}},
            "chat_instance": "-9923423423", "data": "join:september"}},
        {"update_id": 90125, "message": {
            "message_id": 7, "text": "Hello dapier",
            "chat": {"id": 555, "type": "private"},
            "from": {"id": 9, "first_name": "Ada"}}},
    ]

    def updates(method, name, params, transport=None):
        return pending

    monkeypatch.setattr(telegram_api, "call", updates)
    monkeypatch.setattr(
        "src.dapier.connections.credentials.get_credential",
        lambda credential_id: {"token": "12:ABC"})
    configure_connections(monkeypatch, {
        "tg": {"connection_id": "tg", "provider": "telegram", "status": "connected",
               "credential_id": "oauth#tg"}})
    asked = _sample("telegram", event="callback_query.received")
    assert asked["source"] == "live"
    assert asked["sample"]["event"] == "callback_query.received"
    assert asked["sample"]["data"]["data"] == "join:september"
    # the un-asked pull takes the first pending update and names it truly
    unasked = _sample("telegram")
    assert unasked["source"] == "live"
    assert unasked["sample"]["event"] == "callback_query.received"
    assert unasked["sample"]["data"]["id"] == "cq1"


# --- domain: poll pulls a live list item ---


def test_poll_without_a_name_and_without_history_synthesizes():
    """The chip-level ask follows the standard chain: nothing live (no name
    selects a trigger) and nothing recorded lands on the documented example,
    like every other chip — never a bare 404."""
    payload = _sample("poll")
    assert payload["source"] == "synthetic"
    assert payload["sample"]["event"] == "item.new"
    assert payload["sample"]["data"]["poll"] == "discover"
    assert payload["sample"]["data"]["item_id"]


def test_poll_sample_for_an_unknown_name_is_404():
    """A named ask that names nothing stays a precise 404 — a fabricated
    sample would silently hide the typo."""
    status, payload = trigger_discovery.api_discover(
        {"connector": "poll", "event": "blog"})
    assert status == 404
    assert "no stored poll trigger named 'blog'" in payload["error"]


def test_poll_with_a_name_and_an_empty_listing_falls_through(monkeypatch):
    from src.dapier.triggers import poll_triggers

    monkeypatch.setattr(poll_triggers, "get_item",
                        lambda name, table_ref=None: {"poll_id": name})
    monkeypatch.setattr(poll_triggers, "fetch_page",
                        lambda item, cursor=None, transport=None: [])
    payload = _sample("poll", event="blog")
    assert payload["source"] == "synthetic"
    assert payload["sample"]["data"]["poll"] == "blog"


def test_poll_pulls_the_first_listed_item_live(monkeypatch):
    from src.dapier.triggers import poll_triggers

    monkeypatch.setattr(poll_triggers, "get_item",
                        lambda name, table_ref=None: {"poll_id": name})
    monkeypatch.setattr(poll_triggers, "fetch_page",
                        lambda item, cursor=None, transport=None:
                        [{"id": 7, "title": "New post"}])
    monkeypatch.setattr(
        poll_triggers, "event_for",
        lambda item, raw: {"connector": "poll", "event": "item.new",
                           "source": item["poll_id"],
                           "data": {"title": raw["title"],
                                    "poll": item["poll_id"],
                                    "item_id": str(raw["id"])}})
    payload = _sample("poll", event="blog")
    assert payload["source"] == "live"
    assert payload["sample"]["event"] == "item.new"
    assert payload["sample"]["data"]["title"] == "New post"


def test_poll_fetch_failure_is_a_502(monkeypatch):
    from src.dapier.triggers import poll_triggers

    monkeypatch.setattr(poll_triggers, "get_item",
                        lambda name, table_ref=None: {"poll_id": name})
    monkeypatch.setattr(poll_triggers, "fetch_page",
                        lambda item, cursor=None, transport=None:
                        (_ for _ in ()).throw(RuntimeError("connection refused")))
    status, payload = trigger_discovery.api_discover(
        {"connector": "poll", "event": "blog"})
    assert status == 502
    assert "fetch failed" in payload["error"]


# --- domain: options wrappers reach the registry listings ------------------------


class FakeTransport:
    """Route provider calls by URL substring to canned JSON responses."""

    def __init__(self, *routes):
        self.routes = routes  # (substring, status, payload) tuples
        self.calls = []

    def __call__(self, method, url, *, headers=None, body=None, timeout=15):
        self.calls.append({"method": method, "url": url, "body": body})
        for substring, status, payload in self.routes:
            if substring in url:
                return status, json.dumps(payload).encode()
        raise AssertionError(f"unexpected provider call: {method} {url}")


def configure_connections(monkeypatch, connections):
    """A fake connections table, so options wrappers resolve their account."""
    tables = {"connections": Table(connections)}

    class Dynamo:
        def Table(self, name):
            return tables[name]

    import boto3

    monkeypatch.setenv("CONNECTIONS_TABLE", "connections")
    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())


def test_options_wrappers_pull_live_listings(monkeypatch):
    from src.dapier.connections import discovery as provider
    from src.dapier.connections.providers import telegram_api

    transport = FakeTransport(
        ("drive/v3/files", 200, {"files": [{"id": "f1", "name": "Report"}]}),
        ("users/me/meetings", 200, {"meetings": [
            {"id": 999, "topic": "Standup", "join_url": "https://zoom.us/j/999"}]}),
        ("getUpdates", 200, {"ok": True, "result": [
            {"update_id": 5, "message": {"chat": {"id": -7, "title": "Bots",
                                                  "type": "group"}}}]}),
    )
    monkeypatch.setattr(provider, "_default_transport", transport)
    monkeypatch.setattr(provider.tokens, "get_access_token",
                        lambda connection, transport=None: ("tok", {}))
    monkeypatch.setattr(provider.credentials, "get_credential",
                        lambda credential_id: {"token": "12:ABC"})
    monkeypatch.setattr(telegram_api, "_default_transport", transport)
    configure_connections(monkeypatch, {
        "g": {"connection_id": "g", "provider": "google", "status": "connected",
              "credential_id": "oauth#g"},
        "z": {"connection_id": "z", "provider": "zoom", "status": "connected",
              "credential_id": "oauth#z"},
        "tg": {"connection_id": "tg", "provider": "telegram", "status": "connected",
               "credential_id": "oauth#tg"},
    })

    for connector, resource, connection_id, options in (
            ("google-sheets", "google-sheets.spreadsheets", "g",
             [{"value": "f1", "label": "Report"}]),
            ("google-drive", "google-drive.files", "g",
             [{"value": "f1", "label": "Report"}]),
            ("zoom", "zoom.meetings", "z",
             [{"value": "999", "label": "Standup"}]),
            ("telegram", "telegram.chats", "tg",
             [{"value": "-7", "label": "Bots"}])):
        status, payload = trigger_discovery.api_discover(
            {"connector": connector, "kind": "options", "resource": resource,
             "connection_id": connection_id})
        assert status == 200, payload
        assert payload["connector"] == connector
        assert payload["resource"] == resource
        assert payload["options"] == options
        assert payload["connection_id"] == connection_id


def test_s3_object_options_need_a_bucket(monkeypatch):
    configure_connections(monkeypatch, {
        "aws-1": {"connection_id": "aws-1", "provider": "s3", "status": "connected"}})
    status, payload = trigger_discovery.api_discover(
        {"connector": "s3", "kind": "options", "resource": "s3.objects",
         "connection_id": "aws-1"})
    assert status == 404
    assert "bucket" in payload["error"]


def test_s3_object_options_pass_the_bucket_through(monkeypatch):
    import boto3

    class FakeS3:
        def __init__(self):
            self.calls = []

        def list_objects_v2(self, **kwargs):
            self.calls.append(kwargs)
            return {"Contents": [{"Key": "reports/2026/report.pdf", "Size": 9}]}

    fake = FakeS3()
    monkeypatch.setattr(
        "src.dapier.connections.credentials.get_credential",
        lambda credential_id: {"access_key_id": "AKIAX", "secret_access_key": "b" * 40})
    monkeypatch.setattr(boto3, "client", lambda service, **kwargs: fake)
    configure_connections(monkeypatch, {
        "aws-1": {"connection_id": "aws-1", "provider": "s3", "status": "connected"}})
    status, payload = trigger_discovery.api_discover(
        {"connector": "s3", "kind": "options", "resource": "s3.objects",
         "event": "backups", "connection_id": "aws-1"})
    assert status == 200, payload
    assert payload["options"] == [
        {"value": "reports/2026/report.pdf", "label": "reports/2026/report.pdf"}]
    assert fake.calls == [{"Bucket": "backups", "MaxKeys": 100}]


def test_mailchimp_audience_options_fall_back_to_the_shared_credential(monkeypatch):
    """mailchimp is a key credential with often no connection record: the
    pseudo-account fallback still lists audiences from the stored key."""
    import plugins.mailchimp.plugin as mailchimp_plugin

    monkeypatch.setattr(
        "src.dapier.connections.credentials.get_credential",
        lambda credential_id: {"apiKey": "key-us21", "server": "us21"})
    monkeypatch.setattr(
        mailchimp_plugin, "mailchimp_request",
        lambda method, url, api_key, transport=None: (
            200, {"lists": [{"id": "abc123", "name": "Digest",
                             "stats": {"member_count": 42}}]}))
    configure_connections(monkeypatch, {})
    status, payload = trigger_discovery.api_discover(
        {"connector": "mailchimp", "kind": "options",
         "resource": "mailchimp.audiences"})
    assert status == 200, payload
    assert payload["options"] == [{"value": "abc123", "label": "Digest (42 members)"}]


# --- agent API: the CLI's surface ---


class Table:
    def __init__(self, items=None):
        self.items = items or {}

    def get_item(self, **kwargs):
        item = self.items.get(kwargs["Key"].get("connection_id"))
        return {"Item": dict(item)} if item else {}

    def put_item(self, **kwargs):
        self.items[kwargs["Item"]["connection_id"]] = kwargs["Item"]

    def scan(self, **kwargs):
        return {"Items": list(self.items.values())}


def configure_agent(monkeypatch, claims=None):
    agent_api.reset_rate_limits()
    tables = {"connections": Table(), "grants": Table(),
              "credentials": Table(), "api-tokens": Table()}

    class Dynamo:
        def Table(self, name):
            return tables[name]

    import boto3

    monkeypatch.setenv("CONNECTIONS_TABLE", "connections")
    monkeypatch.setenv("GRANTS_TABLE", "grants")
    monkeypatch.setenv("CREDENTIALS_TABLE", "credentials")
    monkeypatch.setenv("API_TOKENS_TABLE", "api-tokens")
    monkeypatch.setenv("AUTH_CLI_CLIENT_ID", "cli-client")
    monkeypatch.setenv("OPERATOR_EMAILS", "op@datatalks.club")
    monkeypatch.delenv("AUDIT_TABLE", raising=False)
    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())
    monkeypatch.setattr(
        agent_api, "verify_id_token",
        lambda token, audience=None: dict(claims) if claims is not None else (_ for _ in ()).throw(ValueError("bad")))
    return tables


def agent_event(body=None, token="dtc-id-token"):
    headers = {"host": "dapier.example.test"}
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    request = {"headers": headers, "cookies": []}
    if body is not None:
        request["body"] = json.dumps(body)
    return request


def test_agent_sample_requires_an_operator(monkeypatch):
    configure_agent(monkeypatch, claims={"sub": "subject-1", "email": "op@datatalks.club"})
    monkeypatch.setenv("OPERATOR_EMAILS", "boss@example.test")
    response = agent_api.route(
        agent_event({"connector": "schedule"}), "POST", "/api/agent/discover")
    assert response["statusCode"] == 403


def test_agent_sample_pulls_a_schedule_envelope(monkeypatch):
    configure_agent(monkeypatch, claims={"sub": "subject-1", "email": "op@datatalks.club"})
    response = agent_api.route(
        agent_event({"connector": "schedule"}), "POST", "/api/agent/discover")
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["connector"] == "schedule"
    assert body["sample"]["event"] == "schedule.triggered"
    assert body["source"] == "synthetic"


def test_agent_sample_unknown_connector_is_404(monkeypatch):
    configure_agent(monkeypatch, claims={"sub": "subject-1", "email": "op@datatalks.club"})
    response = agent_api.route(
        agent_event({"connector": "bogus"}), "POST", "/api/agent/discover")
    assert response["statusCode"] == 404


# --- admin API: the console's surface ---


def configure_admin(monkeypatch):
    tables = {"connections": Table(), "credentials": Table()}

    class Dynamo:
        def Table(self, name):
            return tables[name]

    import boto3

    monkeypatch.setenv("CONNECTIONS_TABLE", "connections")
    monkeypatch.setenv("CREDENTIALS_TABLE", "credentials")
    monkeypatch.delenv("AUDIT_TABLE", raising=False)
    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())
    monkeypatch.setattr(session, "_credentials",
                        lambda: {"username": "admin", "password": "pw"})
    cookie = session._sign({"sub": "op@datatalks.club", "subject": "op-sub",
                            "exp": int(time.time()) + 600})
    return [f"dapier_session={cookie}"]


def admin_request(method, path, body=None, cookies=None):
    event = {
        "requestContext": {"http": {"method": method, "path": path}},
        "headers": {"host": "dapier.example.test",
                    "origin": "https://dapier.example.test"},
        "cookies": cookies or [],
    }
    if body is not None:
        event["body"] = json.dumps(body)
    return event


def test_admin_sample_pull(monkeypatch):
    cookies = configure_admin(monkeypatch)
    response = admin.route(
        admin_request("POST", "/api/admin/discover",
                      body={"connector": "renderer"}, cookies=cookies),
        "POST", "/api/admin/discover")
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["connector"] == "renderer"
    assert body["sample"]["event"] == "job.completed"


def test_admin_sample_requires_signin(monkeypatch):
    configure_admin(monkeypatch)
    response = admin.route(
        admin_request("POST", "/api/admin/discover", body={"connector": "renderer"}),
        "POST", "/api/admin/discover")
    assert response["statusCode"] == 401
