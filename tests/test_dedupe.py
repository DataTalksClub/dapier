"""Trigger dedupe (finding 7 of the 2026-09-28 product-loop audit).

Two gaps shared one shape — the same logical delivery could run a workflow
twice:

- ``next_cursor`` polls treat every fetched page as new: the continuation
  cursor advances, but providers recycle pages, so items re-fire. A
  TTL-bounded seen-set (src/dapier/triggers/seen.py, in the cursors table)
  skips items whose stable id was already published.
- Hook triggers minted a fresh uuid per POST, so a provider retry of the
  same delivery double-ran. An optional ``dedupe_path`` derives the event id
  from a stable value in the payload; a publish-time seen-store claim
  answers the retry with the normal 202 while skipping the duplicate.

Fake-table style like tests/test_poll_triggers.py (no moto): the cursor
table stand-in honors the seen store's conditional update so ``claim``'s
atomicity is exercised for real.
"""
import copy
import json
import os
import uuid
from unittest.mock import patch

import pytest

from src.dapier.api import agent as agent_api
from src.dapier.api import router as ingress
from src.dapier.api.admin import routes as admin_routes
from src.dapier.triggers import hook_triggers, poll_triggers, seen
from src.dapier.triggers.email_triggers import TriggerError


# --- shared fakes ----------------------------------------------------------


class FakeCursorsTable:
    """Cursors-table stand-in (all rows keyed by ``cursor_id``), honoring the
    seen store's conditional update the way DynamoDB does: the claim fails
    when the map key is already taken."""

    def __init__(self):
        self.items = {}

    @staticmethod
    def _key(item):
        return item.get("cursor_id") or item.get("scope_id")

    def get_item(self, Key):
        item = self.items.get(self._key(Key))
        return {"Item": copy.deepcopy(item)} if item else {}

    def put_item(self, Item, **_kwargs):
        self.items[self._key(Item)] = copy.deepcopy(Item)

    def update_item(self, Key, ConditionExpression=None,
                    ExpressionAttributeNames=None, ExpressionAttributeValues=None,
                    **_kwargs):
        # seen.claim's shape: SET seen.#k = :expires, expires_at = :expires,
        # updated_at = :at, honored only while the key is absent.
        names = ExpressionAttributeNames or {}
        values = ExpressionAttributeValues or {}
        scope = self._key(Key)
        item = self.items.get(scope) or {}
        if ":empty" in values:
            item = self.items.setdefault(scope, dict(Key))
            item.setdefault(names["#m"], dict(values[":empty"]))
            return {}
        if ConditionExpression and names["#k"] in (item.get(names["#m"]) or {}):
            from botocore.exceptions import ClientError

            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
        item = dict(item)
        item["cursor_id"] = scope
        item.setdefault(names["#m"], {})[names["#k"]] = values[":expires"]
        item["expires_at"] = values[":expires"]
        item["updated_at"] = values[":at"]
        self.items[scope] = item
        return {}

    def delete_item(self, Key):
        self.items.pop(self._key(Key), None)

    def seen_row(self, scope):
        return (self.items.get(f"seen#{scope}") or {}).get("seen") or {}


class StubHookTable:
    """Hook-triggers table holding one stored hook."""

    def __init__(self, item):
        self.item = dict(item)

    def scan(self, Limit=200):
        return {"Items": [dict(self.item)]}

    def get_item(self, Key):
        return {"Item": dict(self.item)} if Key["hook_id"] == self.item["hook_id"] else {}

    def put_item(self, Item):
        self.item = dict(Item)

    def delete_item(self, Key):
        self.item = {}


# --- the seen store --------------------------------------------------------


class TestSeenStore:
    def test_prune_drops_expired_entries_and_caps_the_set(self):
        entries = {"expired": 10, "live": 10_000, "newer": 10_001}
        assert seen.prune(entries, now=100) == {"live": 10_000, "newer": 10_001}
        flood = {f"item-{index}": index + 1 for index in range(6)}
        assert seen.prune(flood, now=0, max_entries=2) == {"item-4": 5, "item-5": 6}

    def test_claim_is_atomic_first_wins(self):
        table = FakeCursorsTable()
        assert seen.claim("hook#orders", "evt-1", table_ref=table, now=1_000) is True
        assert seen.claim("hook#orders", "evt-1", table_ref=table, now=2_000) is False
        assert seen.claim("hook#orders", "evt-2", table_ref=table, now=2_000) is True

    def test_claim_after_ttl_expiry_succeeds_again(self):
        table = FakeCursorsTable()
        now = 1_000
        assert seen.claim("hook#orders", "evt-1", table_ref=table, now=now) is True
        assert seen.claim("hook#orders", "evt-1", table_ref=table,
                          now=now + 1) is False
        later = now + seen.TTL_DAYS * 86400 + 1
        assert seen.claim("hook#orders", "evt-1", table_ref=table, now=later) is True

    def test_remember_merges_and_persists(self):
        table = FakeCursorsTable()
        first = seen.remember("poll#orders", "evt-1", table_ref=table, now=1_000)
        assert first == {"evt-1": 1_000 + seen.TTL_DAYS * 86400}
        second = seen.remember("poll#orders", "evt-2", first, table_ref=table, now=1_001)
        assert set(second) == {"evt-1", "evt-2"}
        assert table.seen_row("poll#orders").keys() == {"evt-1", "evt-2"}

    def test_operations_without_a_configured_table_raise_store_error(self, monkeypatch):
        monkeypatch.delenv(seen.TABLE_ENV, raising=False)
        with pytest.raises(seen.StoreError):
            seen.load("hook#orders")


# --- (a) poll next_cursor dedupe -------------------------------------------


def next_cursor_poll(**overrides):
    item = {"poll_id": "orders-page", "enabled": True,
            "cursor_mode": "next_cursor", "cursor_path": "meta.next",
            "id_path": "", "max_items": 25, "url": "https://api.example.test/list",
            "method": "GET", "headers": {}}
    item.update(overrides)
    return item


def page(*ids):
    return [{"id": item_id, "title": f"title {item_id}"} for item_id in ids]


class TestPollFireDedupe:
    def run_fire(self, items, *, cursors=None, execute_result=None):
        cursors = cursors or FakeCursorsTable()
        fired = []

        def fake_execute(event, **_kwargs):
            fired.append(event["data"]["item_id"])
            return (execute_result or ["some-workflow"])

        with patch.object(poll_triggers, "get_item",
                          return_value=next_cursor_poll()), \
             patch.object(poll_triggers, "fetch_page_response",
                          return_value=(list(items), None)), \
             patch("src.dapier.engine.execute", side_effect=fake_execute):
            result = poll_triggers.fire("orders-page", cursor_table_ref=cursors)
        return result, fired, cursors

    def test_same_item_on_two_consecutive_pages_publishes_once(self):
        result, fired, cursors = self.run_fire(page("a", "b"))
        assert result == {"poll": "orders-page", "fired": 2}
        assert fired == ["a", "b"]

        # The provider recycles the same page on the next poll.
        result, fired, cursors = self.run_fire(page("a", "b"), cursors=cursors)
        assert result == {"poll": "orders-page", "fired": 0, "skipped_seen": 2}
        assert fired == []

    def test_a_genuinely_new_item_still_publishes(self):
        _result, _fired, cursors = self.run_fire(page("a", "b"))
        result, fired, _cursors = self.run_fire(page("a", "b", "c"), cursors=cursors)
        assert result == {"poll": "orders-page", "fired": 1, "skipped_seen": 2}
        assert fired == ["c"]

    def test_seen_entries_expire_after_the_ttl(self):
        _result, _fired, cursors = self.run_fire(page("a"))
        row = cursors.items["seen#poll#orders-page"]
        row["seen"] = {key: 1 for key in row["seen"]}  # all well past expiry

        result, fired, _cursors = self.run_fire(page("a"), cursors=cursors)
        assert result == {"poll": "orders-page", "fired": 1}
        assert fired == ["a"]

    def test_a_failed_item_is_not_marked_seen_and_retries(self):
        cursors = FakeCursorsTable()
        fired = []

        def failing_then_ok(event, **_kwargs):
            if not fired:
                raise RuntimeError("action downstream is down")
            fired.append(event["data"]["item_id"])
            return ["some-workflow"]

        with patch.object(poll_triggers, "get_item",
                          return_value=next_cursor_poll()), \
             patch.object(poll_triggers, "fetch_page_response",
                          return_value=(page("a", "b"), None)), \
             patch("src.dapier.engine.execute", side_effect=failing_then_ok), \
             patch("src.dapier.engine.notify.notify_failure"):
            first = poll_triggers.fire("orders-page", cursor_table_ref=cursors)
        assert first["fired"] == 0
        assert cursors.seen_row("poll#orders-page") == {}

        result, fired, _cursors = self.run_fire(page("a", "b"), cursors=cursors)
        assert result["fired"] == 2  # 'a' was never marked seen, so it retries
        assert fired == ["a", "b"]

    def test_watermark_polls_write_no_seen_rows(self):
        cursors = FakeCursorsTable()
        with patch.object(poll_triggers, "get_item",
                          return_value=next_cursor_poll(cursor_mode="watermark",
                                                        id_path="id")), \
             patch.object(poll_triggers, "fetch_page_response",
                          return_value=(page("a"), None)), \
             patch("src.dapier.engine.execute", return_value=["wf"]):
            poll_triggers.fire("orders-page", cursor_table_ref=cursors)
        assert not [key for key in cursors.items if key.startswith("seen#")]


# --- (b) hook dedupe_path ---------------------------------------------------


def hook_item(**overrides):
    item = {"hook_id": "orders", "kind": "webhook", "url": "u-orders",
            "token": "tok-123", "actions": [], "enabled": True,
            "dedupe_path": "data.delivery_id"}
    item.update(overrides)
    return item


def post(path, body, headers=None):
    return ingress.handler({
        "requestContext": {"http": {"method": "POST", "path": path}},
        "headers": headers or {},
        "queryStringParameters": None,
        "body": body.decode() if isinstance(body, bytes) else body,
    }, None)


def bearer(token="tok-123"):
    return {"authorization": f"Bearer {token}", "content-type": "application/json"}


class TestHookPublishDedupe:
    @pytest.fixture(autouse=True)
    def route_env(self, monkeypatch):
        self.sent = []
        self.seen_table = FakeCursorsTable()
        monkeypatch.setenv("EVENT_QUEUE_URL", "https://sqs.example.test/events")
        monkeypatch.setenv("HOOK_TRIGGERS_TABLE", "hooks")
        monkeypatch.setattr(ingress.queue, "send_message",
                            lambda **kwargs: self.sent.append(kwargs))
        monkeypatch.setattr(seen, "get_table", lambda table_ref=None: self.seen_table)
        yield

    def route_hook(self, item):
        stub = StubHookTable(item)
        import src.dapier.triggers.hook_triggers as hooks

        return patch.object(hooks, "get_table", lambda *a, **k: stub)

    def envelopes(self):
        return [json.loads(message["MessageBody"]) for message in self.sent]

    def test_provider_retry_of_the_same_delivery_publishes_once(self):
        with self.route_hook(hook_item()):
            first = post("/hooks/webhook/orders",
                         b'{"data": {"delivery_id": "d-1"}}', bearer())
            retry = post("/hooks/webhook/orders",
                         b'{"data": {"delivery_id": "d-1"}}', bearer())

        assert first["statusCode"] == retry["statusCode"] == 202
        envelopes = self.envelopes()
        assert len(envelopes) == 1
        assert envelopes[0]["id"] == hook_triggers.dedupe_event_id("orders", "d-1")
        assert envelopes[0]["correlation_id"] == envelopes[0]["id"]
        assert json.loads(retry["body"])["duplicate"] is True
        assert json.loads(retry["body"])["event_id"] == envelopes[0]["id"]

    def test_different_delivery_values_publish_both(self):
        with self.route_hook(hook_item()):
            assert post("/hooks/webhook/orders",
                        b'{"data": {"delivery_id": "d-1"}}', bearer())["statusCode"] == 202
            assert post("/hooks/webhook/orders",
                        b'{"data": {"delivery_id": "d-2"}}', bearer())["statusCode"] == 202
        assert len(self.envelopes()) == 2
        assert len({envelope["id"] for envelope in self.envelopes()}) == 2

    def test_without_dedupe_path_a_fresh_uuid_is_minted_per_post(self):
        with self.route_hook(hook_item(dedupe_path="")):
            assert post("/hooks/webhook/orders",
                        b'{"data": {"delivery_id": "d-1"}}', bearer())["statusCode"] == 202
            assert post("/hooks/webhook/orders",
                        b'{"data": {"delivery_id": "d-1"}}', bearer())["statusCode"] == 202

        envelopes = self.envelopes()
        assert len(envelopes) == 2
        for envelope in envelopes:
            uuid.UUID(envelope["id"])  # a plain uuid4, as before
        assert envelopes[0]["id"] != envelopes[1]["id"]

    def test_a_delivery_without_a_value_at_the_path_still_publishes(self):
        with self.route_hook(hook_item()):
            response = post("/hooks/webhook/orders", b'{"other": 1}', bearer())
        assert response["statusCode"] == 202
        assert len(self.envelopes()) == 1
        assert self.seen_table.items == {}  # nothing was recorded

    def test_a_non_json_payload_still_publishes(self):
        with self.route_hook(hook_item()):
            response = post("/hooks/webhook/orders", b"plain text",
                            {"authorization": "Bearer tok-123",
                             "content-type": "text/plain"})
        assert response["statusCode"] == 202
        assert len(self.envelopes()) == 1

    def test_a_claim_survives_only_until_the_store_expires_it(self):
        with self.route_hook(hook_item()):
            assert post("/hooks/webhook/orders",
                        b'{"data": {"delivery_id": "d-1"}}', bearer())["statusCode"] == 202
            retry = post("/hooks/webhook/orders",
                         b'{"data": {"delivery_id": "d-1"}}', bearer())
        assert json.loads(retry["body"])["duplicate"] is True

        row = self.seen_table.items["seen#hook#orders"]
        row["seen"] = {key: 1 for key in row["seen"]}  # age past the TTL
        with self.route_hook(hook_item()):
            assert post("/hooks/webhook/orders",
                        b'{"data": {"delivery_id": "d-1"}}', bearer())["statusCode"] == 202
        assert len(self.envelopes()) == 2

    def test_a_failed_publish_releases_the_claim_for_the_retry(self):
        with self.route_hook(hook_item()):
            def failing_publish(**_kwargs):
                raise RuntimeError("sqs is down")

            with patch.object(ingress.queue, "send_message",
                              side_effect=failing_publish):
                with pytest.raises(RuntimeError, match="sqs is down"):
                    # In Lambda the propagate becomes a 5xx and the provider
                    # retries; the claim must be gone by then.
                    post("/hooks/webhook/orders",
                         b'{"data": {"delivery_id": "d-1"}}', bearer())
            assert "seen#hook#orders" not in self.seen_table.items

            retry = post("/hooks/webhook/orders",
                         b'{"data": {"delivery_id": "d-1"}}', bearer())
        assert retry["statusCode"] == 202
        assert json.loads(retry["body"]).get("duplicate") is None
        assert len(self.envelopes()) == 1

    def test_telegram_retry_with_the_same_update_publishes_once(self):
        update = b'{"update_id": 555, "message": {"text": "hi", "chat": {}, "from": {}}}'
        headers = {"x-telegram-bot-api-secret-token": "tok-123"}
        with self.route_hook(hook_item(kind="telegram", dedupe_path="update_id")):
            assert post("/hooks/telegram/orders", update, headers)["statusCode"] == 200
            retry = post("/hooks/telegram/orders", update, headers)
        assert json.loads(retry["body"])["duplicate"] is True
        assert len(self.envelopes()) == 1
        assert self.envelopes()[0]["data"]["update_id"] == 555


# --- save-time validation on both surfaces ----------------------------------


def hook_body(**overrides):
    body = {"name": "orders", "kind": "webhook",
            "actions": [{"type": "webhook", "url": "https://hooks.test/x"}]}
    body.update(overrides)
    return body


class TestSaveValidation:
    @pytest.fixture(autouse=True)
    def env(self, monkeypatch):
        monkeypatch.setenv("HOOK_TRIGGERS_TABLE", "hooks")
        monkeypatch.setenv("CONNECTIONS_TABLE", "connections")
        monkeypatch.setenv("GRANTS_TABLE", "grants")

    def admin_save(self, body, table=None):
        event = {"requestContext": {"http": {"method": "PUT",
                                             "path": "/api/admin/hook-triggers"}},
                 "headers": {"host": "dapier.example.test"},
                 "body": json.dumps(body)}
        with patch.object(hook_triggers, "get_table",
                          return_value=table or StubHookTable(hook_item())), \
             patch("src.dapier.auth.session._audit_event"):
            return admin_routes.save_hook_trigger(event, "op@example.test")

    def agent_save(self, body, table=None):
        event = {"headers": {}, "body": json.dumps(body)}
        with patch("src.dapier.api.agent.authenticate", return_value=("sub-1", None)), \
             patch("src.dapier.api.agent.authz.is_operator", return_value=True), \
             patch.object(hook_triggers, "get_table",
                          return_value=table or StubHookTable(hook_item())):
            return agent_api.hook_triggers_api(event, "PUT")

    @pytest.mark.parametrize("bad", [123, {"nested": True}, "no paths here", "a..b"])
    def test_admin_route_rejects_a_bad_dedupe_path_without_storing(self, bad):
        table = StubHookTable(hook_item())
        response = self.admin_save(hook_body(dedupe_path=bad), table=table)
        assert response["statusCode"] == 400
        assert "dotted path" in json.loads(response["body"])["error"]
        assert table.item["dedupe_path"] == "data.delivery_id"  # untouched

    @pytest.mark.parametrize("bad", [123, {"nested": True}, "no paths here", "a..b"])
    def test_agent_route_rejects_a_bad_dedupe_path(self, bad):
        response = self.agent_save(hook_body(dedupe_path=bad))
        assert response["statusCode"] == 400
        assert "dotted path" in json.loads(response["body"])["error"]

    def test_a_valid_dedupe_path_saves_on_both_surfaces(self):
        for response in (self.admin_save(hook_body(dedupe_path="id")),
                         self.agent_save(hook_body(dedupe_path="data.delivery_id"))):
            assert response["statusCode"] == 200
            assert json.loads(response["body"])["dedupe_path"] in ("id", "data.delivery_id")

    def test_saving_without_the_field_clears_it(self):
        table = StubHookTable(hook_item())
        response = self.admin_save(hook_body(), table=table)
        assert response["statusCode"] == 200
        assert table.item["dedupe_path"] == ""

    def test_dedupe_path_survives_an_edit_that_omits_nothing(self):
        table = StubHookTable(hook_item())
        response = self.admin_save(hook_body(dedupe_path="data.delivery_id",
                                             description="later"), table=table)
        assert response["statusCode"] == 200
        assert table.item["dedupe_path"] == "data.delivery_id"

    def test_bad_path_is_a_trigger_error_from_the_domain_module(self):
        with pytest.raises(TriggerError):
            hook_triggers.build_item(hook_body(dedupe_path="!!"), "op", "webhook")
