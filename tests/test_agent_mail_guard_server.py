"""Agent-to-agent mail is refused server-side from Alexey's mailboxes
(intake a7a0e545): the /api/agent/gmail ops and the workflow gmail_send
runner both resolve the real sender from the Gmail profile and refuse
before anything reaches Gmail, whatever connection label was passed."""
import json

import pytest

from src.dapier.api.agent import gmail_ops
from src.dapier.engine.actions import email as email_actions


# --- canned Gmail over the provider seam -----------------------------------------

ACCOUNTS = {
    "work": "alexey@datatalks.club",
    "personal": "alexey.s.grigoriev@gmail.com",
    "orin": "orin@datatalks.club",
}

GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
GMAIL_MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"


def _connection(cid, address):
    scopes = [GMAIL_SEND_SCOPE, GMAIL_MODIFY_SCOPE]
    return {
        "connection_id": cid,
        "provider": "google",
        "status": "connected",
        "verified_account_id": address,
        "account_title": address.split("@")[0].title(),
        "scopes": list(scopes),
        "granted_scopes": list(scopes),
    }


@pytest.fixture
def gmail_world(monkeypatch):
    """Three Google connections whose profiles carry the real addresses;
    records provider calls so tests can assert Gmail was never asked to send."""
    from src.dapier.api.agent import google_accounts
    from src.dapier.connections import discovery as provider
    from src.dapier.connections import tokens

    calls = []

    def request(method, url, token, payload, *, transport=None):
        calls.append({"method": method, "url": url, "payload": payload})
        if "users/me/profile" in url:
            local = {"token-work": ACCOUNTS["work"], "token-personal": ACCOUNTS["personal"],
                     "token-orin": ACCOUNTS["orin"]}[token]
            return {"emailAddress": local}
        raise AssertionError(f"unexpected provider call {method} {url}")

    def fake_token(connection, **kwargs):
        return f"token-{connection['connection_id']}", {"expires_at": None}

    connections = [_connection(cid, address) for cid, address in ACCOUNTS.items()]
    by_id = {c["connection_id"]: c for c in connections}
    monkeypatch.setattr(google_accounts, "_tables", lambda: (object(), object()))
    monkeypatch.setattr(google_accounts.connections, "_scan_all",
                        lambda table: connections)
    from src.dapier.api import discovery as api_discovery

    monkeypatch.setattr(api_discovery, "_load",
                        lambda cid, table: (by_id[cid], None))
    monkeypatch.setattr(gmail_ops.tokens, "get_access_token", fake_token)
    monkeypatch.setattr(provider, "_request", request)
    return calls


def test_send_to_agent_inbox_refused(gmail_world):
    with pytest.raises(gmail_ops.GmailOpsError) as refused:
        gmail_ops.send("work", to="orin@dtcdev.click", subject="x", text="y")
    assert refused.value.status == 403
    assert "REFUSED" in str(refused.value)
    assert not any("messages/send" in c["url"] for c in gmail_world)


def test_personal_gmail_handoff_subject_refused(gmail_world):
    with pytest.raises(gmail_ops.GmailOpsError) as refused:
        gmail_ops.send("personal", to="friend@example.com",
                       subject="[handoff] box runbook", text="y")
    assert refused.value.status == 403
    assert not any("messages/send" in c["url"] for c in gmail_world)


def test_reply_resolving_agent_recipients_refused(monkeypatch, gmail_world):
    from src.dapier.connections import discovery as provider

    original = {
        "id": "m1", "threadId": "t1",
        "payload": {"headers": [
            {"name": "From", "value": "orin@dtcdev.click"},
            {"name": "Subject", "value": "[handoff] run on the box"},
            {"name": "Message-ID", "value": "<orig@x>"},
        ]},
    }

    def request(method, url, token, payload, *, transport=None):
        if "users/me/profile" in url:
            return {"emailAddress": ACCOUNTS["work"]}
        if "/users/me/messages/m1" in url:
            return original
        raise AssertionError(f"unexpected provider call {method} {url}")

    monkeypatch.setattr(provider, "_request", request)
    with pytest.raises(gmail_ops.GmailOpsError) as refused:
        gmail_ops.reply("work", "m1", text="nudge")
    assert refused.value.status == 403
    assert "REFUSED" in str(refused.value)
    assert not any("messages/send" in c["url"] for c in gmail_world)


def test_send_draft_of_handoff_content_refused(monkeypatch, gmail_world):
    from src.dapier.connections import discovery as provider

    stored = {"id": "d1", "message": {
        "payload": {"headers": [
            {"name": "To", "value": "orin@dtcdev.click"},
            {"name": "Subject", "value": "Re: [handoff] run on the box"},
        ]}}}

    def request(method, url, token, payload, *, transport=None):
        if "users/me/profile" in url:
            return {"emailAddress": ACCOUNTS["work"]}
        if "/users/me/drafts/d1" in url and method == "GET":
            return stored
        raise AssertionError(f"unexpected provider call {method} {url}")

    monkeypatch.setattr(provider, "_request", request)
    with pytest.raises(gmail_ops.GmailOpsError) as refused:
        gmail_ops.send_draft("work", "d1")
    assert refused.value.status == 403
    assert "REFUSED" in str(refused.value)
    assert not any("drafts/send" in c["url"] for c in gmail_world)


def test_business_mail_from_alexey_still_sends(gmail_world, monkeypatch):
    from src.dapier.connections import discovery as provider

    monkeypatch.setattr(
        gmail_ops.provider, "_request",
        lambda method, url, token, payload, *, transport=None: (
            {"emailAddress": ACCOUNTS["work"]}
            if "users/me/profile" in url else
            {"id": "sent-ok", "threadId": "t"}))
    result = gmail_ops.send("work", to="guest@example.com", subject="Sponsor reply", text="hi")
    assert result["message_id"] == "sent-ok"


def test_agent_account_sending_to_agents_unaffected(gmail_world, monkeypatch):
    monkeypatch.setattr(
        gmail_ops.provider, "_request",
        lambda method, url, token, payload, *, transport=None: (
            {"emailAddress": ACCOUNTS["orin"]}
            if "users/me/profile" in url else
            {"id": "sent-2", "threadId": "t"}))
    result = gmail_ops.send("orin", to="dev@dtcdev.click", subject="hi", text="t")
    assert result["message_id"] == "sent-2"


# --- the guard itself ------------------------------------------------------------

def test_guard_cases():
    assert email_actions._is_alexey_address("Alexey@DataTalks.Club")
    assert email_actions._is_alexey_address("alexey.s.grigoriev@gmail.com")
    assert not email_actions._is_alexey_address("orin@datatalks.club")
    assert not email_actions._is_alexey_address("")
    with pytest.raises(email_actions.AgentMailRefused):
        email_actions.refuse_agent_mail("alexey@datatalks.club", ["dev@dtcdev.click"], "hi")
    with pytest.raises(email_actions.AgentMailRefused):
        email_actions.refuse_agent_mail("alexey@datatalks.club", ["a@b.c"], "[HANDOFF] x")
    email_actions.refuse_agent_mail("alexey@datatalks.club", ["a@b.c"], "sponsor deal")


# --- the workflow gmail_send runner ----------------------------------------------

def _runner_transport(profile_address, sent_ok=True):
    def transport(method, url, *, headers=None, body=None, timeout=15):
        if "users/me/profile" in url:
            return 200, json.dumps({"emailAddress": profile_address}).encode()
        if "messages/send" in url:
            if not sent_ok:
                return 500, b"{}"
            return 200, json.dumps({"id": "sent-r", "threadId": "t"}).encode()
        raise AssertionError(f"unexpected gmail call {method} {url}")
    return transport


def _run_send(monkeypatch, transport, sender_is_connected=True):
    from plugins.google.runners.gmail import run_gmail_send
    from src.dapier.connections import tokens

    if sender_is_connected:
        monkeypatch.setattr("plugins.google.runners.gmail._gmail_connection",
                            lambda connection_id: {"connection_id": connection_id})
        monkeypatch.setattr(tokens, "get_access_token",
                            lambda connection, transport=None: ("tok", {}))
    return run_gmail_send(
        {"type": "gmail_send", "connection_id": "google", "to": "orin@dtcdev.click",
         "subject": "[handoff] copy", "text": "runbook"},
        {"data": {}}, transport=transport)


def test_runner_refuses_alexey_sender(monkeypatch):
    with pytest.raises(RuntimeError, match="REFUSED"):
        _run_send(monkeypatch, _runner_transport("alexey@datatalks.club"))


def test_runner_allows_agent_sender(monkeypatch):
    output = _run_send(monkeypatch, _runner_transport("orin@datatalks.club"))
    assert output["message_id"] == "sent-r"
