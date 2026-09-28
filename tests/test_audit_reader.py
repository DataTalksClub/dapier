"""The audit read surface: the shared domain list (audit.api_recent), the
CSV export (audit.api_export), both operator routes, and the CLI commands.

The audit module already covered writes; these cover the reader — filters,
paging, projection, export, and the parity wiring (admin + agent routes over
one domain function, CLI as its thin client).
"""
import json
import time
from datetime import datetime, timezone

import boto3

from dapier_cli import commands, main
from src.dapier import audit
from src.dapier.api import admin
from src.dapier.api import agent as agent_api
from src.dapier.auth import session


class FakeAuditTable:
    """Scan over stored items, handing out fixed-size pages like DynamoDB."""

    def __init__(self, items, page_size=None):
        self.items = list(items)
        self.page_size = page_size
        self.written = []

    def scan(self, **kwargs):
        items = self.items
        if self.page_size:
            start = (kwargs.get("ExclusiveStartKey") or {}).get("index", 0)
            end = start + self.page_size
            response = {"Items": items[start:end]}
            if end < len(items):
                response["LastEvaluatedKey"] = {"index": end}
            return response
        return {"Items": list(items)}

    def put_item(self, **kwargs):
        item = kwargs["Item"]
        self.written.append(item)
        self.items.append(item)


def _event(connection, action, *, actor="op-1", outcome="ok", agent=None,
           error=None, at="2026-09-20T20:00:00+00:00", serial=0):
    epoch = int(datetime.fromisoformat(at).timestamp())
    return {
        "audit_id": f"{connection}#{epoch}#{serial:012d}",
        "connection_id": connection,
        "action": action,
        "actor_subject": actor,
        "agent": agent,
        "outcome": outcome,
        "timestamp": at,
        "expires_at": epoch + 90 * 86400,
        **({"error": error} if error else {}),
    }


def _ids(payload):
    return [event["audit_id"] for event in payload["events"]]


# --- domain: list, filters, projection --------------------------------------


def test_api_list_orders_newest_first_and_projects_fields(monkeypatch):
    monkeypatch.delenv("AUDIT_TABLE", raising=False)
    rows = [
        _event("conn-a", "connect", at="2026-09-20T20:00:00+00:00", serial=1),
        _event("conn-b", "workflow.save", at="2026-09-20T20:00:01+00:00", serial=2),
        _event("conn-a", "token", at="2026-09-20T20:00:00+00:00", serial=3),
    ]
    _, payload = audit.api_recent(table=FakeAuditTable(rows))

    assert [event["actor_subject"] for event in payload["events"]] == ["op-1", "op-1", "op-1"]
    assert payload["events"][0]["action"] == "workflow.save"  # newest second wins on timestamp
    first = payload["events"][0]
    assert set(first) <= set(audit.AUDIT_FIELDS)
    assert "expires_at" not in first
    assert payload["paging"]["filtered"] is False


def test_api_list_filters_by_action_exactly_or_as_family(monkeypatch):
    monkeypatch.delenv("AUDIT_TABLE", raising=False)
    rows = [
        _event("conn-a", "workflow.save", serial=1),
        _event("conn-a", "workflow.test", serial=2),
        _event("conn-a", "connect", serial=3),
    ]
    table = FakeAuditTable(rows)

    _, payload = audit.api_recent(action="connect", table=table)
    assert [event["action"] for event in payload["events"]] == ["connect"]

    _, payload = audit.api_recent(action="workflow.", table=table)
    assert sorted(event["action"] for event in payload["events"]) == ["workflow.save", "workflow.test"]


def test_api_list_filters_by_connection_actor_agent_and_outcome(monkeypatch):
    monkeypatch.delenv("AUDIT_TABLE", raising=False)
    rows = [
        _event("conn-a", "token", actor="op-1", outcome="ok", agent="bot-1", serial=1),
        _event("conn-b", "token", actor="op-1", outcome="ok", agent="bot-1", serial=2),
        _event("conn-a", "token", actor="op-2", outcome="ok", agent="bot-1", serial=3),
        _event("conn-a", "token", actor="op-1", outcome="error", agent="bot-1", serial=4),
        _event("conn-a", "token", actor="op-1", outcome="ok", agent="bot-2", serial=5),
    ]
    table = FakeAuditTable(rows)

    _, payload = audit.api_recent(connection_id="conn-a", actor="op-1",
                                  outcome="ok", agent="bot-1", table=table)
    assert _ids(payload) == [rows[0]["audit_id"]]


def test_api_list_window_is_since_inclusive_before_exclusive(monkeypatch):
    monkeypatch.delenv("AUDIT_TABLE", raising=False)
    rows = [
        _event("conn-a", "connect", at="2026-09-20T20:00:00+00:00", serial=1),
        _event("conn-a", "connect", at="2026-09-20T21:00:00+00:00", serial=2),
        _event("conn-a", "connect", at="2026-09-20T22:00:00+00:00", serial=3),
    ]
    table = FakeAuditTable(rows)

    _, payload = audit.api_recent(since="2026-09-20T21:00:00+00:00",
                                  before="2026-09-20T22:00:00+00:00", table=table)
    assert _ids(payload) == [rows[1]["audit_id"]]

    _, payload = audit.api_recent(before="2026-09-21", table=table)
    assert len(payload["events"]) == 3  # date-only before: everything on the 20th


def test_api_list_query_searches_display_fields(monkeypatch):
    monkeypatch.delenv("AUDIT_TABLE", raising=False)
    rows = [
        _event("dropbox-team", "connect", serial=1),
        _event("conn-b", "token", error="refresh failed: expired", serial=2),
        _event("conn-c", "workflow.save", actor="someone-else", serial=3),
    ]
    table = FakeAuditTable(rows)

    _, payload = audit.api_recent(query="dropbox", table=table)
    assert _ids(payload) == [rows[0]["audit_id"]]

    _, payload = audit.api_recent(query="EXPIRED", table=table)  # case-insensitive
    assert _ids(payload) == [rows[1]["audit_id"]]


# --- domain: paging ----------------------------------------------------------


def test_api_list_next_token_pages_strictly_after(monkeypatch):
    monkeypatch.delenv("AUDIT_TABLE", raising=False)
    rows = [
        _event("conn-a", "connect", at="2026-09-20T20:00:00+00:00", serial=1),
        _event("conn-a", "connect", at="2026-09-20T21:00:00+00:00", serial=2),
        _event("conn-a", "connect", at="2026-09-20T22:00:00+00:00", serial=3),
    ]
    _, first = audit.api_recent(limit=1, table=FakeAuditTable(rows))
    assert _ids(first) == [rows[2]["audit_id"]]
    assert first["paging"]["next"]

    _, second = audit.api_recent(limit=1, next_token=first["paging"]["next"],
                                 table=FakeAuditTable(rows))
    assert _ids(second) == [rows[1]["audit_id"]]


def test_api_list_token_breaks_ties_on_audit_id(monkeypatch):
    monkeypatch.delenv("AUDIT_TABLE", raising=False)
    # Same-second rows: the audit_id's serial tail is the tiebreaker.
    rows = [
        _event("conn-a", "connect", at="2026-09-20T20:00:00+00:00", serial=1),
        _event("conn-a", "token", at="2026-09-20T20:00:00+00:00", serial=2),
    ]
    _, first = audit.api_recent(limit=1, table=FakeAuditTable(rows))
    _, second = audit.api_recent(limit=1, next_token=first["paging"]["next"],
                                 table=FakeAuditTable(rows))
    assert _ids(first) == [rows[1]["audit_id"]]
    assert _ids(second) == [rows[0]["audit_id"]]


def test_api_list_rejects_an_invalid_token(monkeypatch):
    monkeypatch.delenv("AUDIT_TABLE", raising=False)
    status, payload = audit.api_recent(next_token="junk", table=FakeAuditTable([]))
    assert status == 400
    assert "page token" in payload["error"]


def test_api_list_degrades_to_an_empty_trail_without_storage(monkeypatch):
    monkeypatch.delenv("AUDIT_TABLE", raising=False)
    status, payload = audit.api_recent()
    assert status == 200
    assert payload["events"] == []


def test_filters_from_query_maps_parameter_spellings(monkeypatch):
    monkeypatch.delenv("AUDIT_TABLE", raising=False)
    picked = audit.filters_from_query({"connection": "conn-a", "q": "dropbox",
                                       "next": "tok", "action": "", "since": "2026-09-20"})
    assert picked == {"connection_id": "conn-a", "query": "dropbox", "since": "2026-09-20"}


# --- domain: CSV export -------------------------------------------------------


def test_api_export_renders_csv_with_header_and_rows(monkeypatch):
    monkeypatch.delenv("AUDIT_TABLE", raising=False)
    rows = [
        _event("conn-a", "connect", outcome="ok", serial=1),
        _event("conn-b", "token", outcome="error", error="boom", serial=2),
    ]
    status, payload = audit.api_export(table=FakeAuditTable(rows), now=1726867200)

    assert status == 200
    assert payload["count"] == 2
    assert payload["truncated"] is False
    stamp = datetime.fromtimestamp(1726867200, timezone.utc)
    assert payload["filename"] == stamp.strftime("dapier-audit-%Y%m%d-%H%M%S.csv")
    lines = payload["csv"].strip().split("\r\n")
    assert lines[0].split(",") == list(audit.CSV_COLUMNS)
    # Same-second rows tie-break on audit_id, so conn-b (higher id) leads.
    assert "conn-b" in lines[1] and "boom" in lines[1]
    assert "conn-a" in lines[2]
    assert "expires_at" not in payload["csv"]


def test_api_export_applies_filters_and_flags_truncation(monkeypatch):
    monkeypatch.delenv("AUDIT_TABLE", raising=False)
    rows = [_event("conn-a", "connect", at=f"2026-09-20T2{i}:00:00+00:00", serial=i)
            for i in range(4)]
    status, payload = audit.api_export(action="connect", max_rows=2,
                                       table=FakeAuditTable(rows), now=1726867200)
    assert status == 200
    assert payload["count"] == 2
    assert payload["truncated"] is True  # 4 matched rows capped at 2


# --- admin route ---------------------------------------------------------------


def _configure_admin(monkeypatch, rows, page_size=None):
    monkeypatch.setattr(session, "_credentials", lambda: {"username": "admin", "password": "pw"})
    monkeypatch.setenv("OPERATOR_EMAILS", "op@datatalks.club")
    cookie = session._sign({"sub": "op@datatalks.club", "subject": "op-sub",
                            "exp": int(time.time()) + 600})
    table = FakeAuditTable(rows, page_size=page_size)

    class Dynamo:
        def Table(self, _name):
            return table

    monkeypatch.setenv("AUDIT_TABLE", "audit")
    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())
    return table, [f"dapier_session={cookie}"]


def _admin_request(path, query=None, cookies=None):
    return {
        "requestContext": {"http": {"method": "GET", "path": path}},
        "headers": {"host": "dapier.example.test", "origin": "https://dapier.example.test"},
        "cookies": cookies or [],
        "queryStringParameters": query,
        "body": None,
    }


def test_admin_audit_route_serves_filters_and_paging(monkeypatch):
    rows = [_event("conn-a", "connect", at=f"2026-09-20T2{i}:00:00+00:00", serial=i)
            for i in range(3)]
    table, cookies = _configure_admin(monkeypatch, rows)

    listed = admin.route(
        _admin_request("/api/admin/audit", query={"action": "connect", "limit": "1"},
                       cookies=cookies),
        "GET", "/api/admin/audit",
    )
    assert listed["statusCode"] == 200
    body = json.loads(listed["body"])
    assert len(body["events"]) == 1
    assert body["paging"]["next"]

    paged = admin.route(
        _admin_request("/api/admin/audit", query={"action": "connect", "limit": "1",
                                                  "next": body["paging"]["next"]},
                       cookies=cookies),
        "GET", "/api/admin/audit",
    )
    assert json.loads(paged["body"])["events"][0]["audit_id"] != body["events"][0]["audit_id"]
    assert table.written == []  # reads are not audited


def test_admin_audit_export_route_returns_csv_and_leaves_a_mark(monkeypatch):
    rows = [_event("conn-a", "connect", serial=1)]
    table, cookies = _configure_admin(monkeypatch, rows)

    exported = admin.route(
        _admin_request("/api/admin/audit/export", query={"action": "connect"},
                       cookies=cookies),
        "GET", "/api/admin/audit/export",
    )
    assert exported["statusCode"] == 200
    body = json.loads(exported["body"])
    assert body["csv"].startswith("timestamp,action,") or body["csv"].startswith(",".join(audit.CSV_COLUMNS))
    assert body["count"] == 1
    # The export itself is audited — bulk reads land in the trail.
    assert [item["action"] for item in table.written] == ["audit.export"]


# --- agent route ----------------------------------------------------------------


def _configure_agent(monkeypatch, rows):
    table = FakeAuditTable(rows)

    class Dynamo:
        def Table(self, _name):
            return table

    monkeypatch.setenv("AUDIT_TABLE", "audit")
    monkeypatch.setenv("OPERATOR_EMAILS", "op@datatalks.club")
    monkeypatch.setenv("AUTH_CLI_CLIENT_ID", "cli-client")
    monkeypatch.delenv("DAPIER_RATE_LIMIT", raising=False)
    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())
    monkeypatch.setattr(agent_api, "verify_id_token",
                        lambda token, audience=None: {"sub": "op-1", "email": "op@datatalks.club"})
    return table


def _bearer_event(query=None):
    return {
        "headers": {"host": "dapier.example.test", "authorization": "Bearer dtc-token"},
        "cookies": [],
        "queryStringParameters": query,
    }


def test_agent_audit_route_serves_the_same_domain_list(monkeypatch):
    _configure_agent(monkeypatch, [
        _event("conn-a", "connect", at="2026-09-20T20:00:00+00:00", serial=1),
        _event("conn-a", "grant", at="2026-09-20T21:00:00+00:00", serial=2),
    ])

    listed = agent_api.route(
        _bearer_event({"connection": "conn-a", "q": "grant"}), "GET", "/api/agent/audit",
    )
    assert listed["statusCode"] == 200
    body = json.loads(listed["body"])
    assert [event["action"] for event in body["events"]] == ["grant"]
    assert body["paging"]["filtered"] is True


def test_agent_audit_route_still_requires_operator(monkeypatch):
    _configure_agent(monkeypatch, [_event("conn-a", "connect")])
    monkeypatch.setenv("OPERATOR_EMAILS", "someone-else@datatalks.club")

    listed = agent_api.route(_bearer_event(), "GET", "/api/agent/audit")
    exported = agent_api.route(_bearer_event(), "GET", "/api/agent/audit/export")

    assert listed["statusCode"] == 403
    assert exported["statusCode"] == 403


def test_agent_audit_export_route_returns_csv(monkeypatch):
    _configure_agent(monkeypatch, [_event("conn-a", "connect", serial=1)])

    exported = agent_api.route(_bearer_event({"max_rows": "10"}), "GET", "/api/agent/audit/export")

    assert exported["statusCode"] == 200
    body = json.loads(exported["body"])
    assert body["count"] == 1
    assert "conn-a" in body["csv"]


# --- CLI --------------------------------------------------------------------------


def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("DAPIER_API_URL", raising=False)
    return tmp_path


def _stub_api(monkeypatch, response):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path))
        return response

    monkeypatch.setattr(commands.api, "call", fake_call)
    return calls


def test_cli_audit_list_forwards_all_filter_flags(monkeypatch, tmp_path):
    _isolated(monkeypatch, tmp_path)
    calls = _stub_api(monkeypatch, {"events": [], "paging": {"next": None, "limit": 50}})

    rc = main.main(["audit", "list", "--connection", "conn-a", "--action", "workflow.",
                    "--actor", "op-1", "--agent", "bot-1", "--outcome", "ok",
                    "--since", "2026-09-01", "--before", "2026-09-30",
                    "--query", "dropbox", "--limit", "10"])

    assert rc == 0
    assert calls == [("GET", "/api/agent/audit?limit=10&connection=conn-a&action=workflow."
                              "&actor=op-1&agent=bot-1&outcome=ok&since=2026-09-01"
                              "&before=2026-09-30&q=dropbox")]


def test_cli_audit_list_prints_rows_and_next_page_footer(monkeypatch, tmp_path, capsys):
    _isolated(monkeypatch, tmp_path)
    _stub_api(monkeypatch, {
        "events": [_event("conn-a", "connect", outcome="ok", agent="bot-1",
                          error="expired", serial=1)],
        "paging": {"next": "tok-2", "limit": 50, "filtered": False},
    })

    rc = main.main(["audit", "list"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "connect" in out and "conn-a" in out
    assert "bot-1" in out and "(expired)" in out
    assert "next page: tok-2" in out


def test_cli_audit_export_writes_the_csv_file(monkeypatch, tmp_path, capsys):
    _isolated(monkeypatch, tmp_path)
    calls = _stub_api(monkeypatch, {
        "filename": "dapier-audit-20240920-200000.csv",
        "count": 1,
        "truncated": False,
        "csv": "timestamp,action\r\n2026-09-20T20:00:00+00:00,connect\r\n",
    })
    out_path = tmp_path / "trail.csv"

    rc = main.main(["audit", "export", "--out", str(out_path),
                    "--query", "dropbox", "--max-rows", "100"])

    assert rc == 0
    assert calls == [("GET", "/api/agent/audit/export?q=dropbox&max_rows=100")]
    assert out_path.read_bytes().endswith(b",connect\r\n")
    assert "1 audit events" in capsys.readouterr().out


def test_cli_audit_export_defaults_to_the_suggested_filename(monkeypatch, tmp_path, capsys):
    _isolated(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)
    _stub_api(monkeypatch, {
        "filename": "dapier-audit-20240920-200000.csv",
        "count": 0,
        "truncated": False,
        "csv": "timestamp,action\r\n",
    })

    rc = main.main(["audit", "export"])

    assert rc == 0
    assert (tmp_path / "dapier-audit-20240920-200000.csv").exists()
