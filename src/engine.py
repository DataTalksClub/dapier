import hashlib
import hmac
import json
import mimetypes
import os
import urllib.request
from functools import lru_cache
from pathlib import Path

import yaml

from .credentials import get_credential

DROPBOX_UPLOAD_URL = "https://content.dropboxapi.com/2/files/upload"
DROPBOX_DOWNLOAD_URL = "https://content.dropboxapi.com/2/files/download"
DROPBOX_DELETE_URL = "https://api.dropboxapi.com/2/files/delete_v2"


@lru_cache
def workflows():
    root = Path(os.environ.get("WORKFLOWS_DIR", Path(__file__).parent.parent / "workflows"))
    return [yaml.safe_load(path.read_text()) for path in sorted(root.glob("*.yaml"))]


def _matches_filter(value, rule):
    text = "" if value is None else str(value)
    if not isinstance(rule, dict):
        return value == rule
    return all({
        "equals": lambda expected: text == str(expected),
        "prefix": lambda expected: text.startswith(str(expected)),
        "suffix": lambda expected: text.endswith(str(expected)),
        "contains": lambda expected: str(expected) in text,
    }[operator](expected) for operator, expected in rule.items())


def matches(workflow, event):
    trigger = workflow["trigger"]
    if not workflow.get("enabled", True):
        return False
    if trigger["connector"] != event.get("connector") or trigger["event"] != event.get("event"):
        return False
    return all(_matches_filter(event.get("data", {}).get(field), rule)
               for field, rule in trigger.get("filters", {}).items())


@lru_cache
def _signing_secret(secret_id):
    import boto3

    secrets = boto3.client("secretsmanager")
    value = secrets.get_secret_value(SecretId=secret_id)["SecretString"]
    try:
        return json.loads(value).get("signing_secret", value)
    except json.JSONDecodeError:
        return value


def run_webhook(action, event):
    body = json.dumps(event, separators=(",", ":"), sort_keys=True).encode()
    headers = {"content-type": "application/json", "user-agent": "dapier/0.1"}
    if action.get("secret_id"):
        digest = hmac.new(_signing_secret(action["secret_id"]).encode(), body, hashlib.sha256).hexdigest()
        headers["x-dapier-signature"] = f"sha256={digest}"
    request = urllib.request.Request(action["url"], data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=action.get("timeout_seconds", 10)) as response:
        if response.status >= 300:
            raise RuntimeError(f"webhook returned HTTP {response.status}")


def _json_request(url, payload, headers=None, timeout=10):
    body = json.dumps(payload, separators=(",", ":")).encode()
    request = urllib.request.Request(
        url,
        data=body,
        headers={"content-type": "application/json", **(headers or {})},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        response_body = response.read()
        if response.status >= 300:
            raise RuntimeError(f"request returned HTTP {response.status}")
        return json.loads(response_body) if response_body else {}


def run_slack(action, event):
    secret = get_credential(action["credential_id"])
    token = secret.get("token") or secret.get("bot_token") or secret.get("SLACK_BOT_TOKEN")
    if not token:
        raise ValueError("Slack secret does not contain a bot token")
    data = event.get("data", {})
    template = action.get("text", "{title}\n{url}")
    text = template.format_map(_SafeFormat(data))
    result = _json_request(
        "https://slack.com/api/chat.postMessage",
        {
            "channel": action["channel"],
            "text": text,
            "unfurl_links": action.get("unfurl_links", True),
            "unfurl_media": action.get("unfurl_media", True),
        },
        headers={"authorization": f"Bearer {token}"},
        timeout=action.get("timeout_seconds", 10),
    )
    if not result.get("ok"):
        raise RuntimeError(f"Slack rejected message: {result.get('error', 'unknown_error')}")


class _SafeFormat(dict):
    def __missing__(self, key):
        return ""


@lru_cache
def secrets_value(secret_id):
    import boto3

    return boto3.client("secretsmanager").get_secret_value(SecretId=secret_id)["SecretString"]


def run_dataops(action, event):
    value = secrets_value(action["auth_secret_id"])
    try:
        parsed = json.loads(value)
        token = parsed.get("token") or parsed.get("api_key")
    except json.JSONDecodeError:
        token = value
    if not token:
        raise ValueError("DataOps secret does not contain a token")
    _json_request(
        action.get("url") or os.environ[action.get("url_env", "DATAOPS_INTAKE_URL")],
        _intake_body(action, event),
        headers={"x-dataops-intake-secret": token},
        timeout=action.get("timeout_seconds", 15),
    )


def _intake_body(action, event):
    if event.get("connector") == "dropbox":
        return _dropbox_intake_body(action, event)
    return _email_intake_body(action, event)


def _dropbox_intake_body(action, event):
    """Build a DataOps intake for a Dropbox file event.

    The intake contract references documents by S3 URI, so the file is
    copied into the artifacts bucket (which the intake can already read
    from the rendered-invoice flow) before the request is built.
    """
    import boto3

    from . import tokens

    data = event.get("data", {})
    path = data.get("path")
    if not path:
        raise ValueError("dropbox intake requires a file path")
    filename = _safe_filename(path.split("/")[-1])
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    connection = _dropbox_connection(action["connection_id"])
    access_token, _info = tokens.get_access_token(connection)
    body = _dropbox_download(access_token, path)
    bucket = os.environ["RENDER_ARTIFACTS_BUCKET"]
    key = f"dropbox/{str(event['id']).replace('/', '_')}/{filename}"
    boto3.client("s3").put_object(
        Bucket=bucket, Key=key, Body=body, ContentType=content_type,
        ServerSideEncryption="AES256",
    )
    return {
        "version": "2026-07-01",
        "messageId": event["id"],
        "recipientRoute": "dropbox-upload",
        "from": "dropbox",
        "subject": filename,
        "receivedAt": event["occurred_at"],
        "documents": [{
            "kind": "dropbox-file",
            "storageUri": f"s3://{bucket}/{key}",
            "filename": filename,
            "contentType": content_type,
            "sizeBytes": len(body),
            "checksum": f"sha256:{data.get('content_hash') or ''}",
        }],
    }


def _email_intake_body(action, event):
    data = event.get("data", {})
    documents = []
    for attachment in data.get("attachments", []):
        ref = attachment.get("s3") or {}
        if ref.get("bucket") and ref.get("key"):
            documents.append({
                "kind": "attachment",
                "storageUri": f"s3://{ref['bucket']}/{ref['key']}",
                "filename": attachment.get("filename") or "attachment",
                "contentType": attachment.get("content_type") or "application/octet-stream",
                "sizeBytes": attachment.get("size", 0),
                "checksum": attachment["checksum"],
            })
    output = data.get("output") or {}
    if output.get("bucket") and output.get("key"):
        documents.append({
            "kind": "rendered-email-pdf",
            "storageUri": f"s3://{output['bucket']}/{output['key']}",
            "filename": action.get("filename", "invoice-email.pdf"),
            "contentType": data.get("content_type", "application/pdf"),
            "sizeBytes": data["size_bytes"],
            "checksum": f"sha256:{data['checksum']}",
        })
    source_data = data.get("source_event", {}).get("data", data)
    sender = source_data.get("sender", {})
    sender_value = (sender.get("addresses") or [sender.get("header") or "unknown@example.com"])[0]
    return {
        "version": "2026-07-01",
        "messageId": data.get("message_id") or source_data["message_id"],
        "recipientRoute": data.get("route") or source_data["route"],
        "from": sender_value,
        "subject": source_data.get("subject") or "Inbound email",
        "receivedAt": source_data.get("date") or event["occurred_at"],
        "documents": documents,
    }


def _safe_filename(name):
    name = str(name or "").replace("\\", "/").split("/")[-1].strip()
    return (name or "file")[:255]


def _dropbox_connection(connection_id):
    import boto3

    from . import connections

    table = boto3.resource("dynamodb").Table(os.environ["CONNECTIONS_TABLE"])
    connection = connections.get_connection(table, connection_id)
    if not connection:
        raise ValueError(f"connection {connection_id} is not configured")
    if connection.get("status") != connections.STATUS_CONNECTED:
        raise ValueError(f"connection {connection_id} is not connected")
    return connection


def _s3_body(ref):
    import boto3

    return boto3.client("s3").get_object(Bucket=ref["bucket"], Key=ref["key"])["Body"].read()


def _upload_files(action, data):
    """Return the ``{s3, filename}`` files the action should upload."""
    if action.get("source") == "output":
        output = data.get("output") or {}
        if not (output.get("bucket") and output.get("key")):
            raise ValueError("render output does not reference a stored file")
        name = action.get("filename") or str(output["key"]).split("/")[-1]
        return [{"s3": output, "filename": name}]
    files = [
        {"s3": attachment["s3"], "filename": attachment.get("filename") or "attachment"}
        for attachment in data.get("attachments") or []
        if isinstance(attachment.get("s3"), dict)
        and attachment["s3"].get("bucket") and attachment["s3"].get("key")
    ]
    if not files:
        raise ValueError("email has no stored attachments to upload")
    return files


def _default_transport(method, url, *, headers, body, timeout=15):
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, response.read()


def _dropbox_upload(access_token, path, payload, *, transport=None):
    transport = transport or _default_transport
    headers = {
        "authorization": f"Bearer {access_token}",
        "dropbox-api-arg": json.dumps(
            {"path": path, "mode": "add", "autorename": True, "mute": False},
            separators=(",", ":"),
        ),
        "content-type": "application/octet-stream",
    }
    try:
        status, raw = transport(
            "POST", DROPBOX_UPLOAD_URL, headers=headers, body=payload, timeout=15,
        )
    except Exception as exc:
        raise RuntimeError(f"dropbox upload unreachable: {type(exc).__name__}")
    if status >= 300:
        tag = ""
        try:
            error = json.loads(raw.decode() or "{}").get("error")
            if isinstance(error, dict):
                tag = error.get(".tag") or ""
                nested = error.get(tag)
                if isinstance(nested, dict) and nested.get(".tag"):
                    tag = f"{tag}/{nested['.tag']}"
        except (ValueError, UnicodeDecodeError):
            pass
        raise RuntimeError(f"dropbox upload returned HTTP {status}{f' ({tag})' if tag else ''}")


def run_dropbox_upload(action, event, transport=None):
    from . import tokens

    connection = _dropbox_connection(action["connection_id"])
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    stripped = str(action.get("folder") or "").strip("/")
    folder = f"/{stripped}" if stripped else ""
    for file in _upload_files(action, event.get("data", {})):
        path = f"{folder}/{_safe_filename(file['filename'])}"
        _dropbox_upload(access_token, path, _s3_body(file["s3"]), transport=transport)


def _dropbox_rpc(url, access_token, payload, *, transport=None, unreachable="dropbox call unreachable"):
    transport = transport or _default_transport
    headers = {
        "authorization": f"Bearer {access_token}",
        "content-type": "application/json",
    }
    try:
        status, raw = transport(
            "POST", url, headers=headers, body=json.dumps(payload).encode(), timeout=15,
        )
    except Exception as exc:
        raise RuntimeError(f"{unreachable}: {type(exc).__name__}")
    if status >= 300:
        tag = ""
        try:
            error = json.loads(raw.decode() or "{}").get("error")
            if isinstance(error, dict):
                tag = error.get(".tag") or ""
                nested = error.get(tag)
                if isinstance(nested, dict) and nested.get(".tag"):
                    tag = f"{tag}/{nested['.tag']}"
        except (ValueError, UnicodeDecodeError):
            pass
        raise RuntimeError(f"dropbox call returned HTTP {status}{f' ({tag})' if tag else ''}")
    return raw


def _dropbox_download(access_token, path, *, transport=None):
    transport = transport or _default_transport
    headers = {
        "authorization": f"Bearer {access_token}",
        "dropbox-api-arg": json.dumps({"path": path}, separators=(",", ":")),
    }
    try:
        status, raw = transport(
            "POST", DROPBOX_DOWNLOAD_URL, headers=headers, body=b"", timeout=15,
        )
    except Exception as exc:
        raise RuntimeError(f"dropbox download unreachable: {type(exc).__name__}")
    if status >= 300:
        raise RuntimeError(f"dropbox download returned HTTP {status}")
    return raw


def run_dropbox_delete(action, event, transport=None):
    """Delete the processed file from Dropbox; run only after intake succeeds."""
    from . import tokens

    connection = _dropbox_connection(action["connection_id"])
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    path = action.get("path") or event.get("data", {}).get("path")
    if not path:
        raise ValueError("dropbox_delete requires a file path")
    _dropbox_rpc(
        DROPBOX_DELETE_URL, access_token, {"path": path},
        transport=transport, unreachable="dropbox delete unreachable",
    )


def run_render_job(action, event, workflow_id):
    import boto3

    data = event.get("data", {})
    job_id = f"{event['id']}:{workflow_id}:{action.get('id', 'render')}"
    input_value = data.get(action.get("input_field", "html"))
    if not isinstance(input_value, dict):
        raise ValueError("render input must be an email body object")
    s3 = boto3.client("s3")
    input_bucket = os.environ["RENDER_ARTIFACTS_BUCKET"]
    input_key = f"inputs/{job_id.replace('/', '_')}.html"
    if "value" in input_value:
        source = input_value["value"].encode()
    elif isinstance(input_value.get("s3"), dict):
        source_ref = input_value["s3"]
        source = s3.get_object(Bucket=source_ref["bucket"], Key=source_ref["key"])["Body"].read()
    else:
        raise ValueError("render input must contain value or s3 reference")
    s3.put_object(Bucket=input_bucket, Key=input_key, Body=source, ContentType="text/html", ServerSideEncryption="AES256")
    input_ref = {"bucket": input_bucket, "key": input_key}
    key = action.get("output_key", "rendered/{event_id}.pdf").format(event_id=event["id"].replace("/", "_"))
    output_bucket = action.get("output_bucket") or os.environ[action.get("output_bucket_env", "RENDER_ARTIFACTS_BUCKET")]
    job = {
        "schema": "html-renderer.job.v1",
        "job_id": job_id,
        "input": input_ref,
        "output": {"bucket": output_bucket, "key": key},
        "format": "pdf",
        "pdf": action.get("pdf", {"page_format": "A4", "print_background": True}),
        "context": {"source_event": event},
    }
    boto3.client("sqs").send_message(QueueUrl=os.environ["RENDER_QUEUE_URL"], MessageBody=json.dumps(job))


def execute(event, before_action=None, after_action=None, on_action_error=None):
    for workflow in workflows():
        if matches(workflow, event):
            for index, action in enumerate(workflow.get("actions", [])):
                action_id = action.get("id", str(index))
                if before_action and not before_action(workflow["id"], action_id, event):
                    continue
                try:
                    if action["type"] == "webhook":
                        run_webhook(action, event)
                    elif action["type"] == "slack":
                        run_slack(action, event)
                    elif action["type"] == "dataops":
                        run_dataops(action, event)
                    elif action["type"] == "dropbox_upload":
                        run_dropbox_upload(action, event)
                    elif action["type"] == "dropbox_delete":
                        run_dropbox_delete(action, event)
                    elif action["type"] == "render_html_to_pdf":
                        run_render_job(action, event, workflow["id"])
                    else:
                        raise ValueError(f"unsupported action: {action['type']}")
                except Exception as exc:
                    if on_action_error:
                        on_action_error(workflow["id"], action_id, event, exc)
                    raise
                if after_action:
                    after_action(workflow["id"], action_id, event)
