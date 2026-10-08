"""CLI parity for the console's Emails page: received mail and outcomes.

`dapier emails received` lists what the Emails page lists (the inbox
filtered to email, optionally by outcome), with the same columns: when,
from, to, subject, and what happened. `dapier inbox list` prints the same
outcome words and envelope line for email rows.
"""
import pytest

from dapier_cli import commands, main


@pytest.fixture
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("DAPIER_API_URL", raising=False)
    return tmp_path


ROWS = [
    {"inbox_id": "mail-1", "connector": "email", "status": "matched", "outcome": "handled",
     "received_at": "2026-10-08T15:56:46+00:00", "matched": ["invoice-intake"],
     "runs": [{"workflow": "invoice-intake", "run_id": "invoice-intake:mail-1"}],
     "email": {"from": "Ann <ann@example.com>", "from_address": "ann@example.com",
               "to": ["invoice@mail.example"], "subject": "March invoice"}},
    {"inbox_id": "mail-2", "connector": "email", "status": "ignored", "outcome": "refused",
     "received_at": "2026-10-08T15:00:00+00:00", "matched": [],
     "runs": [], "email": {"from": "spam@x.io", "to": ["todo@mail.example"]}},
    {"inbox_id": "fb-1", "connector": "email", "status": "unmatched", "outcome": "unmatched",
     "received_at": "2026-10-08T14:00:00+00:00", "matched": [], "runs": [],
     "email": {"feedback": "bounce", "recipients": ["gone@x.io"]}},
]


def test_emails_received_lists_the_email_inbox_with_outcomes(isolated_home, monkeypatch, capsys):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path))
        return {"events": ROWS, "paging": {"next": "tok"}}

    monkeypatch.setattr(commands.api, "call", fake_call)

    assert main.main(["emails", "received", "--outcome", "handled,failed"]) == 0
    assert calls == [("GET", "/api/agent/triggers/inbox?limit=25&connector=email"
                             "&outcome=handled%2Cfailed")]
    out = capsys.readouterr().out
    assert "handled by invoice-intake" in out
    assert "from ann@example.com to invoice@mail.example: March invoice" in out
    assert "refused: sender not allowed" in out
    assert "no workflow" in out and "bounce for gone@x.io" in out
    assert "next page: tok" in out


def test_inbox_list_passes_the_outcome_filter(isolated_home, monkeypatch, capsys):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append(path)
        return {"events": ROWS[1:2]}

    monkeypatch.setattr(commands.api, "call", fake_call)

    assert main.main(["inbox", "list", "--connector", "email", "--outcome", "refused"]) == 0
    assert calls == ["/api/agent/triggers/inbox?limit=25&connector=email&outcome=refused"]
    assert "refused: sender not allowed" in capsys.readouterr().out


def test_inbox_show_prints_envelope_runs_and_attachments(isolated_home, monkeypatch, capsys):
    event = dict(ROWS[0], email={**ROWS[0]["email"], "cc": "bob@example.com",
                                 "attachments": [{"name": "inv.pdf", "size": 1234}]},
                 runs=[{"workflow": "invoice-intake", "run_id": "invoice-intake:mail-1",
                        "status": "completed"}])
    monkeypatch.setattr(commands.api, "call", lambda *a, **k: {"event": event})

    assert main.main(["inbox", "show", "mail-1"]) == 0
    out = capsys.readouterr().out
    assert "outcome: handled by invoice-intake" in out
    assert "run: invoice-intake:mail-1 (completed)" in out
    assert "email.subject: March invoice" in out
    assert "email.cc: bob@example.com" in out
    assert "attachment: inv.pdf (1234 bytes)" in out
