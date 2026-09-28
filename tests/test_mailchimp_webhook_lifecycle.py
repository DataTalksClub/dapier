"""Mailchimp webhook trigger lifecycle: register on save, unsubscribe on
delete/disable — best-effort on both ends.

The trigger item binds an audience (``list_id``); a save subscribes the
hook URL on that audience with Mailchimp's ``POST /lists/{id}/webhooks``
(after removing any webhook already registered under the same URL) and a
delete or disable unsubscribes it by listing the audience's webhooks and
deleting ours by id. A Mailchimp failure must never fail the local save or
delete: it comes back as a ``warnings`` entry in the response instead.
Non-mailchimp hook kinds make no Mailchimp calls at all.
"""
import base64
import json
import os
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

from dapier_cli import commands as cli_commands
from src.dapier.triggers import hook_triggers


LIST_ID = "abc123"
HOOK_URL = "https://dapier.example.test/hooks/mailchimp/newsletter"
EVENTS = list(hook_triggers.MAILCHIMP_EVENT_TYPES)


class StubTable:
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


class FakeMailchimp:
    """The audience-webhook endpoints the lifecycle drives, recording calls."""

    def __init__(self, *, webhooks=None, register_status=200, delete_status=204,
                 outage=False):
        self.calls = []
        self.webhooks = list(webhooks or [])
        self.register_status = register_status
        self.delete_status = delete_status
        self.outage = outage

    def __call__(self, method, url, headers=None, body=None, timeout=15):
        self.calls.append((method, url, json.loads(body) if body else None))
        assert (headers or {}).get("authorization", "").startswith("Basic ")
        if self.outage:
            raise OSError("connection reset")
        path = url.split(".api.mailchimp.com/3.0", 1)[-1]
        prefix = f"/lists/{LIST_ID}/webhooks"
        if method == "GET" and path == prefix:
            return 200, json.dumps({"webhooks": self.webhooks}).encode()
        if method == "POST" and path == prefix:
            if self.register_status >= 300:
                return self.register_status, b'{"title": "Bad Request", "detail": "nope"}'
            return self.register_status, b'{"id": "wh-new"}'
        if method == "DELETE" and path.startswith(prefix + "/"):
            if self.delete_status >= 300:
                return self.delete_status, b'{"title": "Internal Error"}'
            return self.delete_status, b""
        raise AssertionError(f"unexpected mailchimp call {method} {path}")

    def methods(self):
        return [method for method, _url, _body in self.calls]

    def paths(self):
        return [url.split(".api.mailchimp.com/3.0", 1)[-1]
                for _method, url, _body in self.calls]


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {
            "HOOK_TRIGGERS_TABLE": "hooks",
            "HOOKS_BASE_URL": "https://dapier.example.test",
        })
        env.start()
        self.addCleanup(env.stop)
        credential = patch("src.dapier.connections.credentials.get_credential",
                           return_value={"apiKey": "abc123-us12"})
        credential.start()
        self.addCleanup(credential.stop)

    def api_save(self, body, transport, table=None):
        return hook_triggers.api_save(body, "op", "mailchimp",
                                      table_ref=table or StubTable(),
                                      transport=transport)

    def body(self, **overrides):
        return {"name": "newsletter", "kind": "mailchimp", "list_id": LIST_ID,
                "actions": [{"type": "webhook", "url": "https://hooks.test/x"}],
                **overrides}

    def test_save_registers_the_webhook_on_the_audience(self):
        transport = FakeMailchimp()
        status, payload = self.api_save(self.body(), transport)
        self.assertEqual(status, 200)
        self.assertNotIn("warnings", payload)
        self.assertEqual(payload["url"], HOOK_URL)
        self.assertEqual(payload["list_id"], LIST_ID)
        self.assertEqual(payload["events"], EVENTS)
        methods = transport.methods()
        self.assertEqual(methods, ["GET", "POST"])
        self.assertIn(f"/lists/{LIST_ID}/webhooks", transport.paths()[0])
        self.assertIn(f"/lists/{LIST_ID}/webhooks", transport.paths()[1])
        _method, _url, registered = transport.calls[1]
        self.assertEqual(registered["url"], HOOK_URL)
        self.assertEqual(registered["events"],
                         {name: True for name in EVENTS})

    def test_save_subscribes_only_the_configured_events(self):
        transport = FakeMailchimp()
        self.api_save(self.body(events=["subscribe", "campaign"]), transport)
        _method, _url, registered = transport.calls[-1]
        self.assertEqual(registered["events"],
                         {name: name in ("subscribe", "campaign") for name in EVENTS})

    def test_register_failure_still_saves_with_a_warning(self):
        transport = FakeMailchimp(register_status=400)
        table = StubTable()
        status, payload = self.api_save(self.body(), transport, table=table)
        self.assertEqual(status, 200)
        self.assertEqual([w for w in payload["warnings"] if "Mailchimp" in w],
                         payload["warnings"])
        self.assertIn("registration failed", payload["warnings"][0])
        # The trigger is stored anyway: it is live for local merging and can
        # be re-saved (healing the registration) once Mailchimp answers.
        self.assertIn("newsletter", table.items)
        self.assertEqual(table.items["newsletter"]["list_id"], LIST_ID)

    def test_register_outage_still_saves_with_a_warning(self):
        transport = FakeMailchimp(outage=True)
        table = StubTable()
        status, payload = self.api_save(self.body(), transport, table=table)
        self.assertEqual(status, 200)
        self.assertTrue(payload["warnings"])
        self.assertIn("newsletter", table.items)

    def test_re_save_replaces_the_webhook_registered_under_its_url(self):
        transport = FakeMailchimp()
        self.api_save(self.body(), transport, table=StubTable())
        transport.calls.clear()
        # A previous registration of the same URL is still on the audience.
        transport.webhooks = [{"id": "wh-old", "url": HOOK_URL}]
        status, payload = self.api_save(self.body(description="later"), transport)
        self.assertEqual(status, 200)
        self.assertNotIn("warnings", payload)
        self.assertEqual(transport.methods(), ["GET", "DELETE", "POST"])
        self.assertIn(f"/lists/{LIST_ID}/webhooks/wh-old", transport.paths()[1])

    def test_disable_releases_the_subscription(self):
        table = StubTable()
        transport = FakeMailchimp()
        self.api_save(self.body(), transport, table=table)
        transport.calls.clear()
        transport.webhooks = [{"id": "wh-old", "url": HOOK_URL}]
        status, _payload = self.api_save(self.body(enabled=False), transport, table=table)
        self.assertEqual(status, 200)
        self.assertIn("DELETE", transport.methods())
        self.assertNotIn("POST", transport.methods())
        self.assertFalse(table.items["newsletter"]["enabled"])

    def test_delete_unsubscribes_the_webhook_found_by_url(self):
        transport = FakeMailchimp(
            webhooks=[{"id": "wh-9", "url": "https://other.example.test/x"},
                      {"id": "wh-1", "url": HOOK_URL}])
        table = StubTable([{
            "hook_id": "newsletter", "kind": "mailchimp", "url": HOOK_URL,
            "token": "t", "list_id": LIST_ID, "events": EVENTS,
            "actions": [], "enabled": True,
        }])
        status, payload = hook_triggers.api_delete("newsletter", "op", kind="mailchimp",
                                                   table_ref=table, transport=transport)
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertNotIn("warnings", payload)
        self.assertIn(f"/lists/{LIST_ID}/webhooks/wh-1", transport.paths())
        self.assertNotIn(f"/lists/{LIST_ID}/webhooks/wh-9", transport.paths())
        self.assertEqual(table.items, {})

    def test_delete_without_a_matching_remote_webhook_is_a_clean_noop(self):
        transport = FakeMailchimp(
            webhooks=[{"id": "wh-9", "url": "https://other.example.test/x"}])
        table = StubTable([{
            "hook_id": "newsletter", "kind": "mailchimp", "url": HOOK_URL,
            "token": "t", "list_id": LIST_ID, "events": EVENTS,
            "actions": [], "enabled": True,
        }])
        status, payload = hook_triggers.api_delete("newsletter", "op", kind="mailchimp",
                                                   table_ref=table, transport=transport)
        self.assertEqual(status, 200)
        self.assertNotIn("warnings", payload)
        self.assertNotIn("DELETE", transport.methods())
        self.assertEqual(table.items, {})

    def test_unsubscribe_failure_is_best_effort(self):
        transport = FakeMailchimp(delete_status=500,
                                  webhooks=[{"id": "wh-1", "url": HOOK_URL}])
        table = StubTable([{
            "hook_id": "newsletter", "kind": "mailchimp", "url": HOOK_URL,
            "token": "t", "list_id": LIST_ID, "events": EVENTS,
            "actions": [], "enabled": True,
        }])
        status, payload = hook_triggers.api_delete("newsletter", "op", kind="mailchimp",
                                                   table_ref=table, transport=transport)
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertIn("removal failed", payload["warnings"][0])
        # The trigger is gone locally either way.
        self.assertEqual(table.items, {})

    def test_unsubscribe_outage_is_best_effort(self):
        transport = FakeMailchimp(outage=True)
        table = StubTable([{
            "hook_id": "newsletter", "kind": "mailchimp", "url": HOOK_URL,
            "token": "t", "list_id": LIST_ID, "events": EVENTS,
            "actions": [], "enabled": True,
        }])
        status, payload = hook_triggers.api_delete("newsletter", "op", kind="mailchimp",
                                                   table_ref=table, transport=transport)
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["warnings"])
        self.assertEqual(table.items, {})

    def test_missing_stored_key_warns_but_saves(self):
        transport = FakeMailchimp()
        with patch("src.dapier.connections.credentials.get_credential",
                   side_effect=KeyError("mailchimp")):
            status, payload = self.api_save(self.body(), transport)
        self.assertEqual(status, 200)
        self.assertTrue(payload["warnings"])
        self.assertEqual(transport.calls, [])


class NonMailchimpHooksTests(unittest.TestCase):
    """Webhook triggers are untouched by the Mailchimp lifecycle."""

    def setUp(self):
        env = patch.dict(os.environ, {
            "HOOK_TRIGGERS_TABLE": "hooks",
            "HOOKS_BASE_URL": "https://dapier.example.test",
        })
        env.start()
        self.addCleanup(env.stop)

    def test_webhook_save_and_delete_make_no_mailchimp_calls(self):
        transport = FakeMailchimp()
        table = StubTable()
        body = {"name": "orders",
                "actions": [{"type": "webhook", "url": "https://hooks.test/x"}]}
        status, payload = hook_triggers.api_save(body, "op", table_ref=table,
                                                 transport=transport)
        self.assertEqual(status, 200)
        self.assertNotIn("warnings", payload)
        self.assertEqual(payload["kind"], "webhook")
        status, payload = hook_triggers.api_delete("orders", "op", table_ref=table,
                                                   transport=transport)
        self.assertEqual(status, 200)
        self.assertNotIn("warnings", payload)
        self.assertEqual(transport.calls, [])
        self.assertEqual(table.items, {})


class RegistrationRequestTests(unittest.TestCase):
    """The wire contract of the registration itself: the datacenter comes
    from the stored key's suffix, the key rides the basic-auth header, and
    every source is subscribed (api-sourced changes — like the upsert
    action — must fire too)."""

    def setUp(self):
        env = patch.dict(os.environ, {
            "HOOK_TRIGGERS_TABLE": "hooks",
            "HOOKS_BASE_URL": "https://dapier.example.test",
        })
        env.start()
        self.addCleanup(env.stop)
        credential = patch("src.dapier.connections.credentials.get_credential",
                           return_value={"apiKey": "key123-us21"})
        credential.start()
        self.addCleanup(credential.stop)
        self.calls = []
        self.transport = self._transport

    def _transport(self, method, url, headers=None, body=None, timeout=15):
        self.calls.append({"method": method, "url": url, "headers": headers or {},
                           "body": json.loads(body) if body else None})
        if method == "GET":
            return 200, json.dumps({"webhooks": []}).encode()
        return 201, json.dumps({"id": "wh-new"}).encode()

    def test_registration_uses_the_keys_datacenter_and_subscribes_every_source(self):
        status, payload = hook_triggers.api_save(
            {"name": "newsletter", "kind": "mailchimp", "list_id": LIST_ID,
             "actions": [{"type": "webhook", "url": "https://hooks.test/x"}]},
            "op", "mailchimp", table_ref=StubTable(), transport=self.transport)
        self.assertEqual(status, 200)
        self.assertNotIn("warnings", payload)
        self.assertEqual([call["method"] for call in self.calls], ["GET", "POST"])
        for call in self.calls:
            self.assertEqual(call["url"],
                             "https://us21.api.mailchimp.com/3.0/lists/abc123/webhooks")
        auth = base64.b64decode(
            self.calls[-1]["headers"]["authorization"].split(" ", 1)[1]).decode()
        self.assertEqual(auth, "anystring:key123-us21")
        registered = self.calls[-1]["body"]
        self.assertEqual(registered["url"], HOOK_URL)
        self.assertEqual(registered["events"], {name: True for name in EVENTS})
        self.assertEqual(registered["sources"], {"user": True, "admin": True, "api": True})


class CliOutputTests(unittest.TestCase):
    """`dapier hooks save/delete` surfaces the managed registration."""

    def _patch_call(self, response):
        return patch("dapier_cli.commands.api.call",
                     lambda api_url, method, path, body=None, **kwargs: response)

    def test_mailchimp_save_prints_the_managed_registration_not_a_curl_hint(self):
        response = {"created": True, "hook_id": "newsletter", "kind": "mailchimp",
                    "url": HOOK_URL, "list_id": LIST_ID, "token": "tok"}
        body = {"name": "newsletter", "kind": "mailchimp", "list_id": LIST_ID}
        with patch("dapier_cli.commands._read_json_file",
                   lambda path: (dict(body), None)), \
             self._patch_call(response), \
             redirect_stdout(StringIO()) as out:
            exit_code = cli_commands.hooks_save("https://api.example.test", "/dev/null")
        self.assertEqual(exit_code, 0)
        printed = out.getvalue()
        self.assertIn(f"Mailchimp delivery URL: {HOOK_URL}", printed)
        self.assertIn(f"Audience: {LIST_ID}", printed)
        self.assertNotIn("curl", printed)

    def test_mailchimp_save_surfaces_a_registration_warning(self):
        response = {"created": True, "hook_id": "newsletter", "kind": "mailchimp",
                    "url": HOOK_URL, "list_id": LIST_ID,
                    "warnings": ["the trigger saved, but Mailchimp webhook "
                                 "registration failed: boom"]}
        body = {"name": "newsletter", "kind": "mailchimp", "list_id": LIST_ID}
        with patch("dapier_cli.commands._read_json_file",
                   lambda path: (dict(body), None)), \
             self._patch_call(response), \
             redirect_stdout(StringIO()) as out:
            exit_code = cli_commands.hooks_save("https://api.example.test", "/dev/null")
        self.assertEqual(exit_code, 0)
        self.assertIn("warning: the trigger saved, but Mailchimp webhook "
                      "registration failed: boom", out.getvalue())

    def test_mailchimp_delete_surfaces_the_warning(self):
        response = {"ok": True, "hook_id": "newsletter", "kind": "mailchimp",
                    "warnings": ["Mailchimp webhook removal failed: boom"]}
        with self._patch_call(response), redirect_stdout(StringIO()) as out:
            exit_code = cli_commands.hooks_delete("https://api.example.test",
                                                  "newsletter", kind="mailchimp")
        self.assertEqual(exit_code, 0)
        self.assertIn("warning: Mailchimp webhook removal failed: boom",
                      out.getvalue())


if __name__ == "__main__":
    unittest.main()
