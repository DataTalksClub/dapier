"""The bookkeeping review surfaces: console routes, agent routes (the
CLI's path), and the CLI noun itself — all over the same store, which is
the point (one domain behavior, three ways in)."""
import json
from unittest.mock import patch

from src.dapier import bookkeeping_store
from src.dapier.api.admin import routes as admin_routes
from src.dapier.api.agent import ops as agent_ops
from tests.test_bookkeeping_store import FakeBookkeepingTable

import pytest

ENTRY = {"provider": "AWS", "invoice_number": "EUINDE26-1", "what": "Cloud services",
         "amount": 70.57, "currency": "USD", "vat": 11.27,
         "period": "September 1 - September 30, 2026", "period_month": "2026-09",
         "account": "experiments"}


def request(method, body=None, query=None):
    return {
        "requestContext": {"http": {"method": method}},
        "queryStringParameters": query,
        "body": json.dumps(body) if body is not None else None,
    }


def body_of(response):
    return json.loads(response["body"])


def staged_table():
    table = FakeBookkeepingTable()
    with patch.object(bookkeeping_store, "_table", return_value=table):
        bookkeeping_store.create_pending(
            entry=ENTRY, source="deterministic", workflow_id="invoice-intake",
            message={"message_id": "<m-1>", "subject": "Your AWS invoice"},
            pdf={"filename": "invoice.pdf"})
    return table


def test_admin_queue_lists_entries_with_counts():
    table = staged_table()
    with patch.object(bookkeeping_store, "_table", return_value=table):
        response = admin_routes.list_bookkeeping(request("GET"))
    assert response["statusCode"] == 200
    payload = body_of(response)
    assert len(payload["entries"]) == 1
    assert payload["counts"]["pending"] == 1
    entry = payload["entries"][0]
    assert entry["entry"]["invoice_number"] == "EUINDE26-1"
    assert entry["message"]["subject"]  # staged entries carry provenance


def test_admin_queue_status_filter_and_bad_status():
    table = staged_table()
    with patch.object(bookkeeping_store, "_table", return_value=table):
        ok = admin_routes.list_bookkeeping(request("GET", query={"status": "pending"}))
        bad = admin_routes.list_bookkeeping(request("GET", query={"status": "later"}))
    assert ok["statusCode"] == 200
    assert bad["statusCode"] == 400


def test_admin_get_one_and_missing():
    table = staged_table()
    with patch.object(bookkeeping_store, "_table", return_value=table):
        listed = body_of(admin_routes.list_bookkeeping(request("GET")))
        entry_id = listed["entries"][0]["entry_id"]
        found = admin_routes.get_bookkeeping(request("GET"), entry_id)
        missing = admin_routes.get_bookkeeping(request("GET"), "nope")
    assert found["statusCode"] == 200
    assert body_of(found)["entry"]["amount"] == 70.57
    assert missing["statusCode"] == 404


def test_admin_confirm_applies_edits_and_audits():
    table = staged_table()
    audits = []
    with patch.object(bookkeeping_store, "_table", return_value=table), \
            patch("src.dapier.api.admin.bookkeeping.session._audit_event",
                  side_effect=lambda *args, **kwargs: audits.append(args)):
        listed = body_of(admin_routes.list_bookkeeping(request("GET")))
        entry_id = listed["entries"][0]["entry_id"]
        response = admin_routes.confirm_bookkeeping(
            request("POST", {"edits": {"amount": 691.13}}), entry_id, "op-subject")
    assert response["statusCode"] == 200
    assert body_of(response)["entry"]["amount"] == 691.13
    assert body_of(response)["confirmed_by"] == "op-subject"
    assert audits == [("bookkeeping", "entry.confirm", "op-subject")]


def test_admin_reject_frees_the_dedup_marker():
    table = staged_table()
    with patch.object(bookkeeping_store, "_table", return_value=table), \
            patch("src.dapier.api.admin.bookkeeping.session._audit_event"):
        listed = body_of(admin_routes.list_bookkeeping(request("GET")))
        entry_id = listed["entries"][0]["entry_id"]
        assert admin_routes.reject_bookkeeping(
            request("POST", {"note": "duplicate"}), entry_id, "op")["statusCode"] == 200
        restaged = bookkeeping_store.create_pending(entry=ENTRY, source="deterministic",
                                                    workflow_id="invoice-intake")
    assert restaged["deduped"] is False


def test_admin_bad_edit_field_is_a_400():
    table = staged_table()
    with patch.object(bookkeeping_store, "_table", return_value=table):
        listed = body_of(admin_routes.list_bookkeeping(request("GET")))
        entry_id = listed["entries"][0]["entry_id"]
        response = admin_routes.confirm_bookkeeping(
            request("POST", {"edits": {"entry_id": "hijack"}}), entry_id, "op")
    assert response["statusCode"] == 400


def test_agent_queue_mirrors_the_console():
    table = staged_table()
    with patch.object(bookkeeping_store, "_table", return_value=table), \
            patch.object(agent_ops, "require_operator",
                         return_value=("op-subject", None)), \
            patch("src.dapier.audit.record"), \
            patch("src.dapier.audit.audit_table", return_value=object()):
        listed = agent_ops.bookkeeping_api(request("GET"))
        entry_id = body_of(listed)["entries"][0]["entry_id"]
        confirmed = agent_ops.bookkeeping_api(
            request("POST", {"edits": {}}), entry_id, action="confirm")
        assert confirmed["statusCode"] == 200
        assert body_of(confirmed)["status"] == "confirmed"
        single = agent_ops.bookkeeping_api(request("GET"), entry_id=entry_id)
        assert body_of(single)["entry_id"] == entry_id


def test_agent_queue_denies_non_operators():
    from src.dapier.api.agent.common import _LateBinding  # the facade seam

    table = staged_table()
    denial = {"statusCode": 403, "body": json.dumps({"error": "Operator only"})}
    with patch.object(bookkeeping_store, "_table", return_value=table), \
            patch.object(agent_ops, "require_operator",
                         return_value=(None, denial)):
        response = agent_ops.bookkeeping_api(request("GET"))
    assert response["statusCode"] == 403


def test_agent_missing_entry_is_a_404():
    table = staged_table()
    with patch.object(bookkeeping_store, "_table", return_value=table), \
            patch.object(agent_ops, "require_operator",
                         return_value=("op-subject", None)):
        response = agent_ops.bookkeeping_api(request("GET"), entry_id="nope")
    assert response["statusCode"] == 404


def test_cli_noun_drives_the_agent_routes(monkeypatch, capsys):
    from dapier_cli import commands, main as cli_main

    table = staged_table()
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path, body))
        # Answer from the real store so the CLI's rendering is exercised.
        with patch.object(bookkeeping_store, "_table", return_value=table):
            if method == "GET" and path == "/api/agent/bookkeeping?status=pending":
                entries = bookkeeping_store.list_entries(status="pending")
                return {"entries": entries, "counts": bookkeeping_store.counts()}
            if method == "GET" and path.startswith("/api/agent/bookkeeping/"):
                entry_id = path.rsplit("/", 1)[-1]
                return bookkeeping_store.get_entry(entry_id)
            if path.endswith("/confirm"):
                entry_id = path.split("/")[-2]
                return bookkeeping_store.confirm_entry(
                    entry_id, edits=body.get("edits") or {}, confirmed_by="cli")
            if path.endswith("/reject"):
                entry_id = path.split("/")[-2]
                return bookkeeping_store.reject_entry(entry_id, rejected_by="cli")
        raise AssertionError(f"unexpected call {method} {path}")

    monkeypatch.setattr(commands.api, "call", fake_call)
    with patch.object(bookkeeping_store, "_table", return_value=table):
        assert cli_main.main(["bookkeeping", "list", "--status", "pending"]) == 0
        entry_id = bookkeeping_store.list_entries()[0]["entry_id"]
        assert cli_main.main(["bookkeeping", "get", entry_id]) == 0
        assert cli_main.main(["bookkeeping", "confirm", entry_id,
                              "--edit", "amount=691.13"]) == 0
        # Reject runs against a fresh pending entry: a confirmed entry is
        # final, which the store enforces.
        bookkeeping_store.create_pending(
            entry={**ENTRY, "invoice_number": "EUINDE26-2"}, source="deterministic",
            workflow_id="invoice-intake")
        second = bookkeeping_store.list_entries(status="pending")[0]["entry_id"]
        assert cli_main.main(["bookkeeping", "reject", second,
                              "--note", "test run"]) == 0
    out = capsys.readouterr().out
    assert "EUINDE26-1" in out
    assert ("POST", f"/api/agent/bookkeeping/{entry_id}/confirm",
            {"edits": {"amount": 691.13}}) in calls
    assert ("POST", f"/api/agent/bookkeeping/{second}/reject",
            {"note": "test run"}) in calls


def test_cli_rejects_malformed_edit_specs(monkeypatch):
    from dapier_cli import commands

    with pytest.raises(SystemExit):
        commands.bookkeeping_confirm("https://api.example.test", "e1",
                                     edits=["amount"])
