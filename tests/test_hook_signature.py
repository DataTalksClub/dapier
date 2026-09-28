"""Optional shared-secret HMAC check on webhook hook triggers: save-time
option handling, the intake's signature gate over the raw body, and the
bearer-token regression when no secret is set."""
import hashlib
import hmac
import json

import pytest

from src.dapier.api import router as ingress
from src.dapier.triggers import hook_triggers

SECRET = "whsec-provider-shared-9f2"
BODY = b'{"id": "d-1", "ok": true}'


def _row(hook_id="orders", kind="webhook", token="tok-123", secret=None,
         enabled=True):
    item = {"hook_id": hook_id, "kind": kind, "url": f"u-{hook_id}",
            "token": token, "actions": [], "enabled": enabled, "dedupe_path": ""}
    if secret is not None:
        item["secret"] = secret
    return item


class StubTable:
    """One stored hook row; get_item reflects put_item writes."""

    def __init__(self, items=None):
        self.items = {item["hook_id"]: dict(item) for item in (items or [])}

    def scan(self, Limit=200):
        return {"Items": [dict(value) for value in self.items.values()]}

    def get_item(self, Key):
        item = self.items.get(Key["hook_id"])
        return {"Item": dict(item)} if item else {}

    def put_item(self, Item):
        self.items[Item["hook_id"]] = dict(Item)

    def delete_item(self, Key):
        self.items.pop(Key["hook_id"], None)


def _post(body, headers=None, path="/hooks/webhook/orders"):
    response = ingress.handler({
        "requestContext": {"http": {"method": "POST", "path": path}},
        "headers": headers or {},
        "queryStringParameters": None,
        "body": body.decode(),
    }, None)
    return response["statusCode"], json.loads(response["body"])


def _sig(secret, body):
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _auth_headers(body, secret=SECRET, bearer=None):
    headers = {"content-type": "application/json",
               "x-dapier-signature": f"sha256={_sig(secret, body)}"}
    if bearer:
        headers["authorization"] = f"Bearer {bearer}"
    return headers


@pytest.fixture
def env(monkeypatch):
    """Router environment: a swappable hook row and a spied queue publish.
    Tests point ``state["stub"]`` at their hook row kwargs; nothing queues
    unless the intake really publishes."""
    state = {"stub": {}, "published": []}

    def publish(connector, event_type, data, source=None, event_id=None):
        state["published"].append({"connector": connector, "event": event_type,
                                   "data": data, "source": source, "id": event_id})

    def hook_table(*args, **kwargs):
        return StubTable([_row(**state["stub"])])

    monkeypatch.setenv("EVENT_QUEUE_URL", "https://sqs.example.test/events")
    monkeypatch.setenv("HOOK_TRIGGERS_TABLE", "hooks")
    monkeypatch.setattr(ingress, "_publish", publish)
    monkeypatch.setattr("src.dapier.triggers.hook_triggers.get_table", hook_table)
    return state


# --- save-time option handling (shared by admin and agent routes) ---

def _stored(stub, name="orders"):
    return stub.items[name]


def test_save_stores_the_secret_and_public_view_shows_signed_only():
    stub = StubTable()
    status, payload = hook_triggers.api_save(
        {"name": "orders", "secret": SECRET,
         "actions": [{"type": "webhook", "url": "https://hooks.test/x"}]},
        "op", kind="webhook", table_ref=stub)
    assert status == 200
    assert payload["signed"] is True
    assert payload["signature_header"] == "x-dapier-signature"
    assert SECRET not in json.dumps(payload)
    assert _stored(stub)["secret"] == SECRET


def test_save_without_secret_stays_bearer_only():
    status, payload = hook_triggers.api_save(
        {"name": "orders",
         "actions": [{"type": "webhook", "url": "https://hooks.test/x"}]},
        "op", kind="webhook", table_ref=StubTable())
    assert status == 200
    assert "signed" not in payload
    assert payload["header"] == "authorization"


def test_edit_keeps_the_secret_unless_cleared():
    stub = StubTable()
    hook_triggers.api_save(
        {"name": "orders", "secret": SECRET,
         "actions": [{"type": "webhook", "url": "https://hooks.test/x"}]},
        "op", kind="webhook", table_ref=stub)
    # an edit that omits secret keeps the lock (the binding survives, like
    # a telegram trigger's connection_id)
    _status, kept = hook_triggers.api_save(
        {"name": "orders", "description": "later",
         "actions": [{"type": "webhook", "url": "https://hooks.test/x"}]},
        "op", kind="webhook", table_ref=stub)
    assert kept["signed"] is True
    # an explicit empty value clears it
    _status, cleared = hook_triggers.api_save(
        {"name": "orders", "secret": "",
         "actions": [{"type": "webhook", "url": "https://hooks.test/x"}]},
        "op", kind="webhook", table_ref=stub)
    assert "signed" not in cleared
    assert _stored(stub)["secret"] == ""


def test_secret_is_webhook_only_and_must_be_a_string():
    actions = [{"type": "webhook", "url": "https://hooks.test/x"}]
    # a non-empty secret on a kind that never verifies signatures is
    # rejected, not silently ignored
    with pytest.raises(hook_triggers.TriggerError):
        hook_triggers.build_item(
            {"name": "orders", "connection_id": "tg-bot", "secret": SECRET,
             "actions": [{"type": "telegram_send", "connection_id": "tg-bot"}]},
            "op", "telegram")
    with pytest.raises(hook_triggers.TriggerError):
        hook_triggers.build_item({"name": "orders", "secret": 12345, "actions": actions},
                                 "op", "webhook")
    # webhook accepts (and strips) one; other kinds keep an empty one
    item, _created = hook_triggers.build_item(
        {"name": "orders", "secret": f"  {SECRET}  ", "actions": actions},
        "op", "webhook")
    assert item["secret"] == SECRET
    item, _created = hook_triggers.build_item(
        {"name": "orders", "connection_id": "tg-bot",
         "actions": [{"type": "telegram_send", "connection_id": "tg-bot"}]},
        "op", "telegram")
    assert item["secret"] == ""


# --- the intake's signature gate ---

def test_valid_signature_publishes_exactly_once(env):
    env["stub"] = {"secret": SECRET}
    status, payload = _post(BODY, _auth_headers(BODY))
    assert status == 202
    assert payload == {"accepted": True}
    assert len(env["published"]) == 1
    published = env["published"][0]
    assert published["event"] == "request.received"
    assert published["data"]["hook"] == "orders"
    assert published["data"]["body"] == {"id": "d-1", "ok": True}


def test_bare_hex_signature_is_accepted(env):
    env["stub"] = {"secret": SECRET}
    status, _payload = _post(BODY, {"x-dapier-signature": _sig(SECRET, BODY)})
    assert status == 202
    assert len(env["published"]) == 1


def test_signature_is_over_the_raw_body_bytes_exactly(env):
    env["stub"] = {"secret": SECRET}
    raw = b'{"id": "d-1",  "uneven": [1, 2] }  '
    status, _payload = _post(raw, _auth_headers(raw))
    assert status == 202
    # the same bytes re-serialized differently do not verify
    env["published"].clear()
    status, _payload = _post(raw, _auth_headers(b'{"uneven": [1,2], "id": "d-1"}'))
    assert status == 401
    assert env["published"] == []


def test_missing_header_is_401_and_nothing_is_queued(env):
    env["stub"] = {"secret": SECRET}
    status, payload = _post(BODY, {"authorization": "Bearer tok-123",
                                   "content-type": "application/json"})
    assert status == 401
    assert payload == {"error": "invalid signature"}
    assert env["published"] == []


def test_wrong_signature_is_401_and_nothing_is_queued(env):
    env["stub"] = {"secret": SECRET}
    status, payload = _post(BODY, _auth_headers(BODY, secret="whsec-wrong"))
    assert status == 401
    assert payload == {"error": "invalid signature"}
    assert env["published"] == []


def test_bearer_token_alone_no_longer_admits_a_delivery(env):
    env["stub"] = {"secret": SECRET}
    status, payload = _post(BODY, {"authorization": "Bearer tok-123"})
    assert status == 401
    assert payload == {"error": "invalid signature"}
    assert env["published"] == []


def test_wrong_bearer_with_valid_signature_still_publishes(env):
    # the secret locks the URL: the signature alone is sufficient, the
    # bearer token is not even consulted
    env["stub"] = {"secret": SECRET}
    status, _payload = _post(BODY, _auth_headers(BODY, bearer="tok-wrong"))
    assert status == 202
    assert len(env["published"]) == 1


# --- regression: no secret behaves byte-for-byte like today ---

def test_without_a_secret_the_bearer_token_gates_as_before(env):
    env["stub"] = {"secret": ""}
    status, payload = _post(BODY, {"authorization": "Bearer tok-123"})
    assert status == 202
    assert payload == {"accepted": True}
    assert len(env["published"]) == 1


def test_without_a_secret_a_signature_header_is_ignored(env):
    env["stub"] = {"secret": ""}
    status, _payload = _post(BODY, {"authorization": "Bearer tok-123",
                                    "x-dapier-signature": "sha256=deadbeef"})
    assert status == 202
    assert len(env["published"]) == 1


def test_without_a_secret_a_wrong_bearer_is_still_401(env):
    env["stub"] = {"secret": ""}
    status, payload = _post(BODY, {"authorization": "Bearer nope"})
    assert status == 401
    assert payload == {"error": "invalid token"}
    assert env["published"] == []


# --- verify_signature unit contract ---

def test_verify_signature_accepts_prefix_and_bare_hex():
    digest = _sig(SECRET, BODY)
    assert hook_triggers.verify_signature(SECRET, BODY, f"sha256={digest}")
    assert hook_triggers.verify_signature(SECRET, BODY, f"SHA256={digest}")
    assert hook_triggers.verify_signature(SECRET, BODY, digest)
    assert not hook_triggers.verify_signature(SECRET, BODY, "")
    assert not hook_triggers.verify_signature(SECRET, BODY, None)
    assert not hook_triggers.verify_signature(SECRET, BODY, "sha256=nothex")
    assert not hook_triggers.verify_signature(SECRET, BODY, _sig("other", BODY))
    assert not hook_triggers.verify_signature(SECRET, BODY + b" ", digest)


def test_signature_for_matches_stdlib_reference():
    expected = hmac.new(SECRET.encode(), BODY, hashlib.sha256).hexdigest()
    assert hook_triggers.signature_for(SECRET, BODY) == expected
    assert hook_triggers.verify_signature(SECRET, BODY, f"sha256={expected}")
