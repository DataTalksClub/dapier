"""Mailchimp webhook intake: form parse, ping, per-type publish, routing.

The published data shape must equal the mailchimp trigger sample's
(connectors.mailchimp), so a pulled sample is what a real delivery carries.
"""
import json
from urllib.parse import urlencode

import pytest

from src.dapier.api import router as ingress
from src.dapier.triggers import hook_triggers
from src.dapier.triggers.intake import mailchimp_webhooks


def subscribe_body():
    return urlencode({"type": "subscribe", "data[list_id]": "abc123",
                      "data[email]": "reader@example.test",
                      "data[merges][FNAME]": "Reader"}).encode()


def test_subscribe_publishes_the_documented_sample_shape():
    published = []
    status, payload = mailchimp_webhooks.handle(
        subscribe_body(), hook="orders",
        publish=lambda *args, **kwargs: published.append((args, kwargs)))
    assert status == 200
    assert payload == {"accepted": True}
    (connector, event, data), kwargs = published[0]
    assert (connector, event) == ("mailchimp", "subscribe")
    assert data == {"hook": "orders", "type": "subscribe", "data": {
        "list_id": "abc123", "email": "reader@example.test",
        "merges": {"FNAME": "Reader"}}}
    assert kwargs["source"] == "abc123"


def test_each_declared_type_publishes_its_event():
    for kind in mailchimp_webhooks.EVENT_TYPES:
        published = []
        body = urlencode({"type": kind, "data[list_id]": "abc123"}).encode()
        status, _ = mailchimp_webhooks.handle(
            body, hook="orders",
            publish=lambda *args, **kwargs: published.append(args))
        assert status == 200
        assert published == [("mailchimp", kind,
                              {"hook": "orders", "type": kind, "data": {"list_id": "abc123"}})]


def test_ping_answers_without_publishing():
    published = []
    status, payload = mailchimp_webhooks.handle(
        b"type=ping", publish=lambda *args, **kwargs: published.append(args))
    assert status == 200
    assert payload == {"accepted": True, "ping": True}
    assert published == []


def test_unknown_and_missing_types_are_400():
    published = []
    publish = lambda *args, **kwargs: published.append(args)
    status, payload = mailchimp_webhooks.handle(b"type=forward", publish=publish)
    assert status == 400
    assert "forward" in payload["error"] and "subscribe" in payload["error"]
    status, _ = mailchimp_webhooks.handle(b"data[list_id]=abc", publish=publish)
    assert status == 400
    status, _ = mailchimp_webhooks.handle(b"", publish=publish)
    assert status == 400
    assert published == []


# --- router: the stored hook trigger gates the URL -------------------------------


def _post(path, body):
    return ingress.handler({
        "requestContext": {"http": {"method": "POST", "path": path}},
        "headers": {},
        "queryStringParameters": None,
        "body": body.decode(),
    }, None)


def test_router_gates_on_the_stored_mailchimp_hook(monkeypatch):
    state = {"published": [], "hook": {"hook_id": "mc", "kind": "mailchimp",
                                       "enabled": True}}
    monkeypatch.setenv("EVENT_QUEUE_URL", "https://sqs.example.test/events")
    monkeypatch.setenv("HOOK_TRIGGERS_TABLE", "hooks")
    monkeypatch.setattr(ingress, "_publish",
                        lambda *args, **kwargs: state["published"].append(args))

    def hook_table(*args, **kwargs):
        return type("T", (), {
            "get_item": lambda self, Key: (
                {"Item": dict(state["hook"])} if Key["hook_id"] == "mc" else {}),
        })()

    monkeypatch.setattr("src.dapier.triggers.hook_triggers.get_table", hook_table)

    assert _post("/hooks/mailchimp/mc", subscribe_body())["statusCode"] == 200
    assert state["published"][-1][:2] == ("mailchimp", "subscribe")
    # the URL's hook name rides in the published data, so the trigger's
    # workflow filter can scope deliveries to this hook alone
    assert state["published"][-1][2]["hook"] == "mc"

    # no such hook, a webhook-kind hook, or a disabled one: 404, no publish
    assert _post("/hooks/mailchimp/nope", subscribe_body())["statusCode"] == 404
    state["hook"]["kind"] = "webhook"
    assert _post("/hooks/mailchimp/mc", subscribe_body())["statusCode"] == 404
    state["hook"] = {"hook_id": "mc", "kind": "mailchimp", "enabled": False}
    assert _post("/hooks/mailchimp/mc", subscribe_body())["statusCode"] == 404
    assert len(state["published"]) == 1

    # ping answers 200 without publishing
    state["hook"] = {"hook_id": "mc", "kind": "mailchimp", "enabled": True}
    assert _post("/hooks/mailchimp/mc", b"type=ping")["statusCode"] == 200
    assert len(state["published"]) == 1


# --- router: delivery dedupe (the webhook kind's claim-skip-release) --------------


def _dedupe_state(monkeypatch, hook):
    """Router environment with the seen store spied; ``hook`` is the stored
    trigger row the delivery resolves to."""
    state = {"claims": [], "forgets": [], "published": [], "claim_result": True,
             "fail_publish": False}

    def claim(scope, key, **kwargs):
        state["claims"].append((scope, key))
        return state["claim_result"]

    def forget(scope, key, **kwargs):
        state["forgets"].append((scope, key))

    def publish(connector, event_type, data, source=None, event_id=None):
        if state["fail_publish"]:
            raise RuntimeError("queue down")
        state["published"].append({"connector": connector, "event": event_type,
                                   "data": data, "source": source, "id": event_id})

    monkeypatch.setenv("EVENT_QUEUE_URL", "https://sqs.example.test/events")
    monkeypatch.setenv("HOOK_TRIGGERS_TABLE", "hooks")
    monkeypatch.setattr("src.dapier.triggers.seen.claim", claim)
    monkeypatch.setattr("src.dapier.triggers.seen.forget", forget)
    monkeypatch.setattr(ingress, "_publish", publish)
    monkeypatch.setattr("src.dapier.triggers.hook_triggers.get_table",
                        lambda *args, **kwargs: type("T", (), {
                            "get_item": lambda self, Key: (
                                {"Item": dict(hook)}
                                if Key["hook_id"] == hook["hook_id"] else {}),
                        })())
    return state


def _fired_body():
    """One subscribe delivery: the top-level ``fired_at`` a real Mailchimp
    POST carries — the natural dedupe_path value."""
    return urlencode({"type": "subscribe", "fired_at": "2026-09-28T10:00:00+00:00",
                      "data[list_id]": "abc123",
                      "data[email]": "reader@example.test"}).encode()


def test_mailchimp_delivery_dedupes_on_the_configured_path(monkeypatch):
    hook = {"hook_id": "mc", "kind": "mailchimp", "enabled": True,
            "dedupe_path": "fired_at"}
    state = _dedupe_state(monkeypatch, hook)
    expected = hook_triggers.dedupe_event_id("mc", "2026-09-28T10:00:00+00:00")

    response = _post("/hooks/mailchimp/mc", _fired_body())

    assert response["statusCode"] == 200
    assert state["claims"] == [("hook#mc", expected)]
    assert state["published"][0]["id"] == expected
    assert state["published"][0]["data"]["hook"] == "mc"

    # a Mailchimp retry of the same delivery answers 202 without re-publishing
    state["claim_result"] = False
    response = _post("/hooks/mailchimp/mc", _fired_body())
    assert response["statusCode"] == 202
    assert json.loads(response["body"]) == {
        "accepted": True, "duplicate": True, "event_id": expected}
    assert len(state["published"]) == 1


def test_mailchimp_retry_after_failed_publish_is_released(monkeypatch):
    """The claim is undone when the publish fails, so Mailchimp's retry of
    the same delivery publishes instead of being answered as a duplicate."""
    hook = {"hook_id": "mc", "kind": "mailchimp", "enabled": True,
            "dedupe_path": "fired_at"}
    state = _dedupe_state(monkeypatch, hook)
    state["fail_publish"] = True

    with pytest.raises(RuntimeError):
        _post("/hooks/mailchimp/mc", _fired_body())

    assert state["forgets"] == [("hook#mc",
                                 hook_triggers.dedupe_event_id("mc", "2026-09-28T10:00:00+00:00"))]
    assert state["published"] == []


def test_mailchimp_without_dedupe_path_never_claims(monkeypatch):
    hook = {"hook_id": "mc", "kind": "mailchimp", "enabled": True}
    state = _dedupe_state(monkeypatch, hook)

    response = _post("/hooks/mailchimp/mc", _fired_body())

    assert response["statusCode"] == 200
    assert state["claims"] == []
    assert len(state["published"]) == 1
    assert state["published"][0]["id"] is None  # _publish mints the uuid
