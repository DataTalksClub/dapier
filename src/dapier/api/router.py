"""The single HTTP entry point: static console, hooks, and API dispatch."""
import base64
import json
import hashlib
import hmac
import os
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs

import boto3

from . import admin
from ..connections.providers import oauth_clients


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


CONSOLE_VIEWS = ("/", "/usage", "/workflows", "/connections", "/emails", "/agents", "/workers", "/credentials", "/tokens", "/runs", "/inbox", "/schedules", "/triggers", "/storage", "/audit", "/designer")
# The designer app shell, framed by the console's /designer view.
DESIGNER_APP_VIEW = "/designer/app"


def _static(path):
    # The designer canvas positions nodes with inline style attributes, which
    # CSP only allows with an explicit style-src exception on its page, and
    # the console's /designer view embeds it, so it opts into frame-ancestors
    # 'self' while every other page refuses all framing.
    embed = path == DESIGNER_APP_VIEW
    style_src = "'self' 'unsafe-inline'" if embed else "'self'"
    frame_ancestors = "'self'" if embed else "'none'"
    assets = {
        **{view: ("index.html", "text/html; charset=utf-8") for view in CONSOLE_VIEWS},
        # Public OAuth verification pages. These are served before auth routes
        # so the URLs on the Google consent screen remain publicly reachable.
        "/about": ("public-home.html", "text/html; charset=utf-8"),
        "/privacy": ("privacy.html", "text/html; charset=utf-8"),
        "/terms": ("terms.html", "text/html; charset=utf-8"),
        DESIGNER_APP_VIEW: ("designer.html", "text/html; charset=utf-8"),
        # The CLI device-pairing page (dapier auth login).
        "/device": ("device.html", "text/html; charset=utf-8"),
        "/assets/device.css": ("device.css", "text/css; charset=utf-8"),
        "/assets/js/device.js": ("js/device.js", "text/javascript; charset=utf-8"),
        "/assets/app.css": ("app.css", "text/css; charset=utf-8"),
        "/assets/public.css": ("public.css", "text/css; charset=utf-8"),
        "/assets/app.js": ("app.js", "text/javascript; charset=utf-8"),
        "/assets/js/api.js": ("js/api.js", "text/javascript; charset=utf-8"),
        "/assets/js/icons.js": ("js/icons.js", "text/javascript; charset=utf-8"),
        "/assets/js/theme-init.js": ("js/theme-init.js", "text/javascript; charset=utf-8"),
        "/assets/js/format.js": ("js/format.js", "text/javascript; charset=utf-8"),
        "/assets/js/md.js": ("js/md.js", "text/javascript; charset=utf-8"),
        "/assets/js/main.js": ("js/main.js", "text/javascript; charset=utf-8"),
        "/assets/js/router.js": ("js/router.js", "text/javascript; charset=utf-8"),
        "/assets/js/overview-loader.js": ("js/overview-loader.js", "text/javascript; charset=utf-8"),
        "/assets/js/home-model.js": ("js/home-model.js", "text/javascript; charset=utf-8"),
        "/assets/js/state.js": ("js/state.js", "text/javascript; charset=utf-8"),
        "/assets/js/ui.js": ("js/ui.js", "text/javascript; charset=utf-8"),
        "/assets/js/views/connections.js": ("js/views/connections.js", "text/javascript; charset=utf-8"),
        "/assets/js/views/credentials.js": ("js/views/credentials.js", "text/javascript; charset=utf-8"),
        "/assets/js/views/oauth-clients.js": ("js/views/oauth-clients.js", "text/javascript; charset=utf-8"),
        "/assets/js/views/overview.js": ("js/views/overview.js", "text/javascript; charset=utf-8"),
        "/assets/js/views/runs.js": ("js/views/runs.js", "text/javascript; charset=utf-8"),
        "/assets/js/views/inbox.js": ("js/views/inbox.js", "text/javascript; charset=utf-8"),
        "/assets/js/views/designer.js": ("js/views/designer.js", "text/javascript; charset=utf-8"),
        "/assets/js/views/emails.js": ("js/views/emails.js", "text/javascript; charset=utf-8"),
        "/assets/js/views/agents.js": ("js/views/agents.js", "text/javascript; charset=utf-8"),
        "/assets/js/views/workers.js": ("js/views/workers.js", "text/javascript; charset=utf-8"),
        "/assets/js/views/schedules.js": ("js/views/schedules.js", "text/javascript; charset=utf-8"),
        "/assets/js/views/tokens.js": ("js/views/tokens.js", "text/javascript; charset=utf-8"),
        "/assets/js/views/storage.js": ("js/views/storage.js", "text/javascript; charset=utf-8"),
        "/assets/js/views/audit.js": ("js/views/audit.js", "text/javascript; charset=utf-8"),
        "/assets/js/views/triggers.js": ("js/views/triggers.js", "text/javascript; charset=utf-8"),
        "/assets/js/theme.js": ("js/theme.js", "text/javascript; charset=utf-8"),
        "/assets/designer.js": ("designer.js", "text/javascript; charset=utf-8"),
        "/assets/designer.css": ("designer.css", "text/css; charset=utf-8"),
        "/assets/favicon.svg": ("favicon.svg", "image/svg+xml"),
        # The dakit design system, vendored (make sync-dakit refreshes it).
        "/assets/vendor/dakit.css": ("vendor/dakit.css", "text/css; charset=utf-8"),
        # dakit.css references its fonts as url("../fonts/…"), which resolves
        # against /assets/vendor/ to /assets/fonts/… — alias the vendored
        # files there so the bundle's own @font-faces resolve.
        "/assets/fonts/inter-var.woff2": ("vendor/fonts/inter-var.woff2", "font/woff2"),
        "/assets/fonts/ibm-plex-mono-400.woff2": ("vendor/fonts/ibm-plex-mono-400.woff2", "font/woff2"),
        "/assets/fonts/ibm-plex-mono-500.woff2": ("vendor/fonts/ibm-plex-mono-500.woff2", "font/woff2"),
    }
    if path not in assets:
        # Workflow deep links (/workflows/<id>) load the same console shell;
        # the client router resolves the workflow.
        if path.startswith("/workflows/"):
            path = "/workflows"
        else:
            return None
    filename, content_type = assets[path]
    asset_path = Path(__file__).resolve().parents[2] / "web" / filename
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
            # Deploys swap bundles in place at the same URLs; caching any
            # tier lets a stale app.js drive fresh markup (the "designer
            # lands on /workflows empty" bug), so everything is no-store.
            "cache-control": "no-store",
            "content-security-policy": (
                "default-src 'self'; script-src 'self'; "
                f"style-src {style_src}; img-src 'self' data:; connect-src 'self'; "
                f"frame-ancestors {frame_ancestors}; base-uri 'self'; form-action 'self'"
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
    from ..triggers import hook_triggers

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
    """Parse JSON or URL-encoded fields; retain raw text for other bodies."""
    text = body.decode(errors="replace")
    if content_type.split(";", 1)[0].strip().lower() == "application/x-www-form-urlencoded":
        return {key: values[0] if len(values) == 1 else values
                for key, values in parse_qs(text, keep_blank_values=True).items()}
    if "json" in content_type or text.lstrip().startswith(("{", "[")):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, (dict, list)):
                return parsed
        except ValueError:
            pass
    return {"raw": text}


def _claim_delivery(item, payload):
    """Claim the delivery's stable event id when the trigger dedupes;
    returns ``(event_id, fresh)``.

    Telegram triggers dedupe by default: every update carries a monotonic
    ``update_id`` — the provider's own delivery identity — so a Telegram
    retry of the same update cannot run the workflow twice. Webhook
    triggers opt in with ``dedupe_path``. With no path, no value at it, or
    a seen store that is not configured, dedupe does not apply:
    ``(None, True)`` and the publish keeps a fresh uuid, exactly as before.
    When the id was already claimed (a provider retry of the same
    delivery), ``fresh`` is False and the caller answers 202 without
    publishing, so the workflow runs once.
    """
    from ..triggers import hook_triggers, seen

    path = str(item.get("dedupe_path") or "").strip()
    if not path and item.get("kind") == "telegram":
        path = "update_id"
    value = hook_triggers.dedupe_value(payload, path) if path and isinstance(payload, dict) else None
    if value is None or str(value) == "":
        return None, True
    event_id = hook_triggers.dedupe_event_id(item["hook_id"], value)
    try:
        return event_id, seen.claim(f"hook#{item['hook_id']}", event_id)
    except Exception:
        # A store outage must never drop a delivery: publish anyway. The
        # stable id still dedupes downstream — run grouping and step leases
        # key on the event id, so a re-delivery finds completed steps.
        return event_id, True


def _release_delivery(item, event_id):
    """Undo a claim whose publish failed, so the provider's retry delivers."""
    if event_id:
        from ..triggers import seen

        seen.forget(f"hook#{item['hook_id']}", event_id)


def _challenge_value(payload, query):
    """The caller's verification challenge: query `challenge`/`hub.challenge`
    first, falling back to the body's `challenge` (Slack's url_verification)."""
    value = query.get("challenge") or query.get("hub.challenge")
    if not value and isinstance(payload, dict):
        value = payload.get("challenge")
    return str(value) if value is not None else ""


def _render_response_template(template, event, steps):
    """Render a sync response template with the action template vocabulary:
    string leaves expand against the event and the recorded steps; dicts,
    lists, numbers and booleans keep their JSON shape."""
    def walk(value):
        if isinstance(value, str):
            from ..engine.actions.templating import render

            return render(value, event, steps)
        if isinstance(value, dict):
            return {key: walk(item) for key, item in value.items()}
        if isinstance(value, list):
            return [walk(item) for item in value]
        return value

    return walk(template)


def _webhook_hook(event, name, body, query):
    from ..triggers import hook_triggers

    item = _hook_item(name)
    if not item or item.get("kind") != "webhook":
        return _response(404, {"error": "unknown hook"})
    if item.get("secret"):
        # A saved secret locks the URL: the bearer token alone no longer
        # admits a delivery. The trigger's signature header (a stored
        # ``signature_header`` rename, else X-Dapier-Signature) must carry
        # sha256=HMAC-SHA256(secret, raw body) over the bytes exactly as
        # received, constant-time compared (the Slack/Zoom pattern); a valid
        # signature is sufficient on its own.
        if not hook_triggers.verify_signature(
                item["secret"], body,
                _header(event, hook_triggers.signature_header_for(item))):
            return _response(401, {"error": "invalid signature"})
    else:
        supplied = _bearer_token(event)
        if not supplied or not hmac.compare_digest(supplied, item.get("token") or ""):
            return _response(401, {"error": "invalid token"})
    content_type = _header(event, "content-type").split(";")[0].strip().lower()
    payload = _webhook_payload(body, content_type)
    mode = ((item.get("response") or {}).get("mode")) or "ack"
    if mode == "challenge":
        # A verification handshake: answer it, run nothing.
        challenge = _challenge_value(payload, query)
        return _response(200 if challenge else 400, challenge or "missing challenge",
                         "text/plain")
    if mode != "sync":
        event_id, fresh = _claim_delivery(item, payload)
        if not fresh:
            return _response(202, {"accepted": True, "duplicate": True, "event_id": event_id})
        try:
            _publish("webhook", hook_triggers.WEBHOOK_EVENT, {
                "hook": item["hook_id"],
                "body": payload,
                "query": query,
                "content_type": content_type,
            }, source=item["hook_id"], event_id=event_id)
        except Exception:
            _release_delivery(item, event_id)
            raise
        return _response(202, {"accepted": True})
    return _sync_webhook_run(item, payload, query, content_type)


def _sync_webhook_run(item, payload, query, content_type):
    """Run the matched workflow inline and answer with the outcome (the
    trigger's ``response.mode: sync``). The production path — step leases,
    run history, task usage, failure notify, workflow-level retry — without
    the queue hop: ``worker.execute`` with the worker's own attempt hooks is
    exactly what a delivered event would get, just inside this invocation.

    Exactly-once: the delivery either runs here or is enqueued, never both.
    A fallback enqueues under the same event id the inline run used, so the
    step leases it already wrote dedupe the worker's replay (completed steps
    skip; only the un-run tail executes there). Sync is attempted only when
    exactly one workflow matches — zero or several fall back to the queue
    path (202), whose behavior stays deterministic. The run gets a
    wall-clock budget (``response.budget_seconds``, default 10 s, cap 25 s)
    checked before each step — never mid-step, so a side-effecting action is
    not killed partway; on overrun the remaining steps are skipped in place
    (no side effects, no leases) and the worker finishes the run out of
    band. A failed run is the documented 500: the delivery is released so a
    provider retry re-executes it, and the failure notifies like the worker
    path would.
    """
    from ..engine import matching, worker
    from ..triggers import hook_triggers

    event_id, fresh = _claim_delivery(item, payload)
    if not fresh:
        return _response(200, {"ok": True, "duplicate": True, "event_id": event_id})
    delivery_id = event_id or uuid.uuid4().hex
    data = {
        "hook": item["hook_id"],
        "body": payload,
        "query": query,
        "content_type": content_type,
    }
    event = {
        "schema_version": "1.0",
        "id": delivery_id,
        "correlation_id": delivery_id,
        "connector": "webhook",
        "event": hook_triggers.WEBHOOK_EVENT,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "source": item["hook_id"],
        "data": data,
    }

    def fallback(reason):
        """Hand the delivery to the queue path: publish under the same event
        id (leases dedupe whatever the inline run finished) and answer 202.
        A failed publish releases the claim so the provider's retry delivers,
        exactly like the ack path."""
        try:
            _publish("webhook", hook_triggers.WEBHOOK_EVENT, data,
                     source=item["hook_id"], event_id=delivery_id)
        except Exception:
            _release_delivery(item, delivery_id)
            raise
        return _response(202, {"accepted": True, "mode": "async", "reason": reason})

    # Sync is a single-workflow contract: with zero or several matches the
    # async path keeps its deterministic behavior instead of guessing whose
    # outcome to answer.
    try:
        candidates = [workflow for workflow in matching.all_workflows()
                      if matching.matches(workflow, event)]
    except Exception:
        return fallback("match_error")
    if len(candidates) != 1:
        return fallback("no_match" if not candidates else "multiple_matches")

    steps = {}
    prod_hooks = worker._attempt_hooks(0)
    deadline = time.monotonic() + hook_triggers.response_budget(item)
    over_budget = {"flag": False}

    def before_action(workflow_id, action_id, event, action_type=None):
        if time.monotonic() >= deadline:
            # Budget checked between steps, never mid-step: the step is
            # skipped without a lease or a side effect, the chain drains, and
            # the fallback below hands the un-run tail to the worker.
            over_budget["flag"] = True
            return False
        return prod_hooks["before_action"](workflow_id, action_id, event, action_type)

    def after_action(workflow_id, action_id, event, output=None, duration_ms=None,
                     status="completed"):
        prod_hooks["after_action"](workflow_id, action_id, event, output=output,
                                   duration_ms=duration_ms, status=status)
        entry = steps.setdefault(str(action_id), {})
        entry.update({"status": status, "output": output or {}})

    def on_action_error(workflow_id, action_id, event, exc, duration_ms=None):
        prod_hooks["on_action_error"](workflow_id, action_id, event, exc,
                                      duration_ms=duration_ms)
        steps.setdefault(str(action_id), {}).update(
            {"error": str(exc) or exc.__class__.__name__})

    try:
        matched = worker.execute(event, before_action=before_action,
                                 after_action=after_action,
                                 on_action_error=on_action_error)
    except worker.RunSuspended as susp:
        # A delay past the inline cap: park the continuation exactly as the
        # worker handler would and tell the caller the outcome is async.
        worker._park_suspension(susp)
        return _response(202, {"ok": True, "suspended": True,
                               "resume_at": (susp.output or {}).get("resume_at"),
                               "event_id": delivery_id})
    except Exception as exc:
        # A failed inline run releases the delivery so a provider retry
        # re-executes it, and notifies like the worker path would.
        _release_delivery(item, delivery_id)
        worker.notify_failure(exc, event)
        return _response(500, {"ok": False, "error": str(exc) or exc.__class__.__name__,
                               "event_id": delivery_id})
    if over_budget["flag"]:
        return fallback("budget")
    workflow_id = matched[0] if matched else f"webhook-trigger-{item['hook_id']}"
    response = {"ok": True, "workflow": workflow_id, "steps": steps, "event_id": delivery_id}
    template = (item.get("response") or {}).get("template")
    body = _render_response_template(template, event, steps) if template is not None else response
    return _response(hook_triggers.response_status(item), body)


def _telegram_hook(event, name, body):
    from ..triggers import hook_triggers

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
    event_id, fresh = _claim_delivery(item, update)
    if not fresh:
        return _response(200, {"accepted": True, "duplicate": True, "event_id": event_id})
    # The data shape — and the event name a delivery publishes as (channel
    # announcements are their own channel_post.received event) — lives in
    # hook_triggers so live discovery publishes the exact same shape a real
    # delivery does.
    try:
        _publish("telegram", hook_triggers.telegram_event_for(update),
                 hook_triggers.update_data(update, item["hook_id"]),
                 source=item["hook_id"], event_id=event_id)
    except Exception:
        _release_delivery(item, event_id)
        raise
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

    if method == "GET" and path == "/api/catalog":
        # Public, read-only manifests: the action/trigger/logic catalog the
        # designer and CLI render from (src/dapier/connectors/registry.py).
        from ..connectors import registry

        return _response(200, registry.catalog())

    if method == "GET" and path in ("/hooks/dropbox", "/hooks/youtube"):
        challenge = query.get("challenge") or query.get("hub.challenge")
        return _response(200 if challenge else 400, challenge or "missing challenge", "text/plain")

    if method == "GET" and path == "/hooks/ses-notifications":
        # SNS never GETs this URL; a plain introspection for operators.
        return _response(200, {"hook": "ses-notifications", "method": "POST",
                               "accepts": "SNS envelope (SubscriptionConfirmation | Notification)"})

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
    elif path == "/hooks/ses-notifications":
        from ..triggers.intake import ses_notifications

        status, payload = ses_notifications.handle(body, publish=_publish)
        return _response(status, payload)
    elif path.startswith("/hooks/zoom/"):
        from ..triggers.intake import zoom_webhooks

        connection_id = path.removeprefix("/hooks/zoom/").strip("/")
        if not connection_id or "/" in connection_id:
            return _response(404, {"error": "not found"})
        if len(body) > MAX_HOOK_BODY_BYTES:
            return _response(413, {"error": "body too large"})
        table = boto3.resource("dynamodb").Table(os.environ["CONNECTIONS_TABLE"])
        status, payload = zoom_webhooks.handle(
            connection_id, event.get("headers"), body,
            connections_table=table, publish=_publish,
        )
        return _response(status, payload)
    elif path.startswith("/hooks/slack/"):
        from ..triggers.intake import slack_events

        connection_id = path.removeprefix("/hooks/slack/").strip("/")
        if not connection_id or "/" in connection_id:
            return _response(404, {"error": "not found"})
        if len(body) > MAX_HOOK_BODY_BYTES:
            return _response(413, {"error": "body too large"})
        table = boto3.resource("dynamodb").Table(os.environ["CONNECTIONS_TABLE"])
        status, payload = slack_events.handle(
            connection_id, event.get("headers"), body,
            connections_table=table, publish=_publish,
        )
        return _response(status, payload)
    elif path.startswith("/hooks/mailchimp/"):
        from ..triggers.intake import mailchimp_webhooks

        name = path.removeprefix("/hooks/mailchimp/").strip("/").lower()
        if not name or "/" in name:
            return _response(404, {"error": "not found"})
        if len(body) > MAX_HOOK_BODY_BYTES:
            return _response(413, {"error": "body too large"})
        # The stored hook trigger gates the URL (enable/disable, the same
        # `hooks` CRUD every surface already has); no bearer check — Mailchimp
        # sends no auth headers, the unguessable name is the credential.
        item = _hook_item(name)
        if not item or item.get("kind") != "mailchimp":
            return _response(404, {"error": "unknown hook"})
        # Delivery dedupe like the webhook kind: Mailchimp retries on non-2xx,
        # and a trigger with a dedupe_path (a real delivery's top-level
        # ``fired_at`` is the natural one) answers a re-POST of the same
        # delivery 202 without publishing twice.
        event_id, fresh = _claim_delivery(item, mailchimp_webhooks.parse_form(body))
        if not fresh:
            return _response(202, {"accepted": True, "duplicate": True, "event_id": event_id})

        def _publish_claimed(connector, event_type, data, source=None):
            _publish(connector, event_type, data, source=source, event_id=event_id)

        try:
            status, payload = mailchimp_webhooks.handle(body, hook=name,
                                                        publish=_publish_claimed)
        except Exception:
            _release_delivery(item, event_id)
            raise
        if status >= 300:
            _release_delivery(item, event_id)
        return _response(status, payload)
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
