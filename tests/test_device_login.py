"""Device pairing: storage logic and the /api/agent/device/* endpoints."""

import json
import time

import pytest

from src.dapier.api import agent as agent_api
from src.dapier.auth import device_sessions


class FakeTable:
    """pk-keyed table honoring the conditions device_sessions relies on."""

    def __init__(self):
        self.items = {}

    def put_item(self, **kwargs):
        item = kwargs["Item"]
        self.items[item["pk"]] = dict(item)

    def get_item(self, **kwargs):
        item = self.items.get(kwargs["Key"]["pk"])
        return {"Item": dict(item)} if item else {}

    def update_item(self, **kwargs):
        item = self.items.get(kwargs["Key"]["pk"])
        condition = kwargs.get("ConditionExpression") or ""
        if "attribute_exists(pk)" in condition and item is None:
            raise KeyError(kwargs["Key"]["pk"])
        if ":pending" in condition and (item is None or item.get("status") != "pending"):
            raise KeyError(kwargs["Key"]["pk"])
        if "attribute_not_exists(rotated_at)" in condition and item is not None \
                and item.get("rotated_at"):
            raise KeyError(kwargs["Key"]["pk"])
        values = kwargs.get("ExpressionAttributeValues") or {}
        if ":approved" in values:
            item["status"] = values[":approved"]
            item["session"] = values[":session"]
        if ":grace" in values:
            item["ttl"] = values[":grace"]
        if ":now" in values:
            item["rotated_at"] = values[":now"]
        return {}

    def delete_item(self, **kwargs):
        self.items.pop(kwargs["Key"]["pk"], None)


@pytest.fixture(autouse=True)
def clean_rate_limits():
    agent_api.reset_rate_limits()
    yield
    agent_api.reset_rate_limits()


@pytest.fixture
def store(monkeypatch):
    fake = FakeTable()
    monkeypatch.setattr(device_sessions, "table", lambda: fake)
    return fake


def _device_event(path, body=None, source_ip="10.0.0.1"):
    return {
        "requestContext": {"http": {"method": "POST", "path": path, "sourceIp": source_ip}},
        "body": json.dumps(body or {}),
    }


def test_pairing_lifecycle_end_to_end(store):
    device_code, view = device_sessions.start(table_ref=store)

    assert view["user_code"].count("-") == 1
    left, right = view["user_code"].split("-")
    assert len(left) == len(right) == 4
    assert device_code.startswith(device_sessions.PREFIX)
    assert device_sessions.poll(device_code, table_ref=store) == {"status": "pending"}

    assert device_sessions.approve(
        view["user_code"].lower(), "Google_1", "op@datatalks.club", table_ref=store,
    ) == "ok"

    result = device_sessions.poll(device_code, table_ref=store)
    assert result["status"] == "approved"
    assert result["token"].startswith(device_sessions.PREFIX)
    assert result["subject"] == "Google_1"
    assert result["email"] == "op@datatalks.club"
    assert result["expires_at"] > time.time()
    # Single delivery: the pairing is gone after the first successful poll.
    assert device_sessions.poll(device_code, table_ref=store) == {"status": "expired"}

    session = device_sessions.resolve(result["token"], table_ref=store)
    assert session == {"subject": "Google_1", "email": "op@datatalks.club"}


def test_approve_rejects_unknown_and_expired_codes(store):
    device_code, view = device_sessions.start(table_ref=store)

    assert device_sessions.approve("ZZZZ-ZZZZ", "Google_1", "op@x", table_ref=store) == "invalid"
    assert device_sessions.approve("short", "Google_1", "op@x", table_ref=store) == "invalid"

    pairing_pk = f"pairing#{device_sessions._hash(device_code)}"
    store.items[pairing_pk]["ttl"] = int(time.time()) - 1
    assert device_sessions.approve(view["user_code"], "Google_1", "op@x", table_ref=store) == "invalid"


def test_approve_wins_exactly_once(store):
    device_code, view = device_sessions.start(table_ref=store)

    assert device_sessions.approve(view["user_code"], "Google_1", "op@x", table_ref=store) == "ok"
    assert device_sessions.approve(view["user_code"], "Google_2", "op2@x", table_ref=store) == "invalid"


def test_resolve_rejects_unknown_and_expired_sessions(store):
    token, _ = device_sessions.create_session("Google_1", "op@x", table_ref=store)
    assert device_sessions.resolve(token, table_ref=store)["subject"] == "Google_1"
    assert device_sessions.resolve("dapd_unknown", table_ref=store) is None
    assert device_sessions.resolve("dap_", table_ref=store) is None

    session_pk = f"session#{device_sessions._hash(token)}"
    store.items[session_pk]["ttl"] = int(time.time()) - 1
    assert device_sessions.resolve(token, table_ref=store) is None


def test_refresh_rotates_and_blocks_replay(store):
    token, _ = device_sessions.create_session("Google_1", "op@x", table_ref=store)

    rotated = device_sessions.refresh(token, table_ref=store)
    assert rotated and rotated[0].startswith(device_sessions.PREFIX)
    assert device_sessions.resolve(rotated[0], table_ref=store)["subject"] == "Google_1"
    # The old token survives only its short grace window...
    assert device_sessions.resolve(token, table_ref=store) is not None
    # ...and can never rotate again (replay forks no second chain).
    assert device_sessions.refresh(token, table_ref=store) is None

    old_pk = f"session#{device_sessions._hash(token)}"
    store.items[old_pk]["ttl"] = int(time.time()) - 1
    assert device_sessions.resolve(token, table_ref=store) is None


def test_revoke_kills_the_session(store):
    token, _ = device_sessions.create_session("Google_1", "op@x", table_ref=store)

    assert device_sessions.revoke(token, table_ref=store) is True
    assert device_sessions.resolve(token, table_ref=store) is None
    assert device_sessions.revoke(token, table_ref=store) is False


def test_device_endpoints_start_poll_confirm(monkeypatch, store):
    monkeypatch.setattr(agent_api.session, "_csrf_ok", lambda event, method: True)
    monkeypatch.setattr(
        agent_api.session, "_session_payload",
        lambda event: {"sub": "op@datatalks.club", "subject": "Google_1"},
    )

    response = agent_api.route(
        _device_event("/api/agent/device/start"), "POST", "/api/agent/device/start")
    assert response["statusCode"] == 200
    pairing = json.loads(response["body"])

    poll_event = _device_event("/api/agent/device/token", {"device_code": pairing["device_code"]})
    response = agent_api.route(poll_event, "POST", "/api/agent/device/token")
    assert json.loads(response["body"]) == {"status": "pending"}

    confirm_event = _device_event(
        "/api/agent/device/confirm", {"user_code": pairing["user_code"].lower()})
    response = agent_api.route(confirm_event, "POST", "/api/agent/device/confirm")
    assert response["statusCode"] == 200

    response = agent_api.route(poll_event, "POST", "/api/agent/device/token")
    result = json.loads(response["body"])
    assert result["status"] == "approved"

    event = {"headers": {"authorization": f"Bearer {result['token']}"}}
    subject, error = agent_api.authenticate(event)
    assert error is None and subject == "Google_1"
    assert event["_dtc_claims"] == {"sub": "Google_1", "email": "op@datatalks.club"}


def test_device_bearer_qualifies_as_operator(monkeypatch, store):
    monkeypatch.setenv("OPERATOR_EMAILS", "op@datatalks.club")
    token, _ = device_sessions.create_session("Google_1", "op@datatalks.club", table_ref=store)

    event = {"headers": {"authorization": f"Bearer {token}"}}
    subject, error = agent_api.authenticate(event)
    assert error is None
    assert agent_api._is_operator(event, subject) is True


def test_confirm_requires_a_console_session(monkeypatch, store):
    monkeypatch.setattr(agent_api.session, "_csrf_ok", lambda event, method: True)
    monkeypatch.setattr(agent_api.session, "_session_payload", lambda event: {})

    response = agent_api.route(
        _device_event("/api/agent/device/confirm", {"user_code": "ABCD-EFGH"}),
        "POST", "/api/agent/device/confirm")

    assert response["statusCode"] == 401


def test_public_config_advertises_device_login():
    response = agent_api.route(
        {"requestContext": {"http": {"method": "GET", "path": "/api/agent/config"}}},
        "GET", "/api/agent/config")
    assert json.loads(response["body"])["device_login"] is True


def test_refresh_and_revoke_endpoints(monkeypatch, store):
    token, _ = device_sessions.create_session("Google_1", "op@x", table_ref=store)

    response = agent_api.route(
        _device_event("/api/agent/device/refresh"),
        "POST", "/api/agent/device/refresh",
    )
    assert response["statusCode"] == 401  # no bearer supplied

    event = _device_event("/api/agent/device/refresh")
    event["headers"] = {"authorization": f"Bearer {token}"}
    response = agent_api.route(event, "POST", "/api/agent/device/refresh")
    body = json.loads(response["body"])
    assert body["token"].startswith(device_sessions.PREFIX)
    assert device_sessions.resolve(body["token"], table_ref=store)

    event = _device_event("/api/agent/device/revoke")
    event["headers"] = {"authorization": f"Bearer {body['token']}"}
    response = agent_api.route(event, "POST", "/api/agent/device/revoke")
    assert json.loads(response["body"]) == {"revoked": True}
    assert device_sessions.resolve(body["token"], table_ref=store) is None
