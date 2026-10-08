"""Hook dedupe: dedupe_path validation, stable delivery ids, and the
router's claim-skip-release loop for webhook and Telegram triggers."""
import json

import pytest

from src.dapier.api import router as ingress
from src.dapier.triggers import hook_triggers


def _hook_stub(hook_id="orders", kind="webhook", token="tok-123", enabled=True,
               dedupe_path=None):
    item = {"hook_id": hook_id, "kind": kind, "url": f"u-{hook_id}", "token": token,
            "actions": [], "enabled": enabled,
            "dedupe_path": dedupe_path if dedupe_path is not None else ""}
    return type("T", (), {
        "scan": lambda self, Limit=200: {"Items": [dict(item)]},
        "get_item": lambda self, Key: {"Item": dict(item)} if Key["hook_id"] == hook_id else {},
        "put_item": lambda self, Item: None,
        "delete_item": lambda self, Key: None,
    })()


def _post(path, body, headers=None, query=None):
    return ingress.handler({
        "requestContext": {"http": {"method": "POST", "path": path}},
        "headers": headers or {},
        "queryStringParameters": query,
        "body": body.decode(),
    }, None)


@pytest.fixture
def hooks(monkeypatch):
    """Router environment: published envelopes, seen-store calls spied, a
    swappable hook row. Tests point ``state["claim"]`` at their behavior."""
    state = {"claim": lambda scope, key: True, "claim_calls": [],
             "forget_calls": [], "published": [], "stub": {}}

    def claim(scope, key, **kwargs):
        state["claim_calls"].append((scope, key))
        return state["claim"](scope, key)

    def forget(scope, key, **kwargs):
        state["forget_calls"].append((scope, key))

    def publish(connector, event_type, data, source=None, event_id=None, request=None):
        if state.get("fail_publish"):
            raise RuntimeError("queue down")
        state["published"].append({"connector": connector, "event": event_type,
                                   "data": data, "source": source, "id": event_id})

    def hook_table(*args, **kwargs):
        return _hook_stub(**state["stub"])

    monkeypatch.setenv("EVENT_QUEUE_URL", "https://sqs.example.test/events")
    monkeypatch.setenv("HOOK_TRIGGERS_TABLE", "hooks")
    monkeypatch.setattr("src.dapier.triggers.seen.claim", claim)
    monkeypatch.setattr("src.dapier.triggers.seen.forget", forget)
    monkeypatch.setattr(ingress, "_publish", publish)
    monkeypatch.setattr("src.dapier.triggers.hook_triggers.get_table", hook_table)
    return state


def test_dedupe_path_validation():
    assert hook_triggers.validate_dedupe_path("data.delivery_id") == "data.delivery_id"
    assert hook_triggers.validate_dedupe_path(None) == ""
    assert hook_triggers.validate_dedupe_path("") == ""
    with pytest.raises(hook_triggers.TriggerError):
        hook_triggers.validate_dedupe_path(123)
    with pytest.raises(hook_triggers.TriggerError):
        hook_triggers.validate_dedupe_path("a..b")


def test_dedupe_value_and_event_id():
    payload = {"body": {"delivery": {"id": "d-9"}}}
    assert hook_triggers.dedupe_value(payload, "body.delivery.id") == "d-9"
    assert hook_triggers.dedupe_value(payload, "body.missing") is None
    first = hook_triggers.dedupe_event_id("orders", "d-9")
    assert first == hook_triggers.dedupe_event_id("orders", "d-9")
    assert first != hook_triggers.dedupe_event_id("pings", "d-9")


def test_webhook_with_dedupe_path_claims_a_stable_id(hooks):
    hooks["stub"] = {"dedupe_path": "delivery_id"}
    response = _post("/hooks/webhook/orders",
                     json.dumps({"delivery_id": "d-9"}).encode(),
                     {"authorization": "Bearer tok-123",
                      "content-type": "application/json"})

    expected = hook_triggers.dedupe_event_id("orders", "d-9")
    assert response["statusCode"] == 202
    assert hooks["claim_calls"] == [("hook#orders", expected)]
    assert hooks["published"][0]["id"] == expected
    assert hooks["published"][0]["data"]["body"] == {"delivery_id": "d-9"}


def test_webhook_duplicate_delivery_is_acked_without_publishing(hooks):
    hooks["stub"] = {"dedupe_path": "delivery_id"}
    hooks["claim"] = lambda scope, key: False
    body = json.dumps({"delivery_id": "d-9"}).encode()
    headers = {"authorization": "Bearer tok-123", "content-type": "application/json"}

    response = _post("/hooks/webhook/orders", body, headers)

    assert response["statusCode"] == 202
    assert json.loads(response["body"]) == {
        "accepted": True, "duplicate": True,
        "event_id": hook_triggers.dedupe_event_id("orders", "d-9")}
    assert hooks["published"] == []


def test_webhook_retry_after_a_claim_is_released_publishes_again(hooks):
    """The provider's retry after a failed publish must deliver: the claim
    was undone, so the same delivery claims fresh and publishes."""
    hooks["stub"] = {"dedupe_path": "delivery_id"}
    hooks["fail_publish"] = True
    body = json.dumps({"delivery_id": "d-9"}).encode()
    headers = {"authorization": "Bearer tok-123", "content-type": "application/json"}

    with pytest.raises(RuntimeError):
        _post("/hooks/webhook/orders", body, headers)

    assert hooks["forget_calls"] == [("hook#orders",
                                      hook_triggers.dedupe_event_id("orders", "d-9"))]
    assert hooks["published"] == []


def test_webhook_without_dedupe_path_keeps_fresh_uuids(hooks):
    response = _post("/hooks/webhook/orders",
                     json.dumps({"delivery_id": "d-9"}).encode(),
                     {"authorization": "Bearer tok-123",
                      "content-type": "application/json"})

    assert response["statusCode"] == 202
    assert hooks["claim_calls"] == []
    assert hooks["published"][0]["id"] is None  # _publish mints the uuid


def test_webhook_payload_without_the_dedupe_value_is_never_deduped(hooks):
    hooks["stub"] = {"dedupe_path": "delivery_id"}
    response = _post("/hooks/webhook/orders",
                     json.dumps({"other": "field"}).encode(),
                     {"authorization": "Bearer tok-123",
                      "content-type": "application/json"})

    assert response["statusCode"] == 202
    assert hooks["claim_calls"] == []
    assert len(hooks["published"]) == 1


def test_store_outage_still_publishes(hooks, monkeypatch):
    """A seen-store outage must never drop a delivery; the stable id still
    dedupes downstream through run grouping and step leases."""
    hooks["stub"] = {"dedupe_path": "delivery_id"}

    def store_down(scope, key, **kwargs):
        raise ConnectionError("cursors table unreachable")

    monkeypatch.setattr("src.dapier.triggers.seen.claim", store_down)

    response = _post("/hooks/webhook/orders",
                     json.dumps({"delivery_id": "d-9"}).encode(),
                     {"authorization": "Bearer tok-123",
                      "content-type": "application/json"})

    assert response["statusCode"] == 202
    assert hooks["published"][0]["id"] == hook_triggers.dedupe_event_id("orders", "d-9")


def test_telegram_defaults_to_update_id_dedupe(hooks):
    hooks["stub"] = {"kind": "telegram"}
    update = json.dumps({"update_id": 42, "message": {"text": "hi"}}).encode()
    headers = {"x-telegram-bot-api-secret-token": "tok-123"}

    response = _post("/hooks/telegram/orders", update, headers)

    expected = hook_triggers.dedupe_event_id("orders", 42)
    assert response["statusCode"] == 200
    assert hooks["claim_calls"] == [("hook#orders", expected)]
    assert hooks["published"][0]["id"] == expected


def test_telegram_duplicate_update_is_acked_without_publishing(hooks):
    hooks["stub"] = {"kind": "telegram"}
    hooks["claim"] = lambda scope, key: False
    update = json.dumps({"update_id": 42, "message": {"text": "hi"}}).encode()

    response = _post("/hooks/telegram/orders", update,
                     {"x-telegram-bot-api-secret-token": "tok-123"})

    assert response["statusCode"] == 200
    assert json.loads(response["body"])["duplicate"] is True
    assert hooks["published"] == []


def test_telegram_explicit_dedupe_path_wins_over_the_default(hooks):
    hooks["stub"] = {"kind": "telegram", "dedupe_path": "message.message_id"}
    update = json.dumps({"update_id": 42,
                         "message": {"message_id": 7, "text": "hi"}}).encode()

    _post("/hooks/telegram/orders", update,
          {"x-telegram-bot-api-secret-token": "tok-123"})

    assert hooks["claim_calls"] == [
        ("hook#orders", hook_triggers.dedupe_event_id("orders", 7))]


def test_telegram_update_without_update_id_is_never_deduped(hooks):
    hooks["stub"] = {"kind": "telegram"}
    response = _post("/hooks/telegram/orders",
                     json.dumps({"message": {"text": "no id"}}).encode(),
                     {"x-telegram-bot-api-secret-token": "tok-123"})

    assert response["statusCode"] == 200
    assert hooks["claim_calls"] == []
    assert hooks["published"][0]["id"] is None


def test_json_then_form_retry_shares_identity_and_preserves_fields(hooks):
    hooks["stub"] = {"dedupe_path": "request_id"}
    claimed = set()
    def claim(scope, key):
        fresh = key not in claimed
        claimed.add(key)
        return fresh
    hooks["claim"] = claim
    payload = {"request_id": "same-1", "Date": "2026-10-02", "Text": "Task + café", "Notes": ""}
    from urllib.parse import urlencode
    for content_type, body in [
        ("application/json", json.dumps(payload).encode()),
        ("application/x-www-form-urlencoded; charset=UTF-8", urlencode(payload).encode()),
    ]:
        response = _post("/hooks/webhook/orders", body,
                         {"authorization": "Bearer tok-123", "content-type": content_type})
        assert response["statusCode"] == 202
    assert json.loads(response["body"])["duplicate"] is True
    assert len(hooks["published"]) == 1
    assert hooks["published"][0]["data"]["body"] == payload


def test_form_fields_keep_case_blank_and_repeated_values():
    assert ingress._webhook_payload(b"Date=2026&Text=a%2Bb&Notes=&tag=x&tag=y", "application/x-www-form-urlencoded") == {
        "Date": "2026", "Text": "a+b", "Notes": "", "tag": ["x", "y"]}
    assert ingress._webhook_payload(b"Date=2026", "text/plain") == {"raw": "Date=2026"}
