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
