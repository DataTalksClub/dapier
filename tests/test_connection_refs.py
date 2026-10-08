"""Human connection references: '<service> <account>' resolves to the
internal connection_id across the API and the CLI."""

import json

import pytest

from src.dapier.connections import records, refs

GMAIL = "https://www.googleapis.com/auth/gmail.readonly"
DRIVE = "https://www.googleapis.com/auth/drive.readonly"
DOCS = "https://www.googleapis.com/auth/documents"
CAL = "https://www.googleapis.com/auth/calendar.readonly"


def conn(cid, account, scopes, status="connected", provider="google"):
    return {"connection_id": cid, "provider": provider, "status": status,
            "verified_account_id": account, "granted_scopes": scopes if account else [],
            "scopes": scopes}


ROWS = [
    conn("google-gmail-datatalks", "alexey@datatalks.club", [GMAIL]),
    conn("google-calendar", "alexey.s.grigoriev@gmail.com", [CAL, DRIVE, DOCS]),
    conn("google-sheets", "alexey@datatalks.club", [DRIVE, DOCS]),
    conn("google-drive", None, [DRIVE], status="ready"),
    conn("zoom", "zoom-user", [], provider="zoom"),
]


@pytest.mark.parametrize("ref,expected", [
    ("drive alexey@datatalks.club", "google-sheets"),
    ("drive datatalks", "google-sheets"),
    ("Drive:datatalks", "google-sheets"),
    ("google drive gmail.com", "google-calendar"),
    ("calendar", "google-calendar"),
    ("gmail datatalks", "google-gmail-datatalks"),
    ("zoom", "zoom"),
    ("google-sheets", "google-sheets"),
])
def test_resolves(ref, expected):
    assert refs.resolve(ROWS, ref)["connection_id"] == expected


def test_ambiguous_lists_candidates():
    with pytest.raises(refs.RefError) as exc:
        refs.resolve(ROWS, "docs alexey")
    assert len(exc.value.candidates) == 2


def test_connected_wins_over_unfinished_duplicate():
    # "drive" alone hits two connected grants and one stub: still ambiguous.
    with pytest.raises(refs.RefError):
        refs.resolve(ROWS, "drive")
    rows = [conn("a", "x@y.z", [DRIVE]), conn("b", "x@y.z", [DRIVE], status="revoked")]
    assert refs.resolve(rows, "drive x@y.z")["connection_id"] == "a"


def test_unknown():
    with pytest.raises(refs.RefError) as exc:
        refs.resolve(ROWS, "drive nobody")
    assert not exc.value.candidates
    with pytest.raises(refs.RefError):
        refs.resolve(ROWS, "nonsense words")


def test_public_view_carries_refs():
    view = records.public_view(ROWS[2])
    assert view["refs"] == ["drive alexey@datatalks.club", "docs alexey@datatalks.club"]
    assert records.public_view(ROWS[3])["refs"] == []


def test_new_connection_gets_opaque_id():
    fields = records.validate_new_connection({"provider": "google", "scopes": [DRIVE]})
    assert fields["connection_id"].startswith("google-")
    assert fields["connection_id"] != "google-drive"
    with pytest.raises(records.ConnectionError):
        records.validate_connection_id("resolve")


def test_cli_resolves_reference_before_token_exec(monkeypatch):
    from dapier_cli import commands, main

    calls, seen = [], {}

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append(path)
        return {"connection_id": "google-sheets"}

    def fake_exec(api_url, connection_id, agent, argv, debug=False):
        seen.update(connection_id=connection_id, argv=argv)
        return 0

    monkeypatch.setattr(commands.api, "call", fake_call)
    monkeypatch.setattr(commands, "token_exec", fake_exec)
    assert main.main(["token", "exec", "drive", "datatalks", "--agent", "a",
                      "--", "echo", "hi"]) == 0
    assert calls == ["/api/agent/connections/resolve?ref=drive+datatalks&agent=a"]
    assert seen == {"connection_id": "google-sheets", "argv": ["echo", "hi"]}


def test_cli_prints_candidates_on_ambiguity(monkeypatch):
    from dapier_cli import api, commands

    def fake_call(api_url, method, path, body=None, **kwargs):
        raise api.ApiError("'docs alexey' matches 2 connections", status=409,
                           payload={"candidates": ["docs a", "docs b"]})

    monkeypatch.setattr(commands.api, "call", fake_call)
    with pytest.raises(api.ApiError) as exc:
        commands.resolve_connection_id("https://x", ["docs", "alexey"])
    assert "docs a" in str(exc.value) and "docs b" in str(exc.value)


def test_show_and_resolve_carry_used_in_for_operators(monkeypatch):
    from src.dapier.api.agent import connections as agent_connections
    from src.dapier.triggers import connection_usage

    class Table:
        def scan(self, **kwargs):
            return {"Items": ROWS}

        def get_item(self, Key):
            return {"Item": next(r for r in ROWS if r["connection_id"] == Key["connection_id"])}

    entry = {"ref": "todo-intake", "kind": "workflow", "enabled": True, "where": "step"}
    monkeypatch.setattr(connection_usage, "collect", lambda: {"google-sheets": [entry]})
    import src.dapier.api.agent as agent
    monkeypatch.setattr(agent, "authenticate", lambda event: ("op", None))
    monkeypatch.setattr(agent, "_is_operator", lambda event, subject: True)
    monkeypatch.setattr(agent, "_tables", lambda: (Table(), None))
    monkeypatch.setattr(agent_connections.tokens, "stored_value", lambda cid: {})

    resp = agent_connections.resolve_connection(
        {"queryStringParameters": {"ref": "drive datatalks"}})
    body = json.loads(resp["body"])
    assert body["connection_id"] == "google-sheets"
    assert body["used_in"] == [entry]
    shown = json.loads(agent_connections.show_connection({}, "google-sheets")["body"])
    assert shown["used_in"] == [entry]


class ScanTable:
    def __init__(self, rows):
        self.rows = rows

    def get_item(self, Key):
        item = next((r for r in self.rows if r["connection_id"] == Key["connection_id"]), None)
        return {"Item": item} if item else {}

    def scan(self, **kwargs):
        return {"Items": self.rows}


def test_find_connection_accepts_ids_and_references():
    table = ScanTable(ROWS)
    assert records.find_connection(table, "google-sheets")["connection_id"] == "google-sheets"
    assert records.find_connection(table, "drive datatalks")["connection_id"] == "google-sheets"
    with pytest.raises(LookupError, match="matches 2"):
        records.find_connection(table, "docs alexey")
    # Management lookups stay exact: a reference never lands on a record.
    assert records.get_connection(table, "drive datatalks") is None


def test_actions_resolve_a_flow_reference(monkeypatch):
    from src.dapier.engine.actions import base

    table = ScanTable(ROWS)
    monkeypatch.setenv("CONNECTIONS_TABLE", "t")
    monkeypatch.setattr("boto3.resource", lambda *a, **k: type(
        "R", (), {"Table": lambda self, name: table})())
    assert base._connected_connection("drive alexey@datatalks.club")["connection_id"] == "google-sheets"
    with pytest.raises(ValueError, match="not configured"):
        base._connected_connection("drive nobody")


def test_usage_folds_references_onto_ids():
    from src.dapier.triggers import connection_usage

    entry = {"ref": "todo-intake", "kind": "workflow", "enabled": True, "where": "step"}
    other = {"ref": "backup", "kind": "workflow", "enabled": True, "where": "step"}
    usage = {"drive datatalks": [entry], "google-sheets": [other]}
    rows = [dict(row) for row in ROWS]
    connection_usage.attach(rows, usage)
    sheets = next(r for r in rows if r["connection_id"] == "google-sheets")
    assert sheets["used_in"] == [other, entry]


def test_delete_guard_sees_reference_usage():
    entry = {"ref": "todo-intake", "kind": "workflow", "enabled": True, "where": "step"}
    status, payload = records.api_delete_connection(
        ScanTable([dict(r) for r in ROWS]), "google-sheets",
        usage={"drive alexey@datatalks.club": [entry]})
    assert status == 409 and "todo-intake" in payload["error"]


def test_refs_use_the_human_account_and_disambiguate_duplicates():
    from src.dapier.connections import refs as r

    bot = {"connection_id": "slack", "provider": "slack", "status": "connected",
           "display_name": "DTC Slack workspace", "account_title": "DataTalks.Club",
           "verified_account_id": "T01ATQK62F8"}
    other = {**bot, "connection_id": "slack-automator", "display_name": "Au-Tomator"}
    # Alone, the reference is the readable workspace, not the team id.
    assert r.refs_for(bot) == ["slack DataTalks.Club"]
    rows = r.with_refs([dict(bot), dict(other)])
    assert rows[0]["refs"] == ["slack DataTalks.Club / DTC Slack workspace"]
    assert rows[1]["refs"] == ["slack DataTalks.Club / Au-Tomator"]
    # Each resolves to exactly its own connection; the bare account is ambiguous.
    assert r.resolve([bot, other], rows[1]["refs"][0])["connection_id"] == "slack-automator"
    assert r.resolve([bot, other], "slack au-tomator")["connection_id"] == "slack-automator"
    with pytest.raises(r.RefError) as exc:
        r.resolve([bot, other], "slack DataTalks.Club")
    assert "slack DataTalks.Club / Au-Tomator (connected)" in exc.value.candidates
