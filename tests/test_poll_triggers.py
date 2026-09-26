"""Poll triggers: OAuth-refreshed fetch tokens and the first-fire watermark."""
import json
import unittest
from unittest.mock import patch

from src.dapier.api.admin import routes as admin_routes
from src.dapier.triggers import poll_triggers
from src.dapier.triggers.email_triggers import TriggerError


def connected(provider="", credential_id="conn-1"):
    return {"connection_id": "google-drive", "provider": provider,
            "credential_id": credential_id, "status": "connected"}


class BearerTokenTests(unittest.TestCase):
    def test_oauth_connection_gets_a_refreshed_token(self):
        with patch("src.dapier.engine.actions.base._connected_connection",
                   return_value=connected("google")), \
             patch("src.dapier.connections.tokens.get_access_token",
                   return_value=("fresh-token", {"refreshed": True})) as get_token:
            token = poll_triggers._bearer_token("google-drive")

        self.assertEqual(token, "fresh-token")
        get_token.assert_called_once()

    def test_static_token_connection_uses_the_stored_credential(self):
        with patch("src.dapier.engine.actions.base._connected_connection",
                   return_value=connected("", "static-conn")), \
             patch("src.dapier.connections.credentials.get_credential",
                   return_value={"token": "stored-token"}) as get_credential:
            token = poll_triggers._bearer_token("static-conn")

        self.assertEqual(token, "stored-token")
        get_credential.assert_called_once_with("static-conn")

    def test_oauth_refresh_failure_raises_trigger_error(self):
        from src.dapier.connections.tokens import TokenError

        with patch("src.dapier.engine.actions.base._connected_connection",
                   return_value=connected("google")), \
             patch("src.dapier.connections.tokens.get_access_token",
                   side_effect=TokenError("expired")):
            with self.assertRaises(TriggerError) as caught:
                poll_triggers._bearer_token("google-drive")

        self.assertIn("no usable token", str(caught.exception))

    def test_no_token_in_a_static_credential_fails(self):
        with patch("src.dapier.engine.actions.base._connected_connection",
                   return_value=connected("", "static-conn")), \
             patch("src.dapier.connections.credentials.get_credential",
                   return_value={"api_key": "not-a-token"}):
            with self.assertRaises(TriggerError) as caught:
                poll_triggers._bearer_token("static-conn")

        self.assertIn("no stored token", str(caught.exception))


def poll_item():
    return {"poll_id": "drive-mailchimp-s3", "enabled": True,
            "cursor_mode": "watermark", "id_path": "createdTime",
            "max_items": 25, "url": "https://example.test/list",
            "method": "GET", "headers": {}}


def page(*created_times):
    return [{"id": f"file-{index}", "createdTime": created}
            for index, created in enumerate(created_times)]


class FireWatermarkTests(unittest.TestCase):
    """fire(): a fresh poll (no stored cursor) emits the listed items instead
    of comparing them against an unseeded watermark they can never beat."""

    def run_fire(self, cursor, items):
        fired_events = []
        stored = []

        def fake_execute(event, **_kwargs):
            fired_events.append(event["data"]["item_id"])

        with patch.object(poll_triggers, "get_item", return_value=poll_item()), \
             patch.object(poll_triggers, "get_cursor", return_value=cursor), \
             patch.object(poll_triggers, "fetch_page", return_value=list(items)), \
             patch.object(poll_triggers, "put_cursor",
                          side_effect=lambda name, value, table=None: stored.append(value)), \
             patch("src.dapier.engine.execute", side_effect=fake_execute):
            result = poll_triggers.fire("drive-mailchimp-s3")

        return result, fired_events, stored

    def test_first_fire_emits_every_listed_item_in_order(self):
        result, fired, stored = self.run_fire(None, page(
            "2026-09-26T22:00:00.000Z", "2026-09-26T21:00:00.000Z"))

        self.assertEqual(result, {"poll": "drive-mailchimp-s3", "fired": 2})
        self.assertEqual(fired, ["2026-09-26T21:00:00.000Z", "2026-09-26T22:00:00.000Z"])
        # The cursor advances per item and ends at the newest, so the next
        # fire re-lists nothing.
        self.assertEqual(stored[-1], "2026-09-26T22:00:00.000Z")

    def test_seeded_cursor_emits_only_strictly_newer_items(self):
        result, fired, stored = self.run_fire(
            "2026-09-26T21:00:00.000Z",
            page("2026-09-26T21:00:00.000Z", "2026-09-26T23:00:00.000Z"))

        self.assertEqual(result["fired"], 1)
        self.assertEqual(fired, ["2026-09-26T23:00:00.000Z"])
        self.assertEqual(stored, ["2026-09-26T23:00:00.000Z"])

    def test_items_without_a_watermark_are_skipped(self):
        with patch.object(poll_triggers, "get_item", return_value=poll_item()), \
             patch.object(poll_triggers, "get_cursor", return_value=None), \
             patch.object(poll_triggers, "fetch_page",
                          return_value=[{"name": "no timestamp here"}]), \
             patch("src.dapier.engine.execute") as execute:
            result = poll_triggers.fire("drive-mailchimp-s3")

        self.assertEqual(result["fired"], 0)
        execute.assert_not_called()


class FakePollTable:
    """DynamoDB stand-in keyed by poll_id."""

    def __init__(self, items=None):
        self.items = {item["poll_id"]: dict(item) for item in (items or [])}

    def scan(self, **_kwargs):
        return {"Items": [dict(item) for item in self.items.values()]}

    def get_item(self, Key):
        return {"Item": dict(self.items[Key["poll_id"]])} if Key["poll_id"] in self.items else {}

    def put_item(self, Item):
        self.items[Item["poll_id"]] = dict(Item)

    def delete_item(self, Key):
        self.items.pop(Key["poll_id"], None)


class FakeCursorTable:
    """DynamoDB stand-in keyed by cursor_id."""

    def __init__(self):
        self.items = {}

    def get_item(self, Key):
        return {"Item": dict(self.items[Key["cursor_id"]])} if Key["cursor_id"] in self.items else {}

    def put_item(self, Item):
        self.items[Item["cursor_id"]] = dict(Item)

    def delete_item(self, Key):
        self.items.pop(Key["cursor_id"], None)


class FakeEvents:
    """EventBridge stand-in recording rules, targets, and removals."""

    def __init__(self):
        self.rules = {}
        self.targets = {}
        self.removed = []

    def put_rule(self, **kwargs):
        self.rules[kwargs["Name"]] = kwargs

    def put_targets(self, **kwargs):
        self.targets[kwargs["Rule"]] = kwargs["Targets"]

    def remove_targets(self, Rule, Ids, **_kwargs):
        self.removed.append((Rule, tuple(Ids)))

    def delete_rule(self, Name, **_kwargs):
        self.rules.pop(Name, None)


def trigger_body(**overrides):
    body = {
        "name": "drive-updates",
        "expression": "rate(1 hour)",
        "url": "https://example.test/list",
        "list_path": "data.items",
        "id_path": "createdTime",
        "actions": [{"type": "slack", "channel": "#news", "text": "new item"}],
    }
    body.update(overrides)
    return body


class PollApiTests(unittest.TestCase):
    """api_save/api_list/api_delete: validation, rule sync, cursor teardown."""

    def setUp(self):
        self.polls = FakePollTable()
        self.cursors = FakeCursorTable()
        self.events = FakeEvents()

    def save(self, body, operator="op@example.test"):
        return poll_triggers.api_save(
            body, operator, table_ref=self.polls, cursor_table_ref=self.cursors,
            events_client=self.events, target_arn="arn:worker")

    def test_save_stores_the_trigger_and_programs_the_rule(self):
        status, payload = self.save(trigger_body())

        self.assertEqual(status, 200)
        self.assertTrue(payload["created"])
        self.assertEqual(payload["rule"], "dapier-poll-drive-updates")
        self.assertIn("drive-updates", self.polls.items)
        rule = self.events.rules["dapier-poll-drive-updates"]
        self.assertEqual(rule["ScheduleExpression"], "rate(1 hour)")
        self.assertEqual(rule["State"], "ENABLED")
        target = self.events.targets["dapier-poll-drive-updates"][0]
        self.assertEqual(json.loads(target["Input"]),
                         {"trigger": "poll", "poll_id": "drive-updates"})

    def test_save_rejects_a_bad_name_without_storing(self):
        with self.assertRaises(TriggerError):
            self.save(trigger_body(name="Not A Name!"))

        self.assertEqual(self.polls.items, {})
        self.assertEqual(self.events.rules, {})

    def test_update_keeps_the_creator_and_reports_updated(self):
        self.save(trigger_body(), operator="first@example.test")

        status, payload = self.save(trigger_body(url="https://example.test/v2"),
                                    operator="second@example.test")

        self.assertEqual(status, 200)
        self.assertFalse(payload["created"])
        self.assertEqual(self.polls.items["drive-updates"]["created_by"], "first@example.test")

    def test_delete_removes_rule_item_and_cursor(self):
        self.save(trigger_body())
        poll_triggers.put_cursor("drive-updates", "2026-09-26", table=self.cursors)

        status, payload = poll_triggers.api_delete(
            "drive-updates", "op@example.test", table_ref=self.polls,
            cursor_table_ref=self.cursors, events_client=self.events)

        self.assertEqual(status, 200)
        self.assertEqual(self.polls.items, {})
        self.assertEqual(self.cursors.items, {})
        self.assertEqual(self.events.removed,
                         [("dapier-poll-drive-updates", ("dapier-worker",))])
        self.assertEqual(self.events.rules, {})

    def test_delete_unknown_name_raises(self):
        with self.assertRaises(TriggerError):
            poll_triggers.api_delete("missing", "op@example.test",
                                     table_ref=self.polls, events_client=self.events)

    def test_list_reports_cursor_state(self):
        self.save(trigger_body())
        poll_triggers.put_cursor("drive-updates", "42", table=self.cursors)

        status, payload = poll_triggers.api_list(
            table_ref=self.polls, cursor_table_ref=self.cursors)

        self.assertEqual(status, 200)
        self.assertEqual(payload["polls"][0]["cursor"], "42")


class AdminRouteTests(unittest.TestCase):
    """The console routes over /api/admin/poll-triggers."""

    def setUp(self):
        self.polls = FakePollTable()
        self.cursors = FakeCursorTable()

    def stored_trigger(self):
        poll_triggers.api_save(trigger_body(), "op@example.test", table_ref=self.polls,
                               cursor_table_ref=self.cursors, events_client=FakeEvents(),
                               target_arn="arn:worker")

    def route(self, method, body=None, query=None):
        event = {"requestContext": {"http": {"method": method,
                                             "path": "/api/admin/poll-triggers"}},
                 "headers": {"host": "dapier.example.test"}}
        if body is not None:
            event["body"] = body if isinstance(body, str) else json.dumps(body)
        if query is not None:
            event["queryStringParameters"] = query
        with patch.object(poll_triggers, "get_table", return_value=self.polls), \
             patch.object(poll_triggers, "cursor_table", return_value=self.cursors), \
             patch.object(poll_triggers, "sync_rule"), \
             patch.object(poll_triggers, "remove_rule"), \
             patch("src.dapier.auth.session._audit_event") as audit_event:
            if method == "GET":
                response = admin_routes.list_poll_triggers(event)
            elif method == "PUT":
                response = admin_routes.save_poll_trigger(event, "op@example.test")
            else:
                response = admin_routes.delete_poll_trigger(event, "op@example.test")
        return response, audit_event

    def test_save_persists_and_audits_created(self):
        response, audit_event = self.route("PUT", body=trigger_body())

        self.assertEqual(response["statusCode"], 200)
        self.assertIn("drive-updates", self.polls.items)
        self.assertEqual(audit_event.call_args[0],
                         ("drive-updates", "poll-trigger.save", "op@example.test"))
        self.assertEqual(audit_event.call_args[1], {"outcome": "created"})

    def test_save_rejects_malformed_json_without_storing(self):
        response, _ = self.route("PUT", body="not json")

        self.assertEqual(response["statusCode"], 400)
        self.assertEqual(self.polls.items, {})

    def test_list_returns_stored_polls(self):
        self.stored_trigger()

        response, _ = self.route("GET")

        self.assertEqual(response["statusCode"], 200)
        payload = json.loads(response["body"])
        self.assertEqual([item["poll_id"] for item in payload["polls"]],
                         ["drive-updates"])

    def test_delete_unknown_name_is_404(self):
        response, _ = self.route("DELETE", query={"name": "missing"})

        self.assertEqual(response["statusCode"], 404)

    def test_delete_removes_the_trigger_and_audits(self):
        self.stored_trigger()

        response, audit_event = self.route("DELETE", query={"name": "drive-updates"})

        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(self.polls.items, {})
        self.assertEqual(audit_event.call_args[1], {"outcome": "deleted"})


class WorkerDispatchTests(unittest.TestCase):
    """The worker's EventBridge branch: fire(), notify on failure, re-raise."""

    def test_poll_event_fires_the_trigger(self):
        from src.dapier.engine import worker

        with patch.object(poll_triggers, "fire", return_value={"fired": 1}) as fire:
            result = worker.handler({"trigger": "poll", "poll_id": "drive-updates"}, None)

        fire.assert_called_once_with("drive-updates")
        self.assertEqual(result, {"executed": "drive-updates"})

    def test_poll_failure_notifies_and_reraises(self):
        from src.dapier.engine import worker

        with patch.object(poll_triggers, "fire", side_effect=TriggerError("upstream 500")), \
             patch.object(worker, "notify_failure") as notify:
            with self.assertRaises(TriggerError):
                worker.handler({"trigger": "poll", "poll_id": "drive-updates"}, None)

        notify.assert_called_once()


def test_cli_polls_save_posts_the_file(monkeypatch, tmp_path, capsys):
    from dapier_cli import commands as cli_commands

    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path, body))
        return {"created": True, "poll_id": "drive-updates", "expression": "rate(1 hour)",
                "enabled": True, "rule": "dapier-poll-drive-updates", "method": "GET",
                "url": "https://example.test/list"}

    monkeypatch.setattr(cli_commands.api, "call", fake_call)
    path = tmp_path / "poll.json"
    path.write_text(json.dumps(trigger_body()))

    assert cli_commands.polls_save("https://api.example.test", str(path)) == 0

    assert calls == [("PUT", "/api/agent/poll-triggers", trigger_body())]
    out, _ = capsys.readouterr()
    assert "Created poll trigger 'drive-updates'" in out


def test_cli_polls_save_rejects_invalid_json(monkeypatch, tmp_path, capsys):
    from dapier_cli import commands as cli_commands

    def no_call(*_args, **_kwargs):
        raise AssertionError("the API must not be called")

    monkeypatch.setattr(cli_commands.api, "call", no_call)
    path = tmp_path / "broken.json"
    path.write_text("{oops")

    assert cli_commands.polls_save("https://api.example.test", str(path)) == 2
    out, _ = capsys.readouterr()
    assert out.strip()


def test_cli_polls_list_prints_rows(monkeypatch, capsys):
    from dapier_cli import commands as cli_commands

    monkeypatch.setattr(cli_commands.api, "call", lambda *args, **kwargs: {
        "polls": [{"poll_id": "drive-updates", "expression": "rate(1 hour)",
                   "enabled": True, "url": "https://example.test/list"}],
        "flows": []})

    assert cli_commands.polls_list("https://api.example.test") == 0

    out, _ = capsys.readouterr()
    assert "drive-updates" in out
    assert "enabled" in out


def test_cli_polls_delete_calls_the_api(monkeypatch, capsys):
    from dapier_cli import commands as cli_commands

    calls = []
    monkeypatch.setattr(
        cli_commands.api, "call",
        lambda api_url, method, path, body=None, **kwargs:
            calls.append((method, path)) or {"ok": True})

    assert cli_commands.polls_delete("https://api.example.test", "drive-updates") == 0

    assert calls == [("DELETE", "/api/agent/poll-triggers?name=drive-updates")]
    out, _ = capsys.readouterr()
    assert "Deleted poll trigger 'drive-updates'" in out


if __name__ == "__main__":
    unittest.main()
