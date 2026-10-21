"""Tests verifying the dev-mailbox workflow (email -> Developer bot webhook).

Acceptance criteria:
- mail to dev@dtcdev.click triggers dev-mailbox workflow
- dev-mailbox does NOT trigger an agent step or send agent reports
- dev-mailbox POSTs email details (from, to, subject, date, message-id, text body, attachments)
  to Developer bot webhook as JSON
- Webhook URL and Bearer token are fetched dynamically from storage/config (never hardcoded in YAML)
- Other mailboxes (tofu@) remain unchanged
"""
from io import BytesIO
import json
from pathlib import Path
from unittest.mock import patch
import yaml

from src.dapier.api.designer_store.validation import parse_workflow
from src.dapier.connectors.webhook import run_http_request
from src.dapier.engine.actions.code import run_code
from src.dapier.engine import matching


def test_dev_mailbox_definition():
    wf_path = Path(__file__).parent.parent / "workflows/dev-mailbox.yaml"
    raw_yaml = wf_path.read_text()
    wf = parse_workflow(raw_yaml)

    assert wf["id"] == "dev-mailbox"
    assert wf["enabled"] is True

    # Triggers verify email on route 'dev'
    triggers = wf.get("triggers", [])
    email_trigger = next((t for t in triggers if t.get("connector") == "email"), None)
    assert email_trigger is not None, "dev-mailbox must have an email trigger"
    assert email_trigger["event"] == "message.received"
    assert email_trigger["filters"]["route"]["equals"] == "dev"

    # Actions verify: NO agent step exists
    actions = wf.get("actions", [])
    action_types = [a.get("type") for a in actions]
    assert "agent" not in action_types, "dev-mailbox must not run an agent step"
    assert all("hub intake" not in str(a).lower() for a in actions)

    # Actions must include storage_get for url/token, code format, and http_request
    assert "storage_get" in action_types
    assert "code" in action_types
    assert "http_request" in action_types

    # Ensure no secrets or tokens are committed in the YAML
    assert "crsr_" not in raw_yaml
    assert "https://api2.cursor.sh" not in raw_yaml


def test_dev_mailbox_email_matching():
    wf_path = Path(__file__).parent.parent / "workflows/dev-mailbox.yaml"
    wf = yaml.safe_load(wf_path.read_text())

    event = {
        "connector": "email",
        "event": "message.received",
        "source": "dev",
        "data": {
            "route": "dev",
            "route_address": "dev@dtcdev.click",
            "sender": {"header": "Alexey Grigorev <alexey.s.grigoriev@gmail.com>",
                       "addresses": ["alexey.s.grigoriev@gmail.com"]},
            "recipients": {"to": "dev@dtcdev.click", "addresses": ["dev@dtcdev.click"]},
            "subject": "Ping Developer Bot",
            "message_id": "<dev-ping-1@example.com>",
            "body": {"text": "Hello bot"},
        }
    }
    assert matching.matches(wf, event) is True


def test_dev_mailbox_code_action_payload_formatting():
    wf_path = Path(__file__).parent.parent / "workflows/dev-mailbox.yaml"
    wf = yaml.safe_load(wf_path.read_text())
    code_action = next(a for a in wf["actions"] if a.get("type") == "code")

    event = {
        "connector": "email",
        "data": {
            "from": "alexey.s.grigoriev@gmail.com",
            "to": "dev@dtcdev.click",
            "subject": "Deploy Update",
            "date": "2026-10-20T01:15:00+00:00",
            "message_id": "<deploy-msg-99@mail.test>",
            "body": {
                "text": "Please review the latest build.",
                "html": "<p>Please review the latest build.</p>"
            },
            "attachments": [
                {
                    "filename": "build-log.txt",
                    "content_type": "text/plain",
                    "size": 4096,
                    "checksum": "abc123def456"
                }
            ]
        }
    }

    result = run_code(code_action, event)["result"]
    assert result["from"] == "alexey.s.grigoriev@gmail.com"
    assert result["to"] == "dev@dtcdev.click"
    assert result["subject"] == "Deploy Update"
    assert result["date"] == "2026-10-20T01:15:00+00:00"
    assert result["message_id"] == "<deploy-msg-99@mail.test>"
    assert result["message-id"] == "<deploy-msg-99@mail.test>"
    assert result["text"] == "Please review the latest build."
    assert len(result["attachments"]) == 1
    assert result["attachments"][0]["filename"] == "build-log.txt"
    assert result["attachments"][0]["content_type"] == "text/plain"
    assert result["attachments"][0]["size"] == 4096
    assert result["attachments"][0]["checksum"] == "abc123def456"


def test_dev_mailbox_code_action_with_s3_body_pointer():
    wf_path = Path(__file__).parent.parent / "workflows/dev-mailbox.yaml"
    wf = yaml.safe_load(wf_path.read_text())
    code_action = next(a for a in wf["actions"] if a.get("type") == "code")

    event = {
        "connector": "email",
        "data": {
            "sender": {"header": "Alexey <alexey@datatalks.club>", "addresses": ["alexey@datatalks.club"]},
            "recipients": {"to": "dev@dtcdev.click", "addresses": ["dev@dtcdev.click"]},
            "subject": "SES S3 Pointer Test",
            "date": "2026-10-20T02:00:00Z",
            "message_id": "<s3-msg-1@test>",
            "body": {
                "text": {
                    "content_type": "text/plain",
                    "s3": {"bucket": "inbound-mail", "key": "processed/body.plain"},
                    "size": 32
                }
            },
            "attachments": []
        }
    }

    with patch("boto3.client") as mock_client:
        mock_client.return_value.get_object.return_value = {
            "Body": BytesIO(b"Materialized text content from S3")
        }
        result = run_code(code_action, event)["result"]

    assert result["from"] == "Alexey <alexey@datatalks.club>"
    assert result["to"] == "dev@dtcdev.click"
    assert result["text"] == "Materialized text content from S3"


def test_dev_mailbox_http_request_delivery():
    wf_path = Path(__file__).parent.parent / "workflows/dev-mailbox.yaml"
    wf = yaml.safe_load(wf_path.read_text())
    http_action = next(a for a in wf["actions"] if a.get("type") == "http_request")

    event = {"connector": "email", "data": {}}
    payload = {
        "from": "alexey@datatalks.club",
        "to": "dev@dtcdev.click",
        "subject": "Webhook test",
        "date": "2026-10-20T02:00:00Z",
        "message_id": "<wh-1@test>",
        "message-id": "<wh-1@test>",
        "text": "Hello developer webhook",
        "attachments": []
    }
    steps = {
        "get-url": {"status": "completed", "output": {"value": "https://api2.cursor.sh/automations/webhook/target-id"}},
        "get-token": {"status": "completed", "output": {"value": "crsr_test_secret_token"}},
        "format-email": {"status": "completed", "output": {"result": payload}}
    }

    captured = {}
    def mock_transport(method, url, headers=None, body=None, timeout=None):
        captured["method"] = method
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = body
        return 200, b'{"received": true}', {"content-type": "application/json"}

    res = run_http_request(http_action, event, steps=steps, transport=mock_transport)
    assert res["status"] == 200
    assert res["body"] == {"received": True}
    assert captured["method"] == "POST"
    assert captured["url"] == "https://api2.cursor.sh/automations/webhook/target-id"
    assert captured["headers"]["authorization"] == "Bearer crsr_test_secret_token"
    assert captured["headers"]["content-type"] == "application/json"
    sent_data = json.loads(captured["body"].decode("utf-8"))
    assert sent_data["subject"] == "Webhook test"
    assert sent_data["text"] == "Hello developer webhook"


def test_tofu_mailbox_remains_as_is():
    tofu_path = Path(__file__).parent.parent / "workflows/tofu-mailbox.yaml"
    tofu_wf = yaml.safe_load(tofu_path.read_text())
    assert tofu_wf["id"] == "tofu-mailbox"
    action_types = [a.get("type") for a in tofu_wf.get("actions", [])]
    assert "agent" in action_types


def test_dev_mailbox_guard_missing_storage_keys():
    """Unprovisioned webhook_url/webhook_token fail fast with a clear error.

    Regression test for the EventDeadLetterAlarm: with the storage keys
    never set, storage_get returns "", and without this guard the workflow
    POSTed to an empty URL — every dev@ email failed five redeliveries
    into the DLQ. The guard names the missing keys so the failure notice
    says what to provision.
    """
    import pytest

    from src.dapier.engine.actions.code import run_code

    wf_path = Path(__file__).parent.parent / "workflows/dev-mailbox.yaml"
    wf = yaml.safe_load(wf_path.read_text())
    code_action = next(a for a in wf["actions"] if a.get("type") == "code")

    event = {
        "connector": "email",
        "data": {"from": "a@example.com", "subject": "hi", "body": {"text": "hi"}},
    }
    steps = {
        "get-url": {"status": "completed", "output": {"key": "webhook_url", "value": "", "found": False}},
        "get-token": {"status": "completed", "output": {"key": "webhook_token", "value": "", "found": False}},
    }
    with pytest.raises(RuntimeError, match="webhook_url.*webhook_token"):
        run_code(code_action, event, steps=steps)


def test_dev_mailbox_guard_configured_keys_pass():
    """Provisioned keys let the format step run (guard only fires when set)."""
    from src.dapier.engine.actions.code import run_code

    wf_path = Path(__file__).parent.parent / "workflows/dev-mailbox.yaml"
    wf = yaml.safe_load(wf_path.read_text())
    code_action = next(a for a in wf["actions"] if a.get("type") == "code")

    event = {
        "connector": "email",
        "data": {"from": "a@example.com", "subject": "hi", "body": {"text": "hi"}},
    }
    steps = {
        "get-url": {"status": "completed", "output": {"key": "webhook_url", "value": "https://example.invalid/hook", "found": True}},
        "get-token": {"status": "completed", "output": {"key": "webhook_token", "value": "secret", "found": True}},
    }
    result = run_code(code_action, event, steps=steps)["result"]
    assert result["from"] == "a@example.com"
    assert result["subject"] == "hi"


def test_http_request_empty_url_fails_fast():
    """An unrendered/empty http_request URL raises a clear error, not a
    downstream urllib failure after the event is already mid-flight."""
    import pytest

    wf_path = Path(__file__).parent.parent / "workflows/dev-mailbox.yaml"
    wf = yaml.safe_load(wf_path.read_text())
    http_action = next(a for a in wf["actions"] if a.get("type") == "http_request")

    event = {"connector": "email", "data": {}}
    steps = {
        "get-url": {"status": "completed", "output": {"value": ""}},
        "get-token": {"status": "completed", "output": {"value": ""}},
        "format-email": {"status": "completed", "output": {"result": {}}},
    }
    with pytest.raises(ValueError, match="non-empty url"):
        run_http_request(http_action, event, steps=steps)
