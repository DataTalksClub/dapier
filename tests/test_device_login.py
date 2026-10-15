"""Device pairing: storage logic and the /api/agent/device/* endpoints.

The wire matches the DataOps grant this flow was ported from: RFC 8628
error vocabulary on the poll, review-then-decide in the browser, a
shared five-attempt budget per pairing, and hashed codes everywhere at
rest.
"""

import json
import re
import time

import pytest

from src.dapier.api import agent as agent_api
from src.dapier.auth import device_sessions


_EXPRESSION_WORDS = {
    "SET", "REMOVE", "ADD", "DELETE", "AND", "OR", "NOT", "BETWEEN", "IN",
    "attribute_exists", "attribute_not_exists", "attribute_type",
    "begins_with", "contains", "size", "if_not_exists",
}


def _check_expression(expression, names):
    """Every attribute in an expression must be a #placeholder.

    Real DynamoDB rejects reserved words (session, ttl, status …) used bare;
    moto accepts them — the bug that once made every live approval fail with
    "Unknown or expired code". So the store below enforces what moto does not.
    """
    for word in re.findall(r"[#:]?[A-Za-z_][A-Za-z0-9_]*", expression or ""):
        if word.startswith(("#", ":")) or word in _EXPRESSION_WORDS:
            continue
        if word not in set((names or {}).values()):
            raise ValueError(f"bare attribute in expression: {word}")


class StrictTable:
    """A moto DynamoDB table (real conditional-write semantics) behind the
    placeholder check above. ``items`` is a read-only snapshot by key."""

    def __init__(self, table):
        self._table = table

    def put_item(self, **kwargs):
        return self._table.put_item(**kwargs)

    def get_item(self, **kwargs):
        return self._table.get_item(**kwargs)

    def delete_item(self, **kwargs):
        return self._table.delete_item(**kwargs)

    def update_item(self, **kwargs):
        names = kwargs.get("ExpressionAttributeNames")
        _check_expression(kwargs.get("UpdateExpression"), names)
        _check_expression(kwargs.get("ConditionExpression"), names)
        return self._table.update_item(**kwargs)

    @property
    def items(self):
        return {item["pk"]: item for item in self._table.scan()["Items"]}

    def expire(self, pk):
        self._table.update_item(Key={"pk": pk}, UpdateExpression="SET #t = :t",
                                ExpressionAttributeNames={"#t": "ttl"},
                                ExpressionAttributeValues={":t": int(time.time()) - 1})


@pytest.fixture(autouse=True)
def clean_rate_limits():
    agent_api.reset_rate_limits()
    yield
    agent_api.reset_rate_limits()


@pytest.fixture
def store(monkeypatch):
    import boto3
    from moto import mock_aws

    with mock_aws():
        table = boto3.resource("dynamodb", region_name="eu-west-1").create_table(
            TableName="device-sessions", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}])
        strict = StrictTable(table)
        monkeypatch.setattr(device_sessions, "table", lambda: strict)
        yield strict


def _device_event(path, body=None, source_ip="10.0.0.1", method="POST"):
    event = {
        "requestContext": {"http": {"method": method, "path": path, "sourceIp": source_ip}},
        "headers": {},
    }
    if body is not None:
        event["body"] = json.dumps(body)
    return event


def _start_pairing(label="grace@laptop", source_ip="10.0.0.1"):
    response = agent_api.route(
        _device_event("/api/agent/device/start", {"label": label}, source_ip=source_ip),
        "POST", "/api/agent/device/start")
    assert response["statusCode"] == 200
    return json.loads(response["body"])


def test_pairing_lifecycle_end_to_end(store):
    device_code, view = device_sessions.start(table_ref=store, label="grace@laptop",
                                              request_ip="203.0.113.9")

    assert view["user_code"].count("-") == 1
    left, right = view["user_code"].split("-")
    assert len(left) == len(right) == 4
    assert all(char in device_sessions.USER_CODE_ALPHABET for char in left + right)
    assert device_code.startswith(device_sessions.PREFIX)
    assert device_sessions.poll(device_code, table_ref=store) == {
        "error": "authorization_pending"}

    facts = device_sessions.lookup(view["user_code"], table_ref=store)
    assert facts["label"] == "grace@laptop"
    assert facts["request_ip"] == "203.0.113.9"
    assert facts["expires_at"] > time.time()

    outcome = device_sessions.decide(
        view["user_code"].lower(), True, "Google_1", "op@datatalks.club", table_ref=store)
    assert outcome == {"status": "approved", "label": "grace@laptop"}

    result = device_sessions.poll(device_code, table_ref=store)
    assert result["token"].startswith(device_sessions.PREFIX)
    assert result["subject"] == "Google_1"
    assert result["email"] == "op@datatalks.club"
    assert result["expires_at"] > time.time()
    # Single delivery: the pairing is gone after the first successful poll.
    assert device_sessions.poll(device_code, table_ref=store) == {"error": "expired_token"}

    session = device_sessions.resolve(result["token"], table_ref=store)
    assert session == {"subject": "Google_1", "email": "op@datatalks.club"}


def test_the_store_never_carries_a_plaintext_user_code(store):
    device_code, view = device_sessions.start(table_ref=store)
    plain = view["user_code"].replace("-", "")

    for pk, item in store.items.items():
        assert plain not in pk
        blob = json.dumps(item, default=str)
        assert view["user_code"] not in blob and plain not in blob
    pairing = store.items[f"pairing#{device_sessions._hash(device_code)}"]
    canonical = view["user_code"]
    assert pairing["user_code_hash"] == device_sessions._hash(canonical)
    pointer_keys = [pk for pk in store.items if pk.startswith("usercode#")]
    assert pointer_keys == [f"usercode#{device_sessions._hash(canonical)}"]


def test_normalize_folds_case_and_separators_never_truncates():
    assert device_sessions.normalize_user_code("abcd-efgh") == "ABCD-EFGH"
    assert device_sessions.normalize_user_code(" bcdf ghjk ") == "BCDF-GHJK"
    assert device_sessions.normalize_user_code("BCDF-GHJ") == ""
    assert device_sessions.normalize_user_code("BCDFGHJKX") == ""


def test_denied_grant_refuses_the_poll_and_dies(store):
    device_code, view = device_sessions.start(table_ref=store)
    outcome = device_sessions.decide(
        view["user_code"], False, "Google_1", "op@x", table_ref=store)
    assert outcome == {"status": "denied", "label": "Dapier CLI"}

    assert device_sessions.poll(device_code, table_ref=store) == {"error": "access_denied"}
    # The grant is spent: a second poll reads as an unknown code.
    assert device_sessions.poll(device_code, table_ref=store) == {"error": "expired_token"}
    # A late approve cannot resurrect a denied pairing.
    assert device_sessions.decide(
        view["user_code"], True, "Google_1", "op@x", table_ref=store) is None


def test_the_attempt_budget_kills_guessed_codes(store):
    _, view = device_sessions.start(table_ref=store)

    for _ in range(device_sessions.DEVICE_MAX_ATTEMPTS):
        assert device_sessions.lookup(view["user_code"], table_ref=store)
    # At the budget the decision is refused...
    assert device_sessions.decide(
        view["user_code"], True, "Google_1", "op@x", table_ref=store) is None
    # ...and one peek past it destroys the grant.
    assert device_sessions.lookup(view["user_code"], table_ref=store) is None
    assert not [pk for pk in store.items if pk.startswith(("pairing#", "usercode#"))]


def test_labels_are_sanitized_for_the_operator(store):
    _, view = device_sessions.start(table_ref=store, label="grace@laptop" + "g" * 90)
    facts = device_sessions.lookup(view["user_code"], table_ref=store)
    assert len(facts["label"]) == 80 and facts["label"].startswith("grace@laptop")

    _, bare = device_sessions.start(table_ref=store, label=None)
    assert device_sessions.lookup(bare["user_code"], table_ref=store)["label"] == "Dapier CLI"


def test_approve_wins_exactly_once(store):
    device_code, view = device_sessions.start(table_ref=store)

    assert device_sessions.decide(
        view["user_code"], True, "Google_1", "op@x", table_ref=store)["status"] == "approved"
    assert device_sessions.decide(
        view["user_code"], True, "Google_2", "op2@x", table_ref=store) is None


def test_resolve_rejects_unknown_and_expired_sessions(store):
    token, _ = device_sessions.create_session("Google_1", "op@x", table_ref=store)
    assert device_sessions.resolve(token, table_ref=store)["subject"] == "Google_1"
    assert device_sessions.resolve("dapd_unknown", table_ref=store) is None
    assert device_sessions.resolve("dap_", table_ref=store) is None

    session_pk = f"session#{device_sessions._hash(token)}"
    store.expire(session_pk)
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
    store.expire(old_pk)
    assert device_sessions.resolve(token, table_ref=store) is None


def test_revoke_kills_the_session(store):
    token, _ = device_sessions.create_session("Google_1", "op@x", table_ref=store)

    assert device_sessions.revoke(token, table_ref=store) is True
    assert device_sessions.resolve(token, table_ref=store) is None
    assert device_sessions.revoke(token, table_ref=store) is False


def test_device_endpoints_review_approve_poll(monkeypatch, store):
    monkeypatch.setattr(agent_api.session, "_csrf_ok", lambda event, method: True)
    monkeypatch.setattr(
        agent_api.session, "_session_payload",
        lambda event: {"sub": "op@datatalks.club", "subject": "Google_1"},
    )

    pairing = _start_pairing(label="grace@laptop")
    assert pairing["verification_uri"].endswith("/device")
    assert pairing["verification_uri_complete"] == \
        pairing["verification_uri"] + f"?code={pairing['user_code']}"
    assert pairing["interval"] == device_sessions.POLL_INTERVAL_SECONDS

    poll_event = _device_event("/api/agent/device/token", {"device_code": pairing["device_code"]})
    response = agent_api.route(poll_event, "POST", "/api/agent/device/token")
    assert response["statusCode"] == 400
    assert json.loads(response["body"]) == {"error": "authorization_pending"}

    pending_event = _device_event(
        "/api/agent/device/pending", method="GET")
    pending_event["queryStringParameters"] = {"code": pairing["user_code"].lower()}
    response = agent_api.route(pending_event, "GET", "/api/agent/device/pending")
    assert response["statusCode"] == 200
    facts = json.loads(response["body"])
    assert facts["label"] == "grace@laptop"
    assert facts["request_ip"] == "10.0.0.1"
    assert facts["server_now"] > time.time() - 60

    approve_event = _device_event(
        "/api/agent/device/approve", {"user_code": pairing["user_code"], "approve": True})
    response = agent_api.route(approve_event, "POST", "/api/agent/device/approve")
    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"status": "approved", "label": "grace@laptop"}

    response = agent_api.route(poll_event, "POST", "/api/agent/device/token")
    assert response["statusCode"] == 200
    result = json.loads(response["body"])
    assert result["token"].startswith(device_sessions.PREFIX)

    response = agent_api.route(poll_event, "POST", "/api/agent/device/token")
    assert response["statusCode"] == 400
    assert json.loads(response["body"]) == {"error": "expired_token"}

    event = {"headers": {"authorization": f"Bearer {result['token']}"}}
    subject, error = agent_api.authenticate(event)
    assert error is None and subject == "Google_1"
    assert event["_dtc_claims"] == {"sub": "Google_1", "email": "op@datatalks.club"}


def test_deny_endpoint_refuses_the_poll(monkeypatch, store):
    monkeypatch.setattr(agent_api.session, "_csrf_ok", lambda event, method: True)
    monkeypatch.setattr(
        agent_api.session, "_session_payload",
        lambda event: {"sub": "op@datatalks.club", "subject": "Google_1"},
    )
    pairing = _start_pairing()

    deny = _device_event(
        "/api/agent/device/approve", {"user_code": pairing["user_code"], "approve": False})
    response = agent_api.route(deny, "POST", "/api/agent/device/approve")
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["status"] == "denied"

    poll_event = _device_event("/api/agent/device/token", {"device_code": pairing["device_code"]})
    response = agent_api.route(poll_event, "POST", "/api/agent/device/token")
    assert response["statusCode"] == 400
    assert json.loads(response["body"]) == {"error": "access_denied"}


def test_review_and_decision_require_a_console_session(monkeypatch, store):
    monkeypatch.setattr(agent_api.session, "_csrf_ok", lambda event, method: True)
    monkeypatch.setattr(agent_api.session, "_session_payload", lambda event: {})
    pairing = _start_pairing()

    pending_event = _device_event("/api/agent/device/pending", method="GET")
    pending_event["queryStringParameters"] = {"code": pairing["user_code"]}
    response = agent_api.route(pending_event, "GET", "/api/agent/device/pending")
    assert response["statusCode"] == 401

    response = agent_api.route(
        _device_event("/api/agent/device/approve", {"user_code": pairing["user_code"]}),
        "POST", "/api/agent/device/approve")
    assert response["statusCode"] == 401


def test_approve_rejects_cross_site_and_unknown_codes(monkeypatch, store):
    monkeypatch.setattr(agent_api.session, "_csrf_ok", lambda event, method: False)
    monkeypatch.setattr(
        agent_api.session, "_session_payload",
        lambda event: {"sub": "op@datatalks.club", "subject": "Google_1"},
    )
    response = agent_api.route(
        _device_event("/api/agent/device/approve", {"user_code": "ABCD-EFGH", "approve": True}),
        "POST", "/api/agent/device/approve")
    assert response["statusCode"] == 403

    monkeypatch.setattr(agent_api.session, "_csrf_ok", lambda event, method: True)
    response = agent_api.route(
        _device_event("/api/agent/device/approve", {"user_code": "ZZZZ-ZZZZ", "approve": True}),
        "POST", "/api/agent/device/approve")
    assert response["statusCode"] == 404

    pending_event = _device_event("/api/agent/device/pending", method="GET")
    pending_event["queryStringParameters"] = {"code": "ZZZZ-ZZZZ"}
    response = agent_api.route(pending_event, "GET", "/api/agent/device/pending")
    assert response["statusCode"] == 404


def test_poll_answers_unknown_codes_with_expired_token(store):
    response = agent_api.route(
        _device_event("/api/agent/device/token", {"device_code": "dapd_unknown"}),
        "POST", "/api/agent/device/token")
    assert response["statusCode"] == 400
    assert json.loads(response["body"]) == {"error": "expired_token"}


def test_poll_slow_downs_under_flood(monkeypatch, store):
    for _ in range(120):
        assert agent_api.route(
            _device_event("/api/agent/device/token", {"device_code": "dapd_x"}),
            "POST", "/api/agent/device/token")["statusCode"] in (400, 429)
    response = agent_api.route(
        _device_event("/api/agent/device/token", {"device_code": "dapd_x"}),
        "POST", "/api/agent/device/token")
    assert response["statusCode"] == 429
    assert json.loads(response["body"]) == {"error": "slow_down"}


def test_start_urls_follow_the_request_host(monkeypatch, store):
    event = _device_event("/api/agent/device/start", {})
    event["headers"] = {"x-forwarded-host": "dapier.dtcdev.click", "x-forwarded-proto": "https"}
    body = json.loads(agent_api.route(event, "POST", "/api/agent/device/start")["body"])
    assert body["verification_uri"] == "https://dapier.dtcdev.click/device"
    assert body["verification_uri_complete"].startswith("https://dapier.dtcdev.click/device?code=")

    event = _device_event("/api/agent/device/start", {})
    event["headers"] = {"host": "localhost:3000"}
    body = json.loads(agent_api.route(event, "POST", "/api/agent/device/start")["body"])
    assert body["verification_uri"] == "http://localhost:3000/device"


def test_device_bearer_qualifies_as_operator(monkeypatch, store):
    monkeypatch.setenv("OPERATOR_EMAILS", "op@datatalks.club")
    token, _ = device_sessions.create_session("Google_1", "op@datatalks.club", table_ref=store)

    event = {"headers": {"authorization": f"Bearer {token}"}}
    subject, error = agent_api.authenticate(event)
    assert error is None
    assert agent_api._is_operator(event, subject) is True


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


def _signed_in(monkeypatch):
    monkeypatch.setattr(agent_api.session, "_csrf_ok", lambda event, method: True)
    monkeypatch.setattr(
        agent_api.session, "_session_payload",
        lambda event: {"sub": "op@datatalks.club", "subject": "Google_1"},
    )


def _audits(monkeypatch):
    events = []
    monkeypatch.setattr(agent_api.audit, "emit",
                        lambda *args, **kwargs: events.append((args, kwargs)))
    return events


def test_codes_come_from_the_canonical_family_alphabet(store):
    # dakit's device-code.js validates against this alphabet (no vowels,
    # no L/V, no 0/1); the server must never issue a code it would reject.
    assert device_sessions.USER_CODE_ALPHABET == "BCDFGHJKMNPQRSTWXZ23456789"
    for _ in range(50):
        _, view = device_sessions.start(table_ref=store)
        assert re.fullmatch(r"[BCDFGHJKMNPQRSTWXZ2-9]{4}-[BCDFGHJKMNPQRSTWXZ2-9]{4}",
                            view["user_code"])


def test_the_decision_must_be_explicit(monkeypatch, store):
    """A body without approve: true|false decides nothing."""
    _signed_in(monkeypatch)
    pairing = _start_pairing()
    for body in ({"user_code": pairing["user_code"]},
                 {"user_code": pairing["user_code"], "approve": "yes"},
                 {"user_code": pairing["user_code"], "approve": None}):
        response = agent_api.route(
            _device_event("/api/agent/device/approve", body), "POST", "/api/agent/device/approve")
        assert response["statusCode"] == 400
    poll_event = _device_event("/api/agent/device/token", {"device_code": pairing["device_code"]})
    response = agent_api.route(poll_event, "POST", "/api/agent/device/token")
    assert json.loads(response["body"]) == {"error": "authorization_pending"}


def test_deny_is_recorded_and_audited_without_the_code(monkeypatch, store):
    _signed_in(monkeypatch)
    events = _audits(monkeypatch)
    pairing = _start_pairing()
    response = agent_api.route(
        _device_event("/api/agent/device/approve",
                      {"user_code": pairing["user_code"], "approve": False}),
        "POST", "/api/agent/device/approve")
    assert response["statusCode"] == 200
    row = store.items[f"pairing#{device_sessions._hash(pairing['device_code'])}"]
    assert row["status"] == "denied" and row["decided_at"]
    assert [(args[1], kwargs["outcome"]) for args, kwargs in events] == [
        (agent_api.audit.DEVICE_LOGIN, "denied")]
    plain = pairing["user_code"].replace("-", "")
    for args, kwargs in events:
        blob = json.dumps([args, kwargs], default=str)
        assert pairing["user_code"] not in blob and plain not in blob
    # Nothing at rest carries the plaintext code after the decision either.
    for pk, item in store.items.items():
        blob = pk + json.dumps(item, default=str)
        assert pairing["user_code"] not in blob and plain not in blob


def test_deny_and_authorize_share_one_route_and_one_guard(monkeypatch, store):
    """Deny is as reachable as Authorize: same endpoint, same checks."""
    _signed_in(monkeypatch)
    for approve in (True, False):
        pairing = _start_pairing()
        response = agent_api.route(
            _device_event("/api/agent/device/approve",
                          {"user_code": pairing["user_code"], "approve": approve}),
            "POST", "/api/agent/device/approve")
        assert response["statusCode"] == 200
        assert json.loads(response["body"])["status"] == ("approved" if approve else "denied")


def test_every_device_response_is_no_store(monkeypatch, store):
    _signed_in(monkeypatch)
    pairing = _start_pairing()
    pending_event = _device_event("/api/agent/device/pending", method="GET")
    pending_event["queryStringParameters"] = {"code": "ZZZZ-ZZZZ"}
    responses = [
        agent_api.route(pending_event, "GET", "/api/agent/device/pending"),
        agent_api.route(_device_event("/api/agent/device/approve", {"user_code": "ZZZZ-ZZZZ",
                                                                    "approve": True}),
                        "POST", "/api/agent/device/approve"),
        agent_api.route(_device_event("/api/agent/device/approve", {"user_code": "x"}),
                        "POST", "/api/agent/device/approve"),
        agent_api.route(_device_event("/api/agent/device/approve",
                                      {"user_code": pairing["user_code"], "approve": False}),
                        "POST", "/api/agent/device/approve"),
        agent_api.route(_device_event("/api/agent/device/token", {"device_code": "dapd_x"}),
                        "POST", "/api/agent/device/token"),
    ]
    monkeypatch.setattr(agent_api.session, "_session_payload", lambda event: {})
    pending_event["queryStringParameters"] = {"code": pairing["user_code"]}
    responses.append(agent_api.route(pending_event, "GET", "/api/agent/device/pending"))
    for response in responses:
        assert response["headers"].get("cache-control") == "no-store", response


def test_peeks_are_rate_limited_per_account(monkeypatch, store):
    _signed_in(monkeypatch)
    pending_event = _device_event("/api/agent/device/pending", method="GET")
    pending_event["queryStringParameters"] = {"code": "ZZZZ-ZZZZ"}
    statuses = [agent_api.route(pending_event, "GET", "/api/agent/device/pending")["statusCode"]
                for _ in range(21)]
    assert statuses[:20] == [404] * 20
    assert statuses[20] == 429
    limited = agent_api.route(pending_event, "GET", "/api/agent/device/pending")
    assert limited["headers"]["retry-after"]


def test_review_facts_say_whether_the_reviewer_shares_the_requesting_network(monkeypatch, store):
    _signed_in(monkeypatch)
    pairing = _start_pairing(source_ip="203.0.113.7")
    for reviewer_ip, same in (("203.0.113.7", True), ("198.51.100.23", False)):
        event = _device_event("/api/agent/device/pending", method="GET", source_ip=reviewer_ip)
        event["queryStringParameters"] = {"code": pairing["user_code"]}
        response = agent_api.route(event, "GET", "/api/agent/device/pending")
        assert response["statusCode"] == 200
        facts = json.loads(response["body"])
        assert facts["same_network"] is same
        assert facts["request_ip"] == "203.0.113.7"
        assert "user_code" not in facts and "device_code" not in facts


class _PeekBeforeDecision:
    """Interleaves: the moment a decision is about to write, another browser
    spends a peek first (the race the review reproduced)."""

    def __init__(self, store, user_code):
        self._store, self._code, self._fired = store, user_code, False
        self.peeked = None

    def __getattr__(self, name):
        return getattr(self._store, name)

    def update_item(self, **kwargs):
        if not self._fired and ":pending" in (kwargs.get("ExpressionAttributeValues") or {}) \
                and "#st" in kwargs.get("UpdateExpression", ""):
            self._fired = True
            self.peeked = device_sessions.lookup(self._code, table_ref=self._store)
        return self._store.update_item(**kwargs)


@pytest.mark.parametrize("approve", [True, False])
def test_a_peek_racing_the_decision_cannot_exceed_the_budget(store, approve):
    device_code, view = device_sessions.start(table_ref=store)
    for _ in range(device_sessions.DEVICE_MAX_ATTEMPTS - 1):
        assert device_sessions.lookup(view["user_code"], table_ref=store) is not None
    racing = _PeekBeforeDecision(store, view["user_code"])
    outcome = device_sessions.decide(view["user_code"], approve, "Google_1", "op@x", table_ref=racing)
    # The interleaved fifth peek took the last attempt; the decision, a sixth
    # operation, is refused by the atomic guard and the grant is destroyed.
    assert racing.peeked is not None
    assert outcome is None
    assert not [pk for pk in store.items if pk.startswith("session#")]
    assert device_sessions.poll(device_code, table_ref=store) == {"error": "expired_token"}


def test_the_counter_is_incremented_in_place_never_assigned(store):
    _, view = device_sessions.start(table_ref=store)
    device_sessions.lookup(view["user_code"], table_ref=store)
    device_sessions.lookup(view["user_code"], table_ref=store)
    outcome = device_sessions.decide(view["user_code"], False, "Google_1", "op@x", table_ref=store)
    assert outcome["status"] == "denied"
    pairing = next(item for pk, item in store.items.items() if pk.startswith("pairing#"))
    assert int(pairing["attempts"]) == 3


def test_an_expired_pairing_cannot_be_decided_even_if_still_stored(store):
    _, view = device_sessions.start(table_ref=store)
    later = time.time() + device_sessions.PAIRING_TTL_SECONDS + 1
    assert device_sessions.decide(view["user_code"], True, "Google_1", "op@x", table_ref=store, now=later) is None
    assert device_sessions.lookup(view["user_code"], table_ref=store, now=later) is None
    assert not [pk for pk in store.items if pk.startswith("session#")]
