"""Device token delivery (review M7/M8), on real moto DynamoDB.

Approval stores only the decision and identity; the winning poll mints
the bearer in memory and, in one TransactWriteItems, consumes the
approved, unexpired grant and creates the session keyed by the bearer's
hash. These tests force every race the review reproduced against the old
read-put-delete delivery: concurrent polls (barrier and nested), stale
polls after revoke or refresh, a crash before the commit followed by
expiry, expiry at the final write, and whole-table scans for plaintext
codes or bearers at every stage."""
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import boto3
import pytest
from moto import mock_aws

from src.dapier.auth import device_sessions as d

TABLE = "device-delivery"


@pytest.fixture
def store():
    with mock_aws():
        yield boto3.resource("dynamodb", region_name="eu-west-1").create_table(
            TableName=TABLE, BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}])


def rows(store, kind=None):
    return [r for r in store.scan()["Items"] if kind is None or r.get("kind") == kind]


def approved(store):
    device_code, view = d.start(table_ref=store)
    assert d.decide(view["user_code"], True, "winner", "w@x", table_ref=store)
    return device_code, view["user_code"]


class Hook:
    """Wraps the table; runs ``callback`` around the first matching call.

    ``method`` is a table method (get_item, delete_item …) or "transact"
    for the transaction behind ``meta.client``."""

    def __init__(self, store, method, callback, after=False):
        self.store, self.method, self.callback, self.after, self.fired = store, method, callback, after, False

    def _wrap(self, target):
        def call(**kwargs):
            fire = not self.fired
            self.fired = True
            if fire and not self.after:
                self.callback()
            result = target(**kwargs)
            if fire and self.after:
                self.callback()
            return result
        return call

    def __getattr__(self, name):
        target = getattr(self.store, name)
        if name == "meta" and self.method == "transact":
            hook, client = self, target.client

            class _Client:
                def transact_write_items(self, **kwargs):
                    return hook._wrap(client.transact_write_items)(**kwargs)

            class _Meta:
                pass

            _Meta.client = _Client()
            return _Meta()
        return self._wrap(target) if name == self.method else target


def scan_blob(store):
    return json.dumps(rows(store), default=str)


# --- M8: nothing plaintext at rest, at any stage ------------------------------

def test_no_plaintext_code_or_bearer_is_ever_stored(store):
    device_code, view = d.start(table_ref=store)
    code = view["user_code"]
    snapshots = {"before approval": scan_blob(store)}
    assert d.decide(code, True, "winner", "w@x", table_ref=store)
    snapshots["after approval"] = scan_blob(store)
    hooked = Hook(store, "transact", lambda: snapshots.__setitem__("just before delivery", scan_blob(store)))
    result = d.poll(device_code, table_ref=hooked)
    snapshots["after delivery"] = scan_blob(store)
    d.revoke(result["token"], table_ref=store)
    snapshots["after consumption and revoke"] = scan_blob(store)
    secrets = [code, code.replace("-", ""), device_code, result["token"]]
    for stage, blob in snapshots.items():
        for secret in secrets:
            assert secret not in blob, stage
        assert "dapd_" not in blob, stage   # no bearer or device code in any form
    approved_row = json.loads(snapshots["after approval"])
    assert [r for r in approved_row if r.get("kind") == "pairing"][0].get("session") is None


# --- M7: one delivery, losers write nothing ------------------------------------

def test_sequential_polls_deliver_once(store):
    device_code, _ = approved(store)
    first = d.poll(device_code, table_ref=store)
    assert d.resolve(first["token"], table_ref=store)["subject"] == "winner"
    for _ in range(3):
        assert d.poll(device_code, table_ref=store) == {"error": "expired_token"}
    assert len(rows(store, "session")) == 1
    assert not rows(store, "pairing") and not rows(store, "usercode")


def test_nested_concurrent_poll_cannot_redeliver(store):
    device_code, _ = approved(store)
    delivered = []
    stale = Hook(store, "get_item", lambda: delivered.append(d.poll(device_code, table_ref=store)), after=True)
    second = d.poll(device_code, table_ref=stale)
    assert delivered[0].get("token")
    assert second == {"error": "expired_token"}
    assert len(rows(store, "session")) == 1


def test_two_threads_behind_a_barrier_get_one_delivery(store):
    device_code, _ = approved(store)
    barrier = Barrier(2)

    def run():
        own = boto3.resource("dynamodb", region_name="eu-west-1").Table(TABLE)
        return d.poll(device_code, table_ref=Hook(own, "get_item", lambda: barrier.wait(timeout=10), after=True))

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: run(), range(2)))
    assert sum(bool(o.get("token")) for o in outcomes) == 1
    assert [o for o in outcomes if not o.get("token")] == [{"error": "expired_token"}]
    assert len(rows(store, "session")) == 1


def test_stale_poll_cannot_resurrect_a_revoked_session(store):
    device_code, _ = approved(store)
    delivered = []

    def winner():
        result = d.poll(device_code, table_ref=store)
        delivered.append(result)
        assert d.revoke(result["token"], table_ref=store)

    second = d.poll(device_code, table_ref=Hook(store, "get_item", winner, after=True))
    assert second == {"error": "expired_token"}
    assert d.resolve(delivered[0]["token"], table_ref=store) is None
    assert rows(store, "session") == []


def test_stale_poll_cannot_reset_a_rotation(store):
    device_code, _ = approved(store)
    delivered, rotated = [], []

    def winner():
        result = d.poll(device_code, table_ref=store)
        delivered.append(result)
        rotated.append(d.refresh(result["token"], table_ref=store)[0])

    second = d.poll(device_code, table_ref=Hook(store, "get_item", winner, after=True))
    assert second == {"error": "expired_token"}
    old = store.get_item(Key={"pk": "session#" + d._hash(delivered[0]["token"])})["Item"]
    assert "rotated_at" in old                       # the rotation marker stands
    assert d.refresh(delivered[0]["token"], table_ref=store) is None   # single chain
    assert len(rows(store, "session")) == 2          # old (grace) + one replacement


class Crash(Exception):
    pass


def test_crash_before_the_commit_then_expiry_leaves_nothing_live(store, monkeypatch):
    device_code, _ = approved(store)

    def crash():
        raise Crash("process dies before the delivery transaction")

    with pytest.raises(Crash):
        d.poll(device_code, table_ref=Hook(store, "transact", crash))
    assert rows(store, "session") == []              # nothing minted
    assert rows(store, "pairing")[0]["status"] == "approved"
    ttl = int(rows(store, "pairing")[0]["ttl"])
    monkeypatch.setattr(d.time, "time", lambda: ttl + 1)
    assert d.poll(device_code, table_ref=store) == {"error": "expired_token"}
    assert rows(store, "session") == [] and not rows(store, "pairing")


def test_a_retry_after_a_crash_before_commit_still_delivers_once(store):
    device_code, _ = approved(store)

    def crash():
        raise Crash("process dies before the delivery transaction")

    with pytest.raises(Crash):
        d.poll(device_code, table_ref=Hook(store, "transact", crash))
    result = d.poll(device_code, table_ref=store)
    assert d.resolve(result["token"], table_ref=store)["subject"] == "winner"
    assert d.poll(device_code, table_ref=store) == {"error": "expired_token"}
    assert len(rows(store, "session")) == 1


def test_expiry_at_the_final_write_cannot_deliver(store, monkeypatch):
    device_code, _ = approved(store)
    ttl = int(rows(store, "pairing")[0]["ttl"])
    # The poll's read-time check still sees a live grant (first clock read);
    # every later clock read — the ones that build the delivery transaction —
    # is past the ttl, so only the write's own condition (ttl > now) can stop
    # the mint. DynamoDB conditions have no server-clock function, so "now"
    # is the poll's clock at write time.
    reads = iter([ttl - 1])
    monkeypatch.setattr(d.time, "time", lambda: next(reads, ttl + 1))
    seen = []
    watch = Hook(store, "transact", lambda: seen.append(True))
    assert d.poll(device_code, table_ref=watch) == {"error": "expired_token"}
    assert seen == [True]                     # the transaction ran and was refused
    assert rows(store, "session") == []


def test_expired_approval_never_mints(store, monkeypatch):
    device_code, _ = approved(store)
    ttl = int(rows(store, "pairing")[0]["ttl"])
    monkeypatch.setattr(d.time, "time", lambda: ttl + 1)
    assert d.poll(device_code, table_ref=store) == {"error": "expired_token"}
    assert rows(store) == []


def test_denial_is_reported_once_and_mints_nothing(store):
    device_code, view = d.start(table_ref=store)
    assert d.decide(view["user_code"], False, "denier", "x@x", table_ref=store)
    stale = Hook(store, "get_item", lambda: d.poll(device_code, table_ref=store), after=True)
    assert d.poll(device_code, table_ref=stale) == {"error": "expired_token"}
    assert rows(store) == []


def test_pending_poll_writes_nothing(store):
    device_code, _ = d.start(table_ref=store)
    before = scan_blob(store)
    assert d.poll(device_code, table_ref=store) == {"error": "authorization_pending"}
    assert scan_blob(store) == before


def test_api_audit_and_logs_omit_codes_and_bearer(store, monkeypatch, caplog, capsys):
    from src.dapier.api import agent as api

    events = []
    api.reset_rate_limits()
    monkeypatch.setattr(d, "table", lambda: store)
    monkeypatch.setattr(api.session, "_csrf_ok", lambda *a: True)
    monkeypatch.setattr(api.session, "_session_payload", lambda e: {"subject": "winner", "sub": "w@x"})
    monkeypatch.setattr(api.audit, "emit", lambda *a, **kw: events.append((a, kw)))

    def route(path, body=None, params=None, method="POST"):
        return api.route({"body": json.dumps(body or {}), "headers": {},
                          "requestContext": {"http": {"sourceIp": "127.0.0.1"}},
                          "queryStringParameters": params or {}}, method, path)

    view = json.loads(route("/api/agent/device/start")["body"])
    code, device_code = view["user_code"], view["device_code"]
    assert route("/api/agent/device/pending", params={"code": code}, method="GET")["statusCode"] == 200
    assert route("/api/agent/device/approve", {"user_code": code, "approve": True})["statusCode"] == 200
    token = json.loads(route("/api/agent/device/token", {"device_code": device_code})["body"])["token"]
    assert d.resolve(token, table_ref=store)
    again = json.loads(route("/api/agent/device/token", {"device_code": device_code})["body"])
    assert again == {"error": "expired_token"}
    captured = capsys.readouterr()
    logs = json.dumps(events, default=str) + caplog.text + captured.out + captured.err
    assert all(secret not in logs for secret in (code, code.replace("-", ""), device_code, token))
