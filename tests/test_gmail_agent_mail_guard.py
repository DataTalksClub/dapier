"""Agent-to-agent mail is refused from Alexey's mailboxes (intake a7a0e545)."""
import pytest

from dapier_cli import api
from dapier_cli.commands import gmail

ALEXEY = ["alexey@datatalks.club", "alexey.s.grigoriev@gmail.com"]


def _no_api(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("API must not be called for a refused send")
    monkeypatch.setattr(gmail.api, "call", boom)


@pytest.mark.parametrize("account", ALEXEY + [None])
def test_send_to_agent_inbox_refused(monkeypatch, account):
    _no_api(monkeypatch)
    with pytest.raises(api.ApiError, match="REFUSED"):
        gmail.gmail_send("u", account=account, to="orin@dtcdev.click", subject="x", text="y")


def test_handoff_subject_refused_even_to_other_address(monkeypatch):
    _no_api(monkeypatch)
    with pytest.raises(api.ApiError, match="REFUSED"):
        gmail.gmail_send("u", account=ALEXEY[0], to="a@example.com", subject="[handoff] box", text="y")


def test_draft_create_refused_but_preview_too(monkeypatch):
    _no_api(monkeypatch)
    with pytest.raises(api.ApiError, match="REFUSED"):
        gmail.gmail_draft_create("u", account=ALEXEY[1], to="dev@dtcdev.click", subject="s", text="t", preview=True)


def test_reply_to_agent_thread_refused(monkeypatch):
    calls = []

    def call(url, verb, path, **k):
        calls.append(verb)
        assert verb == "GET"
        return {"from": "alexey@datatalks.club", "to": "orin@dtcdev.click", "subject": "Re: [handoff] x"}
    monkeypatch.setattr(gmail.api, "call", call)
    with pytest.raises(api.ApiError, match="REFUSED"):
        gmail.gmail_reply("u", "M1", account=ALEXEY[0], text="t")
    assert calls == ["GET"]


def test_normal_human_mail_and_agent_accounts_unaffected(monkeypatch):
    monkeypatch.setattr(gmail.api, "call", lambda *a, **k: {"message_id": "m", "thread_id": "t"})
    assert gmail.gmail_send("u", account=ALEXEY[0], to="guest@example.com", subject="Hi", text="t") == 0
    assert gmail.gmail_send("u", account="orin@datatalks.club", to="dev@dtcdev.click", subject="Hi", text="t") == 0
