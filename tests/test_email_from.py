"""Per-address sender policy: seed, edits, and the worker gate for SES and Datamailer."""
import json

import pytest

from src.dapier.api.admin import routes
from src.dapier.auth import session
from src.dapier.engine import worker
from src.dapier.triggers import email_from
from dapier_cli import commands, main


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
            "attributes": {"SenderId": "AROAINTAKE:test"}, "body": json.dumps(body),
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
    from test_email_provenance import signed_notification
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
        "attributes": {"SenderId": "AROAINTAKE:test"}, "body": json.dumps(signed_notification(message, monkeypatch)),
    }]}, None)
    assert ran and ran[0]["connector"] == "email"
    assert ran[0]["data"]["sender"]["addresses"] == ["alexey@datatalks.club"]
    assert inbox_table.items["sns-1"]["status"] != "ignored"

    ran.clear()
    message["sender"] = {"addresses": ["nope@example.com"]}
    message["event_id"] = "sns-2"
    worker.handler({"Records": [{
        "messageId": "m2",
        "attributes": {"SenderId": "AROAINTAKE:test"}, "body": json.dumps(signed_notification(message, monkeypatch)),
    }]}, None)
    assert ran == []
    assert inbox_table.items["sns-2"]["status"] == "ignored"
    assert inbox_table.items["sns-2"]["matched"] == []


def test_delivery_feedback_is_not_blocked_by_the_inbound_sender_gate(monkeypatch):
    table = _bind(monkeypatch)
    for address in SEED:
        email_from.api_remove(address)
    for event in ("bounce.received", "complaint.received"):
        assert not email_from.rejected({"connector": "email", "event": event,
                                        "data": {"source": "no-reply@dtcdev.click"}})
    assert email_from.rejected({"connector": "email", "event": "message.received", "data": {}})


def _mail(sender, *, route=None, source=None, to=None, matched=None, event="message.received"):
    data = {"from": sender}
    if route is not None:
        data["route"] = route
    if to is not None:
        data["to"] = to
    if matched is not None:
        data["recipients"] = {"matched": matched}
    payload = {"connector": "email", "event": event, "data": data}
    if source is not None:
        payload["source"] = source
    return payload


def test_get_shape_includes_default_and_only_stored_routes(monkeypatch):
    _bind(monkeypatch)
    payload = email_from.api_list()[1]
    assert payload["default"] == {"mode": "allowlist", "addresses": SEED}
    assert payload["routes"] == {}
    assert payload["addresses"] == SEED
    email_from.api_save({"route": "newsletter", "mode": "anyone"})
    email_from.api_save({"route": "invoice", "address": "ops@example.com"})
    payload = email_from.api_list()[1]
    assert payload["default"]["addresses"] == SEED
    assert payload["addresses"] == SEED
    assert payload["routes"]["newsletter"]["mode"] == "anyone"
    assert payload["routes"]["invoice"]["mode"] == "allowlist"
    assert "ops@example.com" in payload["routes"]["invoice"]["addresses"]
    assert set(SEED).issubset(payload["routes"]["invoice"]["addresses"])
    assert "consulting" not in payload["routes"]


def test_route_anyone_accepts_a_stranger_but_empty_from_is_still_refused(monkeypatch):
    _bind(monkeypatch)
    email_from.api_save({"route": "newsletter", "mode": "anyone"})
    stranger = _mail("stranger@example.com", route="newsletter")
    assert not email_from.rejected(stranger)
    assert email_from.rejected(_mail("stranger@example.com"))
    assert email_from.rejected({"connector": "email", "event": "message.received",
                                "data": {"route": "newsletter"}})


def test_route_allowlist_uses_its_list_not_the_default(monkeypatch):
    table = _bind(monkeypatch)
    table.put_item(Item={"id": "route:invoice", "mode": "allowlist",
                         "addresses": ["only@example.com"]})
    assert not email_from.rejected(_mail("only@example.com", route="invoice"))
    assert email_from.rejected(_mail("alexey@datatalks.club", route="invoice"))
    assert not email_from.rejected(_mail("alexey@datatalks.club"))


def test_ses_like_event_resolves_route_from_source_or_to(monkeypatch):
    _bind(monkeypatch)
    email_from.api_save({"route": "newsletter", "mode": "anyone"})
    assert not email_from.rejected(_mail(
        "stranger@example.com", source="newsletter@dtcdev.click",
        to="newsletter@dtcdev.click"))
    assert not email_from.rejected(_mail(
        "stranger@example.com", to="Newsletter <newsletter@dtcdev.click>"))
    assert not email_from.rejected(_mail(
        "stranger@example.com", matched=["newsletter@dtcdev.click"]))
    assert email_from.rejected(_mail(
        "stranger@example.com", source="invoice@dtcdev.click"))


def test_datamailer_anyone_lets_a_stranger_through(monkeypatch):
    from test_email_provenance import signed_notification
    from src.dapier.triggers import inbox

    _bind(monkeypatch)
    email_from.api_save({"route": "newsletter", "mode": "anyone"})
    inbox_table = Table()
    monkeypatch.setenv("TRIGGER_INBOX_TABLE", "inbox")
    monkeypatch.setattr(inbox, "_table", lambda: inbox_table)
    ran = []
    monkeypatch.setattr(worker, "execute", lambda event, **kwargs: ran.append(event) or [])
    message = {
        "contract": "inbound-email",
        "version": 1,
        "event_id": "sns-news",
        "event_type": "email.received",
        "occurred_at": "2026-07-12T10:00:00Z",
        "route": "newsletter",
        "message_id": "message-n",
        "sender": {"addresses": ["nope@example.com"], "header": "Nope <nope@example.com>"},
        "recipients": {"matched": ["newsletter@dtcdev.click"]},
        "subject": "Issue",
        "date": "2026-07-12T09:59:00Z",
        "body": {"html": {"value": "<p>hi</p>"}},
        "attachments": [],
        "raw_mime": {"bucket": "mail", "key": "raw/n"},
    }
    worker.handler({"Records": [{
        "messageId": "m",
        "attributes": {"SenderId": "AROAINTAKE:test"}, "body": json.dumps(signed_notification(message, monkeypatch)),
    }]}, None)
    assert ran and ran[0]["data"]["route"] == "newsletter"
    assert inbox_table.items["sns-news"]["status"] != "ignored"


def test_post_mode_add_and_delete_with_route(monkeypatch):
    _bind(monkeypatch)
    monkeypatch.setattr(session, "_audit_event", lambda *args, **kwargs: None)
    created = email_from.api_save({"route": "invoice", "mode": "allowlist"})[1]
    assert created["routes"]["invoice"]["addresses"] == SEED
    added = email_from.api_save({"route": "invoice", "address": "Ada <Ada@Example.com>"})[1]
    assert added["added"] is True
    assert "ada@example.com" in added["routes"]["invoice"]["addresses"]
    assert "ada@example.com" not in added["addresses"]
    removed = email_from.api_remove("ada@example.com", route="invoice")[1]
    assert removed["removed"] is True
    assert "ada@example.com" not in removed["routes"]["invoice"]["addresses"]
    anyone = email_from.api_save({"route": "newsletter", "mode": "anyone"})[1]
    assert anyone["routes"]["newsletter"]["mode"] == "anyone"
    assert anyone["routes"]["newsletter"]["addresses"] == []
    flipped = email_from.api_save({"route": "newsletter", "mode": "allowlist"})[1]
    assert flipped["routes"]["newsletter"]["mode"] == "allowlist"
    assert flipped["routes"]["newsletter"]["addresses"] == SEED
    email_from.api_save({"route": "newsletter", "mode": "anyone"})
    with pytest.raises(email_from.FromError, match="accepts anyone"):
        email_from.api_save({"route": "newsletter", "address": "a@b.co"})
    missing = email_from.api_remove("ada@example.com", route="newsletter")[1]
    assert missing["removed"] is False
    with pytest.raises(email_from.FromError, match="unknown route"):
        email_from.api_remove("ada@example.com", route="consulting")
    resp = routes.email_from_add(
        {"attributes": {"SenderId": "AROAINTAKE:test"}, "body": json.dumps({"route": "consulting", "mode": "anyone"})}, "op")
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["routes"]["consulting"]["mode"] == "anyone"
    dropped = routes.email_from_remove(
        {"queryStringParameters": {"route": "invoice", "address": SEED[0]}}, "op")
    assert dropped["statusCode"] == 200
    assert json.loads(dropped["body"])["removed"] is True


def test_invalid_wildcard_anyone_plus_address_and_bad_route(monkeypatch):
    _bind(monkeypatch)
    with pytest.raises(email_from.FromError, match="wildcard"):
        email_from.api_save({"address": "*@datatalks.club"})
    with pytest.raises(email_from.FromError, match="wildcard"):
        email_from.api_save({"route": "invoice", "address": "*@datatalks.club"})
    with pytest.raises(email_from.FromError, match="anyone does not take"):
        email_from.api_save({"route": "newsletter", "mode": "anyone", "address": "a@b.co"})
    with pytest.raises(email_from.FromError, match="2-32"):
        email_from.api_save({"route": "X", "mode": "anyone"})
    with pytest.raises(email_from.FromError, match="named address"):
        email_from.api_save({"mode": "anyone"})


def test_missing_table_env_leaves_the_gate_off(monkeypatch):
    monkeypatch.delenv("EMAIL_FROM_TABLE", raising=False)
    assert not email_from.rejected(_mail("stranger@example.com"))


def test_legacy_row_without_mode_is_allowlist(monkeypatch):
    table = _bind(monkeypatch)
    table.put_item(Item={"id": "from-allow", "addresses": ["only@example.com"]})
    payload = email_from.api_list()[1]
    assert payload["default"]["mode"] == "allowlist"
    assert payload["default"]["addresses"] == ["only@example.com"]
    assert not email_from.rejected(_mail("only@example.com"))
    assert email_from.rejected(_mail("alexey@datatalks.club"))


def test_cli_emails_from_list_add_remove_and_mode(monkeypatch, capsys):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path, body))
        if method == "GET":
            return {
                "default": {"mode": "allowlist", "addresses": SEED},
                "routes": {
                    "newsletter": {"mode": "anyone", "addresses": []},
                    "invoice": {"mode": "allowlist", "addresses": ["ops@example.com"]},
                },
                "addresses": SEED,
            }
        if method == "DELETE":
            return {"removed": True, "addresses": SEED, "default": {"mode": "allowlist", "addresses": SEED},
                    "routes": {}}
        return {"added": True, "addresses": SEED, "default": {"mode": "allowlist", "addresses": SEED},
                "routes": {"newsletter": {"mode": "anyone", "addresses": []}}}

    monkeypatch.setattr(commands.api, "call", fake_call)
    parser = main.build_parser()
    from_help = [action for action in parser._subparsers._actions
                 if getattr(action, "choices", None) and "emails" in action.choices][0] \
        .choices["emails"].format_help()
    assert "shared sender allow-list for every inbound email" not in from_help
    emails_from = [action for action in parser._subparsers._actions
                   if getattr(action, "choices", None) and "emails" in action.choices][0] \
        .choices["emails"]
    from_sub = [action for action in emails_from._subparsers._actions
                if getattr(action, "choices", None) and "from" in action.choices][0] \
        .choices["from"].format_help()
    assert "anyone" in from_sub and "allowlist" in from_sub
    assert "per-address" in from_sub.lower() or "per address" in from_sub.lower() \
        or "named address" in from_sub.lower() or "inbound address" in from_sub.lower()

    assert main.main(["emails", "from", "list"]) == 0
    out = capsys.readouterr().out
    assert "default (allowlist)" in out
    assert "alexey@datatalks.club" in out
    assert "newsletter (anyone)" in out
    assert "invoice (allowlist)" in out
    assert "ops@example.com" in out

    assert main.main(["emails", "from", "list", "--route", "newsletter@"]) == 0
    assert "newsletter (anyone)" in capsys.readouterr().out

    assert main.main(["emails", "from", "add", "ada@example.com", "--route", "invoice"]) == 0
    assert calls[-1][0] == "POST"
    assert calls[-1][1] == "/api/agent/email-from"
    assert calls[-1][2] == {"address": "ada@example.com", "route": "invoice"}
    assert "Added ada@example.com to invoice." in capsys.readouterr().out

    assert main.main(["emails", "from", "remove", "ada@example.com", "--route", "invoice"]) == 0
    assert calls[-1][0] == "DELETE"
    assert "route=invoice" in calls[-1][1]
    assert "Removed ada@example.com from invoice." in capsys.readouterr().out

    assert main.main(["emails", "from", "anyone", "newsletter"]) == 0
    assert calls[-1][2] == {"route": "newsletter", "mode": "anyone"}
    assert "newsletter now accepts anyone." in capsys.readouterr().out

    assert main.main(["emails", "from", "allowlist", "invoice@dtcdev.click"]) == 0
    assert calls[-1][2] == {"route": "invoice", "mode": "allowlist"}
    assert "invoice now uses an allow-list." in capsys.readouterr().out


@pytest.fixture(autouse=True)
def trusted_queue_publisher(monkeypatch):
    monkeypatch.setenv("EMAIL_PUBLISHER_ROLE_IDS", "AROAINTAKE")
