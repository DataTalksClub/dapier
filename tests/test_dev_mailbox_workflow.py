"""Tests verifying the per-bot mailbox workflows (email -> Grok bot webhook).

Acceptance criteria (dev / tofu / orin):
- mail to <bot>@dtcdev.click triggers <bot>-mailbox workflow, enabled
- <bot>-mailbox does NOT run an agent step, sends no email, and has no
  auto-reply (loop-safe: only storage_get / code / http_request)
- <bot>-mailbox POSTs email details (from, to, subject, snippet, text, date,
  message_id, attachments metadata) to its Grok bot webhook as JSON
- Webhook URL and Bearer token are fetched dynamically from this workflow's
  storage (keys webhook_url / webhook_token, never hardcoded in YAML);
  missing keys fail fast with a clear error (no POST to an empty URL)
- Bearer token is accepted with or without a leading "Bearer " prefix
"""
from io import BytesIO
import json
from pathlib import Path
from unittest.mock import patch
import pytest
import yaml

from src.dapier.api.designer_store.validation import parse_workflow
from src.dapier.connectors.webhook import run_http_request
from src.dapier.engine.actions.code import run_code
from src.dapier.engine import matching

MAILBOXES = (
    ("dev-mailbox", "dev", "dev@dtcdev.click"),
    ("tofu-mailbox", "tofu", "tofu@dtcdev.click"),
    ("orin-mailbox", "orin", "orin@dtcdev.click"),
)


def _load(workflow_id):
    wf_path = Path(__file__).parent.parent / f"workflows/{workflow_id}.yaml"
    return wf_path, yaml.safe_load(wf_path.read_text())


@pytest.mark.parametrize("workflow_id,route,address", MAILBOXES)
def test_bot_mailbox_definition(workflow_id, route, address):
    wf_path = Path(__file__).parent.parent / f"workflows/{workflow_id}.yaml"
    raw_yaml = wf_path.read_text()
    wf = parse_workflow(raw_yaml)

    assert wf["id"] == workflow_id
    assert wf["enabled"] is True

    triggers = wf.get("triggers", [])
    email_trigger = next((t for t in triggers if t.get("connector") == "email"), None)
    assert email_trigger is not None, f"{workflow_id} must have an email trigger"
    assert email_trigger["event"] == "message.received"
    assert email_trigger["filters"]["route"]["equals"] == route

    hook_trigger = next((t for t in triggers if t.get("connector") == "webhook"), None)
    assert hook_trigger is not None, f"{workflow_id} must keep its webhook trigger"
    assert hook_trigger["filters"]["hook"]["equals"] == route

    actions = wf.get("actions", [])
    action_types = [a.get("type") for a in actions]
    # Loop-safe: no agent, no email send, no auto-reply.
    assert "agent" not in action_types, f"{workflow_id} must not run an agent step"
    assert all("hub intake" not in str(a).lower() for a in actions)
    assert not any(str(t).startswith("email") and t != "email_content"
                   for t in action_types), f"{workflow_id} must not send email"
    assert "storage_get" in action_types
    assert "code" in action_types
    assert "http_request" in action_types

    storage_keys = sorted(a.get("key") for a in actions if a.get("type") == "storage_get")
    assert storage_keys == ["webhook_token", "webhook_url"]
    assert next(a for a in actions if a.get("key") == "webhook_url")["secret"] is True

    http_action = next(a for a in actions if a.get("type") == "http_request")
    assert http_action["method"] == "POST"
    assert http_action["auth_type"] == "bearer"

    # Ensure no secrets or tokens are committed in the YAML
    assert "crsr_" not in raw_yaml
    assert "https://api2.cursor.sh" not in raw_yaml

    # The fail-fast guard must name this workflow's storage keys.
    code_action = next(a for a in actions if a.get("type") == "code")
    assert workflow_id in code_action["code"]
    assert "webhook_url" in code_action["code"]
    assert "webhook_token" in code_action["code"]


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


@pytest.mark.parametrize("workflow_id,route,address", MAILBOXES)
def test_bot_mailbox_email_matching(workflow_id, route, address):
    _, wf = _load(workflow_id)
    event = {
        "connector": "email",
        "event": "message.received",
        "source": route,
        "data": {
            "route": route,
            "route_address": address,
            "sender": {"header": "Alexey Grigorev <alexey.s.grigoriev@gmail.com>",
                       "addresses": ["alexey.s.grigoriev@gmail.com"]},
            "recipients": {"to": address, "addresses": [address]},
            "subject": f"Ping {route} bot",
            "message_id": f"<{route}-ping-1@example.com>",
            "body": {"text": "Hello bot"},
        },
    }
    assert matching.matches(wf, event) is True


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


@pytest.mark.parametrize("workflow_id,route,address", MAILBOXES)
def test_bot_mailbox_payload_shape(workflow_id, route, address):
    _, wf = _load(workflow_id)
    code_action = next(a for a in wf["actions"] if a.get("type") == "code")
    long_text = "lorem ipsum dolor sit amet " * 30
    event = {
        "connector": "email",
        "data": {
            "from": "alexey@datatalks.club",
            "to": address,
            "subject": f"Hello {route}",
            "date": "2026-10-20T01:15:00+00:00",
            "message_id": f"<{route}-99@mail.test>",
            "body": {"text": long_text},
            "attachments": [
                {"filename": "a.txt", "content_type": "text/plain",
                 "size": 3, "checksum": "abc"},
            ],
        },
    }
    result = run_code(code_action, event)["result"]
    assert result["from"] == "alexey@datatalks.club"
    assert result["to"] == address
    assert result["subject"] == f"Hello {route}"
    assert result["date"] == "2026-10-20T01:15:00+00:00"
    assert result["message_id"] == f"<{route}-99@mail.test>"
    assert result["message-id"] == f"<{route}-99@mail.test>"
    assert result["text"] == long_text
    assert result["text_body"] == long_text
    assert result["snippet"] == " ".join(long_text.split())[:200]
    assert len(result["snippet"]) <= 200
    assert len(result["attachments"]) == 1
    assert result["attachments"][0]["filename"] == "a.txt"


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
    assert result["snippet"] == "Please review the latest build."
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


@pytest.mark.parametrize("workflow_id", ["dev-mailbox", "tofu-mailbox", "orin-mailbox"])
def test_bot_mailbox_http_request_delivery(workflow_id):
    _, wf = _load(workflow_id)
    http_action = next(a for a in wf["actions"] if a.get("type") == "http_request")

    event = {"connector": "email", "data": {}}
    payload = {
        "from": "alexey@datatalks.club",
        "to": f"{workflow_id.split('-')[0]}@dtcdev.click",
        "subject": "Webhook test",
        "snippet": "Hello webhook",
        "date": "2026-10-20T02:00:00Z",
        "message_id": "<wh-1@test>",
        "message-id": "<wh-1@test>",
        "text": "Hello webhook",
        "text_body": "Hello webhook",
        "attachments": [],
    }
    steps = {
        "get-url": {"status": "completed", "output": {"value": "https://example.invalid/hook"}},
        "get-token": {"status": "completed", "output": {"value": "secret-token"}},
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
    assert captured["url"] == "https://example.invalid/hook"
    assert captured["headers"]["authorization"] == "Bearer secret-token"
    assert captured["headers"]["content-type"] == "application/json"
    sent_data = json.loads(captured["body"].decode("utf-8"))
    assert sent_data["snippet"] == "Hello webhook"
    assert sent_data["text"] == "Hello webhook"


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


@pytest.mark.parametrize("token_value", [
    "secret-token",
    "Bearer secret-token",
    "bearer secret-token",
    "  Bearer secret-token  ",
])
def test_http_request_bearer_accepts_optional_bearer_prefix(token_value):
    _, wf = _load("dev-mailbox")
    http_action = next(a for a in wf["actions"] if a.get("type") == "http_request")
    event = {"connector": "email", "data": {}}
    steps = {
        "get-url": {"status": "completed", "output": {"value": "https://example.invalid/hook"}},
        "get-token": {"status": "completed", "output": {"value": token_value}},
        "format-email": {"status": "completed", "output": {"result": {"hi": 1}}},
    }
    captured = {}
    def mock_transport(method, url, headers=None, body=None, timeout=None):
        captured["headers"] = headers
        return 200, b'{}', {"content-type": "application/json"}
    run_http_request(http_action, event, steps=steps, transport=mock_transport)
    assert captured["headers"]["authorization"] == "Bearer secret-token"


@pytest.mark.parametrize("workflow_id,route", [
    ("dev-mailbox", "dev"),
    ("tofu-mailbox", "tofu"),
    ("orin-mailbox", "orin"),
])
def test_bot_mailbox_guard_missing_storage_keys(workflow_id, route):
    """Unprovisioned webhook_url/webhook_token fail fast with a clear error.

    Regression test for the EventDeadLetterAlarm: with the storage keys
    never set, storage_get returns "", and without this guard the workflow
    POSTed to an empty URL — every mail failed five redeliveries
    into the DLQ. The guard names the missing keys so the failure notice
    says what to provision.
    """
    _, wf = _load(workflow_id)
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
    # The error names this workflow so the operator knows which storage to set.
    try:
        run_code(code_action, event, steps=steps)
    except RuntimeError as exc:
        assert workflow_id in str(exc)


def test_dev_mailbox_guard_missing_storage_keys():
    """Unprovisioned webhook_url/webhook_token fail fast with a clear error.

    Regression test for the EventDeadLetterAlarm: with the storage keys
    never set, storage_get returns "", and without this guard the workflow
    POSTed to an empty URL — every dev@ email failed five redeliveries
    into the DLQ. The guard names the missing keys so the failure notice
    says what to provision.
    """

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


@pytest.mark.parametrize("token", ["Bearer", "Bearer ", "bearer\t"])
def test_empty_bearer_header_fails_before_transport(token):
    def transport(*args, **kwargs):
        pytest.fail("empty bearer credentials must never make a request")
    with pytest.raises(ValueError, match="bearer auth needs"):
        run_http_request({"url": "https://example.invalid/hook", "auth_type": "bearer",
                          "auth_token": token}, {}, transport=transport)
