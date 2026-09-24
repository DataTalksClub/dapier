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
from . import oauth_clients


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


CONSOLE_VIEWS = ("/", "/workflows", "/connections", "/credentials", "/runs")
DESIGNER_VIEW = "/designer"
# Pages whose static cache policy matches the console views.
HTML_VIEWS = CONSOLE_VIEWS + (DESIGNER_VIEW,)


def _static(path):
    # The designer canvas positions nodes with inline style attributes, which
    # CSP only allows with an explicit style-src exception for that page.
    style_src = "'self' 'unsafe-inline'" if path == DESIGNER_VIEW else "'self'"
    assets = {
        **{view: ("index.html", "text/html; charset=utf-8") for view in CONSOLE_VIEWS},
        DESIGNER_VIEW: ("designer.html", "text/html; charset=utf-8"),
        "/assets/app.css": ("app.css", "text/css; charset=utf-8"),
        "/assets/app.js": ("app.js", "text/javascript; charset=utf-8"),
        "/assets/lucide.min.js": ("lucide.min.js", "text/javascript; charset=utf-8"),
        "/assets/designer.js": ("designer.js", "text/javascript; charset=utf-8"),
        "/assets/designer.css": ("designer.css", "text/css; charset=utf-8"),
        "/assets/fonts/IBMPlexSans-VF.woff2": ("assets/fonts/IBMPlexSans-VF.woff2", "font/woff2"),
        "/assets/fonts/IBMPlexMono-Regular.woff2": ("assets/fonts/IBMPlexMono-Regular.woff2", "font/woff2"),
        "/assets/fonts/IBMPlexMono-Medium.woff2": ("assets/fonts/IBMPlexMono-Medium.woff2", "font/woff2"),
    }
    if path not in assets:
        return None
    filename, content_type = assets[path]
    asset_path = Path(__file__).parent / "web" / filename
    if filename.endswith(".woff2"):
        body = base64.b64encode(asset_path.read_bytes()).decode()
        extra = {"isBase64Encoded": True}
    else:
        body = asset_path.read_text()
        extra = {}
    response = _response(
        200,
        body,
        content_type,
        headers={
            "cache-control": "no-store" if path in HTML_VIEWS else "public, max-age=300",
            "content-security-policy": (
                "default-src 'self'; script-src 'self'; "
                f"style-src {style_src}; img-src 'self' data:; connect-src 'self'; "
                "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
            ),
            "x-content-type-options": "nosniff",
            "referrer-policy": "same-origin",
        },
    )
    response.update(extra)
    return response


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
    """Dropbox app secrets that may sign webhook deliveries.

    Dropbox signs each webhook body with the app secret (X-Dropbox-Signature,
    HMAC-SHA256) of the OAuth client. The runtime-configured client (config
    DB, set from the console) and the deploy-time environment variable are
    both accepted so a rotation window doesn't drop deliveries. With no
    secret configured anywhere there is nothing to verify against and
    verification fails closed.
    """
    secrets = []
    try:
        _, configured_secret = oauth_clients.get("dropbox")
        secrets.append(configured_secret)
    except oauth_clients.ClientConfigError:
        pass
    env_secret = os.environ.get("DROPBOX_OAUTH_CLIENT_SECRET", "").strip()
    if env_secret and env_secret not in secrets:
        secrets.append(env_secret)
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


# Envelopes ride SQS (256 KB messages), so oversized webhook bodies are
# rejected before they can wedge the pipeline.
MAX_HOOK_BODY_BYTES = 200_000

TELEGRAM_SECRET_HEADER = "x-telegram-bot-api-secret-token"


def _hook_item(name):
    """The enabled stored hook trigger for ``name``, or None when absent."""
    if not os.environ.get("HOOK_TRIGGERS_TABLE"):
        return None
    from . import hook_triggers

    try:
        item = hook_triggers.get_item(name)
    except hook_triggers.TriggerError:
        return None
    return item if item and item.get("enabled", True) else None


def _bearer_token(event):
    """Token from ``Authorization: Bearer <token>`` (bare values tolerated)."""
    value = _header(event, "authorization").strip()
    scheme, _, token = value.partition(" ")
    return token.strip() if scheme.lower() == "bearer" else value


def _webhook_payload(body, content_type):
    """Parsed JSON when the body is JSON, otherwise ``{"raw": text}``."""
    text = body.decode(errors="replace")
    if "json" in content_type or text.lstrip().startswith(("{", "[")):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, (dict, list)):
                return parsed
        except ValueError:
            pass
    return {"raw": text}


def _webhook_hook(event, name, body, query):
    from . import hook_triggers

    item = _hook_item(name)
    if not item or item.get("kind") != "webhook":
        return _response(404, {"error": "unknown hook"})
    supplied = _bearer_token(event)
    if not supplied or not hmac.compare_digest(supplied, item.get("token") or ""):
        return _response(401, {"error": "invalid token"})
    content_type = _header(event, "content-type").split(";")[0].strip().lower()
    _publish("webhook", hook_triggers.WEBHOOK_EVENT, {
        "hook": item["hook_id"],
        "body": _webhook_payload(body, content_type),
        "query": query,
        "content_type": content_type,
    }, source=item["hook_id"])
    return _response(202, {"accepted": True})


def _telegram_hook(event, name, body):
    from . import hook_triggers

    item = _hook_item(name)
    if not item or item.get("kind") != "telegram":
        return _response(404, {"error": "unknown hook"})
    secret = _header(event, TELEGRAM_SECRET_HEADER)
    if not secret or not hmac.compare_digest(secret, item.get("token") or ""):
        return _response(401, {"error": "invalid secret"})
    try:
        update = json.loads(body.decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        return _response(400, {"error": "invalid json"})
    if not isinstance(update, dict):
        return _response(400, {"error": "invalid update"})
    message = update.get("message") or update.get("edited_message") or {}
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    _publish("telegram", hook_triggers.TELEGRAM_EVENT, {
        "hook": item["hook_id"],
        "update_id": update.get("update_id"),
        "message_id": message.get("message_id"),
        "text": message.get("text") or message.get("caption") or "",
        "chat_id": chat.get("id"),
        "chat": chat,
        "from": sender,
        "update": update,
    }, source=item["hook_id"])
    return _response(200, {"accepted": True})


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
    elif path.startswith("/hooks/webhook/") or path.startswith("/hooks/telegram/"):
        prefix = "/hooks/webhook/" if path.startswith("/hooks/webhook/") else "/hooks/telegram/"
        name = (path.split(prefix, 1)[1] or "").strip("/").lower()
        if not name or "/" in name:
            return _response(404, {"error": "not found"})
        if len(body) > MAX_HOOK_BODY_BYTES:
            return _response(413, {"error": "body too large"})
        if prefix == "/hooks/telegram/":
            return _telegram_hook(event, name, body)
        return _webhook_hook(event, name, body, query)
    elif path.startswith("/hooks/custom/"):
        source = (event.get("pathParameters") or {}).get("source", "custom")
        _publish("custom", "received", json.loads(body or b"{}"), source=source)
    else:
        return _response(404, {"error": "not found"})
    return _response(202, {"accepted": True})
