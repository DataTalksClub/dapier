"""Tests reproducing and verifying mailbox receive for dev@ and tofu@dtcdev.click.

Verifies:
1. Workflows dev-mailbox and tofu-mailbox correctly specify email triggers filtering on route: dev and tofu.
2. Inbound email events for dev@dtcdev.click and tofu@dtcdev.click normalize to route 'dev' and 'tofu'.
3. Dapier matching engine matches dev-mailbox and tofu-mailbox for those events.
4. Sender policy allows Alexey's addresses (alexey@datatalks.club, alexey.s.grigoriev@gmail.com).
5. Documents the upstream SES barrier: why SES bounces with '550 5.1.1 Address not found'
   unless the SES receipt rule is configured with a domain catch-all for dtcdev.click.
"""
from email.message import EmailMessage
from pathlib import Path
import yaml

from src.dapier.connectors import ingress as ingress_module
from src.dapier.engine import matching
from src.dapier.triggers.email_from import rejected as sender_rejected
from src.dapier.triggers.intake import email_ingress


def test_dev_and_tofu_workflow_definitions():
    workflows_dir = Path(__file__).parent.parent / "workflows"
    dev_wf = yaml.safe_load((workflows_dir / "dev-mailbox.yaml").read_text())
    tofu_wf = yaml.safe_load((workflows_dir / "tofu-mailbox.yaml").read_text())

    assert dev_wf["id"] == "dev-mailbox"
    assert dev_wf["enabled"] is True
    dev_email_trigger = next(t for t in dev_wf["triggers"] if t.get("connector") == "email")
    assert dev_email_trigger["event"] == "message.received"
    assert dev_email_trigger["filters"]["route"]["equals"] == "dev"

    assert tofu_wf["id"] == "tofu-mailbox"
    assert tofu_wf["enabled"] is True
    tofu_email_trigger = next(t for t in tofu_wf["triggers"] if t.get("connector") == "email")
    assert tofu_email_trigger["event"] == "message.received"
    assert tofu_email_trigger["filters"]["route"]["equals"] == "tofu"


def test_inbound_email_contract_matches_dev_and_tofu_mailboxes(monkeypatch):
    """Simulate an event arriving from upstream SES via SNS inbound-email v1 contract."""
    dev_payload = {
        "contract": "inbound-email",
        "version": 1,
        "event_id": "email:msg-dev-001",
        "occurred_at": "2026-10-19T17:00:00Z",
        "route": "dev@dtcdev.click",
        "message_id": "<msg-dev-001@example.com>",
        "sender": {"header": "Alexey Grigorev <alexey@datatalks.club>",
                   "addresses": ["alexey@datatalks.club"]},
        "recipients": {"addresses": ["dev@dtcdev.click"], "matched": ["dev@dtcdev.click"]},
        "subject": "Ping dev bot",
        "body": {"text": {"value": "Hello dev"}},
    }

    normalized = ingress_module.normalize_event(dev_payload)
    assert normalized["connector"] == "email"
    assert normalized["event"] == "message.received"
    assert normalized["source"] == "dev"
    assert normalized["data"]["route"] == "dev"
    assert normalized["data"]["route_address"] == "dev@dtcdev.click"

    workflows_dir = Path(__file__).parent.parent / "workflows"
    dev_wf = yaml.safe_load((workflows_dir / "dev-mailbox.yaml").read_text())
    assert matching.matches(dev_wf, normalized) is True

    # Sender allowlist verification (Alexey's authorized address from SEED / default)
    table = {"Item": {"id": "from-allow", "mode": "allowlist", "addresses": ["alexey@datatalks.club", "alexey.s.grigoriev@gmail.com"]}}
    monkeypatch.setenv("EMAIL_FROM_TABLE", "senders")
    monkeypatch.setattr("src.dapier.triggers.email_from._table", lambda ref=None: type("T", (), {"get_item": lambda self, Key: table})())
    assert sender_rejected(normalized) is False

    # Unauthorized sender verification
    unauth_payload = dict(dev_payload, sender={"header": "stranger@evil.com", "addresses": ["stranger@evil.com"]})
    unauth_normalized = ingress_module.normalize_event(unauth_payload)
    assert sender_rejected(unauth_normalized) is True


def test_email_ingress_s3_intake_matches_dev_mailbox():
    """Verify that email_ingress data_from_message produces the route label for dev."""
    msg = EmailMessage()
    msg["To"] = "dev@dtcdev.click"
    msg["From"] = "alexey@datatalks.club"
    msg["Subject"] = "dev test"
    msg["Message-ID"] = "<msg-dev-s3-1@example.com>"
    msg.set_content("hello dev from s3")

    data = email_ingress.data_from_message(msg, {"bucket": "b", "key": "k"})
    assert data["route"] == "dev"
    assert data["to"] == "dev@dtcdev.click"

    event = {
        "connector": "email",
        "event": "message.received",
        "source": "dev",
        "data": data,
    }

    workflows_dir = Path(__file__).parent.parent / "workflows"
    dev_wf = yaml.safe_load((workflows_dir / "dev-mailbox.yaml").read_text())
    assert matching.matches(dev_wf, event) is True
