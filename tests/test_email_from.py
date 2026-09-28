"""Shared sender list: seed, edits, and the worker gate for SES and Datamailer."""
import json

from src.dapier.api.admin import routes
from src.dapier.engine import worker
from src.dapier.triggers import email_from, email_triggers


SEED = ["alexey.s.grigoriev@gmail.com", "alexey@datatalks.club"]


class Table:
    def __init__(self):
        self.items = {}

    def get_item(self, Key):
        item = self.items.get(Key["id"] if "id" in Key else Key.get("name"))
        return {"Item": dict(item)} if item is not None else {}

    def put_item(self, Item, **_kwargs):
        key = Item.get("id") or Item.get("name") or Item.get("inbox_id")
        self.items[key] = dict(Item)

    def update_item(self, Key, UpdateExpression, ExpressionAttributeNames=None, ExpressionAttributeValues=None, **_):
        key = Key.get("inbox_id") or Key.get("id") or Key.get("name")
        item = self.items[key]
        expr = UpdateExpression
        for placeholder, attr in (ExpressionAttributeNames or {}).items():
            expr = expr.replace(placeholder, attr)
        body = expr.split("SET", 1)[1]
        for assignment in body.split(","):
            left, right = assignment.split("=")
            item[left.strip()] = ExpressionAttributeValues[right.strip()]

    def delete_item(self, Key):
        self.items.pop(Key.get("id") or Key.get("name"), None)

    def scan(self, **_):
        return {"Items": [dict(item) for item in self.items.values()]}


def _bind(monkeypatch):
    table = Table()
    monkeypatch.setenv("EMAIL_FROM_TABLE", "senders")
    monkeypatch.setenv("TRIGGER_EMAIL_DOMAIN", "dtcdev.click")
    monkeypatch.setattr(email_from, "_table", lambda table_ref=None: table_ref or table)
    return table


def test_seed_add_display_name_duplicate_remove_and_empty(monkeypatch):
    _bind(monkeypatch)
    listed = email_from.api_list()[1]["addresses"]
    assert listed == SEED
    added = email_from.api_add("Ada <Ada@Example.com>")[1]
    assert added["added"] is True
    assert added["addresses"][-1] == "ada@example.com"
    again = email_from.api_add("ada@example.com")[1]
    assert again["added"] is False
    assert again["addresses"].count("ada@example.com") == 1
    try:
        email_from.api_add("*@datatalks.club")
    except email_from.FromError as exc:
        assert "wildcard" in str(exc)
    else:
        raise AssertionError("wildcard was stored")
    for address in list(again["addresses"]):
        email_from.api_remove(address)
    assert email_from.api_list()[1]["addresses"] == []
    assert email_from.rejected({
        "connector": "email",
        "data": {"from": "alexey@datatalks.club"},
    })


def test_worker_ignores_a_stranger_and_runs_an_allowed_sender(monkeypatch):
    _bind(monkeypatch)
    ran = []
    monkeypatch.setattr(worker, "execute", lambda event, **kwargs: ran.append(event) or ["flow"])

    def deliver(body):
        ran.clear()
        return worker.handler({"Records": [{
            "messageId": "m",
            "body": json.dumps(body),
        }]}, None)

    stranger = {
        "id": "email:1", "connector": "email", "event": "message.received",
        "data": {"from": "stranger@example.com", "subject": "hi"},
    }
    deliver(stranger)
    assert ran == []

    allowed = dict(stranger, id="email:2", data={
        "from": "Alexey <Alexey.S.Grigoriev@gmail.com>", "subject": "hi"})
    deliver(allowed)
    assert ran and ran[0]["id"] == "email:2"

    # A non-email connector is not filtered by the sender list.
    deliver({
        "id": "hook:1", "connector": "webhook", "event": "received",
        "data": {"from": "stranger@example.com"},
    })
    assert ran and ran[0]["id"] == "hook:1"


def test_datamailer_sns_uses_the_same_sender_list(monkeypatch):
    """Replay the inbound-email v1 shape: sender is {addresses, header}, not a string."""
    from src.dapier.triggers import inbox

    _bind(monkeypatch)
    inbox_table = Table()
    monkeypatch.setenv("TRIGGER_INBOX_TABLE", "inbox")
    monkeypatch.setattr(inbox, "_table", lambda: inbox_table)
    ran = []
    monkeypatch.setattr(worker, "execute", lambda event, **kwargs: ran.append(event) or [])
    message = {
        "contract": "inbound-email",
        "version": 1,
        "event_id": "sns-1",
        "event_type": "email.received",
        "occurred_at": "2026-07-12T10:00:00Z",
        "route": "agent",
        "message_id": "message-1",
        "sender": {
            "addresses": ["alexey@datatalks.club"],
            "header": "Alexey <alexey@datatalks.club>",
        },
        "recipients": {"matched": ["agent@dtcdev.click"]},
        "subject": "Do this",
        "date": "2026-07-12T09:59:00Z",
        "body": {"html": {"value": "<p>Do this</p>"}},
        "attachments": [],
        "raw_mime": {"bucket": "mail", "key": "raw/1"},
    }
    worker.handler({"Records": [{
        "messageId": "m",
        "body": json.dumps({"Type": "Notification", "Message": json.dumps(message)}),
    }]}, None)
    assert ran and ran[0]["connector"] == "email"
    assert ran[0]["data"]["sender"]["addresses"] == ["alexey@datatalks.club"]
    assert inbox_table.items["sns-1"]["status"] != "ignored"

    ran.clear()
    message["sender"] = {"addresses": ["nope@example.com"]}
    message["event_id"] = "sns-2"
    worker.handler({"Records": [{
        "messageId": "m2",
        "body": json.dumps({"Type": "Notification", "Message": json.dumps(message)}),
    }]}, None)
    assert ran == []
    assert inbox_table.items["sns-2"]["status"] == "ignored"
    assert inbox_table.items["sns-2"]["matched"] == []


def test_address_keeps_route_and_ands_subject_and_rejects_from(monkeypatch):
    monkeypatch.setenv("TRIGGER_EMAIL_DOMAIN", "dtcdev.click")
    monkeypatch.setattr(email_triggers, "yaml_email_routes", lambda: set())
    item = email_triggers.build_item({
        "name": "agent",
        "actions": [{"type": "webhook", "url": "https://example.test"}],
        "filters": {"subject": {"contains": "invoice"}},
    }, "op")
    workflow = email_triggers.workflow_for(item)
    assert workflow["trigger"]["filters"]["route"] == {"equals": "agent"}
    assert workflow["trigger"]["filters"]["subject"] == {"contains": "invoice"}
    try:
        email_triggers.build_item({
            "name": "agent",
            "actions": [{"type": "webhook", "url": "https://example.test"}],
            "filters": {"from": {"equals": "a@b.co"}},
        }, "op")
    except email_triggers.TriggerError as exc:
        assert "from" in str(exc)
    else:
        raise AssertionError("from filter was accepted")

    # The console route rejects the same body.
    monkeypatch.setattr(email_triggers, "get_table", lambda table_ref=None: Table())
    response = routes.save_email_trigger({
        "body": json.dumps({
            "name": "agent",
            "actions": [{"type": "webhook", "url": "https://example.test"}],
            "filters": {"from": {"equals": "a@b.co"}},
        }),
    }, "op")
    assert response["statusCode"] == 400
