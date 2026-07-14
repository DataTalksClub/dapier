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


def handler(event, _context):
    request = event.get("requestContext", {}).get("http", {})
    method, path = request.get("method"), request.get("path", "")
    query = event.get("queryStringParameters") or {}

    if method == "GET":
        static = _static(path)
        if static:
            return static

    if path.startswith("/api/admin/") or path == "/oauth/callback" or path.startswith("/auth/"):
        return admin.route(event, method, path)

    if method == "GET" and path == "/health":
        return _response(200, {"ok": True, "service": "dapier"})

    if method == "GET" and path in ("/hooks/dropbox", "/hooks/youtube"):
        challenge = query.get("challenge") or query.get("hub.challenge")
        return _response(200 if challenge else 400, challenge or "missing challenge", "text/plain")

    body = _body(event)
    if path == "/hooks/dropbox":
        payload = json.loads(body or b"{}")
        # A resolver consumes these account IDs and emits individual file events.
        _publish("dropbox", "account.changed", payload, event_id=str(uuid.uuid4()))
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
