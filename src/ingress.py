import base64
import json
import hashlib
import hmac
import os
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import boto3
from boto3.dynamodb.conditions import Attr

from . import admin


queue = boto3.client("sqs")
_youtube_secret = None


def _response(status, body, content_type="application/json", headers=None):
    if content_type == "application/json" and not isinstance(body, str):
        body = json.dumps(body)
    return {
        "statusCode": status,
        "headers": {"content-type": content_type, **(headers or {})},
        "body": body,
    }


def _static(path):
    assets = {
        "/": ("index.html", "text/html; charset=utf-8"),
        "/assets/app.css": ("app.css", "text/css; charset=utf-8"),
        "/assets/app.js": ("app.js", "text/javascript; charset=utf-8"),
    }
    if path not in assets:
        return None
    filename, content_type = assets[path]
    body = (Path(__file__).parent / "web" / filename).read_text()
    return _response(
        200,
        body,
        content_type,
        headers={
            "cache-control": "no-store" if path == "/" else "public, max-age=300",
            "content-security-policy": (
                "default-src 'self'; script-src 'self' https://unpkg.com; "
                "style-src 'self'; img-src 'self' data:; connect-src 'self'; "
                "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
            ),
            "x-content-type-options": "nosniff",
            "referrer-policy": "same-origin",
        },
    )


def _body(event):
    raw = event.get("body") or ""
    return base64.b64decode(raw) if event.get("isBase64Encoded") else raw.encode()


def _header(event, name):
    expected = name.lower()
    return next(
        (str(value) for key, value in (event.get("headers") or {}).items() if key.lower() == expected),
        "",
    )


def _publish(connector, event_type, data, source=None, event_id=None):
    event_id = event_id or str(uuid.uuid4())
    envelope = {
        "schema_version": "1.0",
        "id": event_id,
        "correlation_id": event_id,
        "connector": connector,
        "event": event_type,
        "source": source or connector,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "data": data,
    }
    queue.send_message(QueueUrl=os.environ["EVENT_QUEUE_URL"], MessageBody=json.dumps(envelope))


def _youtube(body):
    root = ET.fromstring(body)
    ns = {"atom": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
    for entry in root.findall("atom:entry", ns):
        video_id = entry.findtext("yt:videoId", namespaces=ns)
        channel_id = entry.findtext("yt:channelId", namespaces=ns)
        _publish("youtube", "video.published", {
            "video_id": video_id,
            "channel_id": channel_id,
            "title": entry.findtext("atom:title", namespaces=ns),
            "url": f"https://www.youtube.com/watch?v={video_id}",
        }, source=channel_id, event_id=f"youtube:{video_id}")


def _verify_youtube(event, body):
    global _youtube_secret
    secret_id = os.environ.get("YOUTUBE_WEBHOOK_SECRET_ID")
    if not secret_id:
        return True
    if _youtube_secret is None:
        _youtube_secret = boto3.client("secretsmanager").get_secret_value(SecretId=secret_id)["SecretString"]
    signature = _header(event, "x-hub-signature")
    if not signature.startswith("sha1="):
        return False
    expected = hmac.new(_youtube_secret.encode(), body, hashlib.sha1).hexdigest()
    return hmac.compare_digest(signature[5:], expected)


def _dropbox_secrets():
    """Distinct Dropbox app secrets of all configured connections.

    Dropbox signs each webhook body with the app secret (X-Dropbox-Signature,
    HMAC-SHA256); a connection's client_secret is that app secret. With no
    configured Dropbox connection there is nothing to verify against and
    nothing can be resolved either, so verification fails closed.
    """
    connections_table = boto3.resource("dynamodb").Table(os.environ["CONNECTIONS_TABLE"])
    credentials_table = boto3.resource("dynamodb").Table(os.environ["CREDENTIALS_TABLE"])
    response = connections_table.scan(
        FilterExpression=Attr("provider").eq("dropbox"),
        ProjectionExpression="connection_id",
    )
    secrets = []
    for item in response.get("Items", []):
        record = credentials_table.get_item(
            Key={"credential_id": f"oauth#{item['connection_id']}"},
        ).get("Item") or {}
        value = record.get("value") or {}
        secret = value.get("client_secret") if isinstance(value, dict) else None
        if secret and secret not in secrets:
            secrets.append(secret)
    return secrets


def _verify_dropbox(event, body):
    signature = _header(event, "x-dropbox-signature")
    if not signature:
        return False
    return any(
        hmac.compare_digest(signature, hmac.new(secret.encode(), body, hashlib.sha256).hexdigest())
        for secret in _dropbox_secrets()
    )


def _dropbox_accounts(payload):
    """Notified account IDs from both webhook sections, deduplicated."""
    accounts = []
    for section in ("list_folder", "delta"):
        for account_id in (payload.get(section) or {}).get("accounts") or []:
            if account_id not in accounts:
                accounts.append(account_id)
    return accounts


def _notify_dropbox(account_id, correlation_id):
    event_id = str(uuid.uuid4())
    envelope = {
        "schema_version": "1.0",
        "id": event_id,
        "correlation_id": correlation_id,
        "connector": "dropbox",
        "event": "account.changed",
        "source": account_id,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "data": {"account_id": account_id},
    }
    queue.send_message(QueueUrl=os.environ["DROPBOX_QUEUE_URL"], MessageBody=json.dumps(envelope))


def handler(event, _context):
    request = event.get("requestContext", {}).get("http", {})
    method, path = request.get("method"), request.get("path", "")
    query = event.get("queryStringParameters") or {}

    if method == "GET":
        static = _static(path)
        if static:
            return static

    if path.startswith("/api/admin/") or path.startswith("/api/agent/") or path == "/oauth/callback" or path.startswith("/auth/"):
        return admin.route(event, method, path)

    if method == "GET" and path == "/health":
        return _response(200, {"ok": True, "service": "dapier"})

    if method == "GET" and path in ("/hooks/dropbox", "/hooks/youtube"):
        challenge = query.get("challenge") or query.get("hub.challenge")
        return _response(200 if challenge else 400, challenge or "missing challenge", "text/plain")

    body = _body(event)
    if path == "/hooks/dropbox":
        if not _verify_dropbox(event, body):
            return _response(401, {"error": "invalid signature"})
        payload = json.loads(body or b"{}")
        correlation_id = str(uuid.uuid4())
        for account_id in _dropbox_accounts(payload):
            # One message per account; the resolver turns it into file events.
            _notify_dropbox(account_id, correlation_id)
    elif path == "/hooks/youtube":
        if not _verify_youtube(event, body):
            return _response(401, {"error": "invalid signature"})
        _youtube(body)
    elif path.startswith("/hooks/custom/"):
        source = (event.get("pathParameters") or {}).get("source", "custom")
        _publish("custom", "received", json.loads(body or b"{}"), source=source)
    else:
        return _response(404, {"error": "not found"})
    return _response(202, {"accepted": True})
