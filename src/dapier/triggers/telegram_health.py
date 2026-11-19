"""Telegram webhook health: Telegram's own view of every enabled trigger.

A Telegram trigger is a push integration: when Telegram stops delivering —
the bot's webhook was repointed or deleted, or Dapier answered a delivery
with an error so Telegram parks every later update behind it and retries
with backoff — Dapier simply sees nothing. No run fails, no inbox row is
written, and the silence looks exactly like a quiet channel. The only
place the outage is visible is Telegram's ``getWebhookInfo``.

``check()`` asks Telegram, for every enabled telegram hook trigger, and
flags a trigger as failing when:

- ``url_mismatch`` — the bot's webhook is not this trigger's URL (another
  service called setWebhook/deleteWebhook with the same token);
- ``delivery_error`` — Telegram recorded a delivery error within
  ``ERROR_WINDOW_SECONDS`` while it still holds pending updates (the stuck
  queue the 2026-11 outage was);
- ``backlog`` — ``pending_update_count`` reached ``BACKLOG_THRESHOLD``;
- ``missing_updates`` — the registration's ``allowed_updates`` dropped a
  type Dapier subscribes to (channel posts would silently stop);
- ``probe_failed`` — the bot token or Telegram itself could not be reached.

The webhook view cannot see one failure class: posts Telegram never sends
at all. Telegram does not deliver a bot's own messages back to it, so a
channel announcement posted *through* the trigger's bot is invisible — the
2026-11 outage, with a perfectly healthy webhook. ``channel_check()`` closes
that end to end: for every public channel a telegram trigger has received
posts from (the trigger inbox, 30-day retention) it reads the channel's
public preview (``t.me/s/<username>``) and flags ``missed_posts`` when a
post older than ``CHANNEL_GRACE_SECONDS`` (and younger than
``CHANNEL_MAX_AGE_SECONDS``, so one old gap does not page forever) has a
higher message id than anything Dapier received from that channel.

Missed posts do not just page: while a trigger's webhook is fully healthy
(no delivery problems and nothing pending, so Telegram is not holding the
post for later delivery) the schedule relays every missed text post back
through the trigger's own intake under a stable ``relay-<chat>-<message>``
update id — the same wrap the Send test request performs — so the crosspost
workflows run within one check cycle. Media-only posts have no text to
relay and stay alert-only (relay them manually, see below).
``handler()`` is the TelegramHealthFunction schedule (template.yaml, every
5 minutes). It publishes ``Dapier/Triggers TelegramWebhookUnhealthy`` (a
CloudWatch alarm pages the operator inboxes through AlarmNotifyFunction
and sends OK on recovery; a missing datapoint — the check itself dead —
also alarms) and emails a detailed report to the operator inboxes at most
once an hour while anything is failing. ``repair()`` re-registers the
webhook (the same setWebhook a trigger save performs) — the fix for
``url_mismatch`` and for a secret the bot no longer echoes; the operator
runs it explicitly through ``dapier webhooks telegram-health --repair``.
"""

import logging
from datetime import datetime, timezone

from ..connections.providers import telegram_api

logger = logging.getLogger(__name__)

ERROR_WINDOW_SECONDS = 30 * 60
BACKLOG_THRESHOLD = 20
REQUIRED_UPDATES = ("message", "channel_post")
METRIC_NAMESPACE = "Dapier/Triggers"
METRIC_NAME = "TelegramWebhookUnhealthy"
ALERT_SCOPE = "telegram-health"
CHANNEL_GRACE_SECONDS = 30 * 60
CHANNEL_MAX_AGE_SECONDS = 3 * 86400
CHANNEL_PREVIEW_URL = "https://t.me/s/{username}"
_INBOX_PAGES = 10


def _now():
    return datetime.now(timezone.utc)


def _iso(epoch):
    try:
        return datetime.fromtimestamp(int(epoch), timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def assess(item, info, *, now=None):
    """The problems one trigger's ``getWebhookInfo`` result shows (pure)."""
    now = now or _now()
    problems = []
    url = str(info.get("url") or "")
    if url != str(item.get("url") or ""):
        problems.append({
            "code": "url_mismatch",
            "detail": (f"the bot's webhook points at {url!r}, not this trigger's URL"
                       if url else "the bot has no webhook registered (deleteWebhook was called)"),
        })
    pending = int(info.get("pending_update_count") or 0)
    error_at = info.get("last_error_date")
    try:
        error_age = now.timestamp() - int(error_at) if error_at else None
    except (TypeError, ValueError):
        error_age = None
    if error_age is not None and error_age <= ERROR_WINDOW_SECONDS and pending > 0:
        problems.append({
            "code": "delivery_error",
            "detail": (f"Telegram failed to deliver ({info.get('last_error_message') or 'no message'}) "
                       f"at {_iso(error_at)} and holds {pending} pending update(s)"),
        })
    if pending >= BACKLOG_THRESHOLD:
        problems.append({
            "code": "backlog",
            "detail": f"Telegram holds {pending} undelivered update(s)",
        })
    allowed = info.get("allowed_updates")
    if url and isinstance(allowed, list) and allowed:
        missing = [name for name in REQUIRED_UPDATES if name not in allowed]
        if missing:
            problems.append({
                "code": "missing_updates",
                "detail": f"the registration no longer subscribes to {', '.join(missing)}",
            })
    return problems


def _report(item, info=None, problems=None, error=None):
    info = info or {}
    row = {
        "hook_id": item.get("hook_id"),
        "connection_id": item.get("connection_id"),
        "expected_url": item.get("url"),
        "webhook_url": info.get("url"),
        "pending_update_count": int(info.get("pending_update_count") or 0),
        "last_error_at": _iso(info.get("last_error_date")) if info.get("last_error_date") else None,
        "last_error_message": info.get("last_error_message"),
        "allowed_updates": info.get("allowed_updates"),
        "problems": list(problems or []),
    }
    if error:
        row["problems"].append({"code": "probe_failed", "detail": error})
    row["status"] = "failing" if row["problems"] else "ok"
    return row


def _token(item, connections_table=None):
    from . import hook_triggers

    connection = hook_triggers._telegram_connection(item["connection_id"], connections_table)
    return hook_triggers._telegram_connection_token(connection, connections_table)


def telegram_items(table_ref=None):
    from . import hook_triggers

    return [item for item in hook_triggers.load_items(table_ref=table_ref)
            if item.get("kind") == "telegram" and item.get("enabled", True)]


def check(*, table_ref=None, connections_table=None, transport=None, now=None):
    """Health rows for every enabled telegram trigger (see the module doc)."""
    from . import hook_triggers

    rows = []
    for item in telegram_items(table_ref):
        try:
            info = telegram_api.get_webhook_info(
                _token(item, connections_table), transport=transport)
        except (hook_triggers.TriggerError, telegram_api.TelegramApiError) as exc:
            rows.append(_report(item, error=str(exc)))
            continue
        rows.append(_report(item, info, assess(item, info, now=now)))
    return rows


def received_channels(events):
    """``{(hook, username): {chat_id, max_message_id}}`` from inbox events.

    Only public channels (they carry a ``username``) — the preview the check
    compares against exists for nothing else."""
    seen = {}
    for event in events:
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        chat = data.get("chat") if isinstance(data.get("chat"), dict) else {}
        username = str(chat.get("username") or "").strip().lower()
        if not data.get("is_channel_post") or not username or chat.get("type") != "channel":
            continue
        try:
            message_id = int(data.get("message_id"))
        except (TypeError, ValueError):
            continue
        key = (str(data.get("hook") or event.get("source") or ""), username)
        row = seen.setdefault(key, {"chat_id": chat.get("id"), "max_message_id": message_id})
        row["max_message_id"] = max(row["max_message_id"], message_id)
    return seen


def _telegram_events(table_ref=None):
    from . import inbox

    events, token = [], None
    for _ in range(_INBOX_PAGES):
        status, payload = inbox.api_list("telegram", inbox.MAX_LIMIT, next_token=token,
                                         table_ref=table_ref)
        if status != 200:
            raise RuntimeError(payload.get("error") or "inbox list failed")
        events.extend(payload.get("events") or [])
        token = (payload.get("paging") or {}).get("next")
        if not token:
            break
    return events


def public_posts(page_html):
    """``[(message_id, epoch, text)]`` of the posts on one t.me/s preview page.

    The text is the rendered body with inline markup stripped and links
    replaced by their href — the plain text the relay posts back through
    the trigger. A post with no text div (media only) relays as ``""``.
    """
    import re

    posts = []
    for block in re.split(r'(?=<div class="tgme_widget_message_wrap)', page_html or ""):
        post = re.search(r'data-post="[^"/]+/(\d+)"', block)
        when = re.search(r'<time[^>]*datetime="([^"]+)"', block)
        if not post or not when:
            continue
        try:
            epoch = datetime.fromisoformat(when.group(1)).timestamp()
        except ValueError:
            continue
        body = re.search(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', block, re.S)
        text = ""
        if body:
            inner = body.group(1)
            inner = re.sub(r'<br\s*/?>', '\n', inner)
            inner = re.sub(r'<a[^>]*href="([^"]*)"[^>]*>.*?</a>', lambda m: m.group(1), inner, flags=re.S)
            inner = re.sub(r'<[^>]+>', '', inner)
            import html as _html

            text = re.sub(r'\n{3,}', '\n\n', _html.unescape(inner)).strip()
        posts.append((int(post.group(1)), epoch, text))
    return posts


def _fetch_preview(username, transport=None):
    if transport is None:
        from src.dapier.engine.actions import base

        transport = base._default_transport
    status, raw = transport("GET", CHANNEL_PREVIEW_URL.format(username=username),
                            headers={"user-agent": "Mozilla/5.0 (compatible; DapierHealth/1.0)"},
                            body=None, timeout=15)
    if status >= 300:
        raise RuntimeError(f"t.me preview answered HTTP {status}")
    return raw.decode("utf-8", errors="replace")


def channel_check(*, events=None, fetch=None, now=None, table_ref=None):
    """End-to-end rows: did Dapier receive every recent public channel post?"""
    now = now or _now()
    fetch = fetch or _fetch_preview
    active = {item.get("hook_id") for item in telegram_items()}
    if events is None:
        events = _telegram_events(table_ref)
    rows = []
    for (hook, username), seen in sorted(received_channels(events).items()):
        if hook not in active:
            continue
        row = {"hook_id": hook, "channel": username, "chat_id": seen["chat_id"],
               "received_max_message_id": seen["max_message_id"], "missed": [],
               "problems": []}
        try:
            posts = public_posts(fetch(username))
        except Exception as exc:  # noqa: BLE001 — a preview hiccup is reported, never fatal
            row["preview_error"] = f"{type(exc).__name__}: {exc}"[:200]
            row["status"] = "unknown"
            rows.append(row)
            continue
        row["public_max_message_id"] = max((post for post, _, _ in posts), default=None)
        missed = [(post, when, text) for post, when, text in posts
                  if post > seen["max_message_id"]
                  and CHANNEL_GRACE_SECONDS <= now.timestamp() - when <= CHANNEL_MAX_AGE_SECONDS]
        if missed:
            row["missed"] = [post for post, _, _ in missed]
            row["missed_posts"] = [{"message_id": post, "date": int(when), "text": text}
                                   for post, when, text in missed]
            row["problems"].append({
                "code": "missed_posts",
                "detail": (f"t.me/{username}/{', '.join(str(post) for post in row['missed'])} never reached "
                           f"Dapier (newest received: {seen['max_message_id']}). Posts the trigger's bot "
                           f"sent itself are never delivered back to it; the schedule relays text posts "
                           f"automatically while the webhook is healthy (`dapier webhooks test {hook} "
                           f"--data <message.json>` relays one by hand)"),
            })
        row["status"] = "failing" if row["problems"] else "ok"
        rows.append(row)
    return rows


def auto_relay(channel_rows, webhook_rows, *, transport=None, connections_table=None):
    """Relay missed public channel posts through their trigger's intake.

    Only for hooks whose webhook is fully healthy — no delivery problems
    and nothing pending — because that is exactly the signature of a post
    the trigger's bot sent itself: Telegram will never deliver it, so the
    relay cannot duplicate a delivery that is still in flight. When the
    webhook is failing, Telegram holds the posts and will deliver them
    itself once the hook answers 200 again; relaying here would double-post.

    Each post is POSTed to the hook URL with its secret as the wrap the
    Send test request performs: ``{"update_id": "relay-<chat>-<message>",
    "channel_post": {...}}`` — the hook's own dedupe (update_id) keeps
    repeated checks from running the crosspost twice. Returns
    ``{hook_id: {"relayed": [ids], "errors": [(id, reason)]}}`` for the
    hooks something happened on. A post with no text (media only) cannot
    be relayed from the preview and is left to the operator.
    """
    import json as _json

    if transport is None:
        from src.dapier.engine.actions import base

        transport = base._default_transport
    deliverable = {row["hook_id"] for row in webhook_rows
                   if row["status"] == "ok" and not row.get("pending_update_count")}
    pending = []
    for row in channel_rows:
        hook = row.get("hook_id")
        posts = [post for post in (row.get("missed_posts") or [])
                 if str(post.get("text") or "").strip()]
        if hook in deliverable and posts:
            pending.append((row, posts))
    result = {}
    if not pending:
        return result
    items = {item.get("hook_id"): item for item in telegram_items()}
    for row, posts in pending:
        hook = row["hook_id"]
        item = items.get(hook)
        if not item or not item.get("url"):
            continue
        try:
            token = _token(item, connections_table)
        except Exception as exc:  # noqa: BLE001 — reported per hook, never fatal
            result[hook] = {"relayed": [], "errors": [(post["message_id"], str(exc)[:120])
                                                      for post in posts]}
            continue
        chat_id = row.get("chat_id")
        relayed, errors = [], [
            (post["message_id"], "no text in the public preview (media post)")
            for post in (row.get("missed_posts") or [])
            if not str(post.get("text") or "").strip()]
        for post in posts:
            update = {
                "update_id": f"relay-{chat_id}-{post['message_id']}",
                "channel_post": {
                    "message_id": post["message_id"],
                    "date": post["date"],
                    "chat": {"id": chat_id, "type": "channel", "username": row.get("channel")},
                    "text": post["text"],
                },
            }
            url = item.get("url")
            try:
                status, raw = transport(
                    "POST", url,
                    headers={"content-type": "application/json",
                             "x-telegram-bot-api-secret-token": token},
                    body=_json.dumps(update).encode(), timeout=15)
                if status >= 300:
                    raise RuntimeError(f"hook answered HTTP {status}")
                relayed.append(post["message_id"])
            except Exception as exc:  # noqa: BLE001 — reported per post, never fatal
                errors.append((post["message_id"], f"{type(exc).__name__}: {exc}"[:120]))
        if relayed or errors:
            result[hook] = {"relayed": relayed, "errors": errors}
    return result


def repair(name=None, *, table_ref=None, connections_table=None, transport=None):
    """Re-run setWebhook for one (or every) enabled telegram trigger.

    The same registration a trigger save performs: the bot is pointed back
    at the trigger URL with the stored secret. Pending updates are kept, so
    Telegram redelivers the backlog once Dapier answers 200.
    """
    from . import hook_triggers

    repaired = []
    for item in telegram_items(table_ref):
        if name and item.get("hook_id") != name:
            continue
        hook_triggers._register_telegram(
            item, table_ref=table_ref, connections_table=connections_table,
            transport=transport)
        repaired.append(item["hook_id"])
    if name and not repaired:
        raise hook_triggers.TriggerError(f"no enabled telegram trigger named '{name}'")
    return repaired


def summary(rows):
    lines = []
    for row in rows:
        if "channel" in row:
            lines.append(f"channel @{row['channel']} via {row['hook_id']}: {row['status'].upper()}")
            for problem in row["problems"]:
                lines.append(f"  PROBLEM {problem['code']}: {problem['detail']}")
            if row.get("relayed"):
                lines.append(f"  relayed back through the trigger: {', '.join(str(p) for p in row['relayed'])}")
            for miss in row.get("relay_errors") or []:
                lines.append(f"  RELAY FAILED {miss['message_id']}: {miss['error']}")
            continue
        lines.append(f"{row['hook_id']} (connection {row['connection_id']}): {row['status'].upper()}")
        lines.append(f"  expected url: {row['expected_url']}")
        lines.append(f"  webhook url:  {row['webhook_url']}")
        lines.append(f"  pending updates: {row['pending_update_count']}")
        if row.get("last_error_at"):
            lines.append(f"  last delivery error: {row['last_error_at']} {row.get('last_error_message') or ''}")
        for problem in row["problems"]:
            lines.append(f"  PROBLEM {problem['code']}: {problem['detail']}")
    return "\n".join(lines)


def _put_metric(failing, cloudwatch=None):
    if cloudwatch is None:
        import boto3

        cloudwatch = boto3.client("cloudwatch")
    cloudwatch.put_metric_data(Namespace=METRIC_NAMESPACE, MetricData=[
        {"MetricName": METRIC_NAME, "Value": float(failing), "Unit": "Count"},
    ])


def _alert(rows, *, now=None, ses=None):
    """Email the failing rows to the operator, at most once per hour."""
    from . import seen
    from ..engine.notify import operator_recipients, operator_sender, ses_client

    now = now or _now()
    bucket = now.strftime("%Y-%m-%dT%H")
    try:
        if not seen.claim(ALERT_SCOPE, f"alert-{bucket}", ttl_days=2):
            return False
    except Exception:  # noqa: BLE001 — a store hiccup must not swallow the page
        logger.exception("telegram health alert throttle unavailable; alerting anyway")
    failing = [row for row in rows if row["status"] == "failing"]
    names = ", ".join(sorted({row["hook_id"] for row in failing}))
    body = (
        "CRITICAL: Telegram posts are not reaching Dapier.\n\n"
        "Telegram messages for the triggers below are NOT reaching Dapier, so the "
        "workflows they start (e.g. telegram-crosspost to Slack) are silently not running.\n"
        "Text posts missed on a healthy webhook are relayed automatically by this "
        "check; media-only posts and failing webhooks still need the steps below.\n\n"
        f"{summary(failing)}\n\n"
        "Fix:\n"
        "  dapier webhooks telegram-health            # Telegram's current view\n"
        "  dapier webhooks telegram-health --repair   # re-register the webhook (keeps the backlog)\n"
        "  dapier webhooks test <hook> --data <message.json>  # relay one post by hand\n"
        "If last delivery error is an HTTP 5xx from Dapier, Dapier is rejecting an update: "
        "check the API Lambda logs for /hooks/telegram/<name>.\n"
        "Repeats hourly while failing; the CloudWatch alarm TelegramWebhookUnhealthy sends OK on recovery.\n"
    )
    (ses or ses_client()).send_email(
        Source=operator_sender(),
        Destination={"ToAddresses": operator_recipients()},
        Message={
            "Subject": {"Data": f"CRITICAL: Telegram webhook failing ({names})", "Charset": "utf-8"},
            "Body": {"Text": {"Data": body, "Charset": "utf-8"}},
        },
    )
    return True


def handler(event=None, context=None):
    """Scheduled check: metric every run, relay + operator email while failing.

    A probe failure counts as failing — a check that cannot see Telegram
    must not report healthy. Missed channel posts on a healthy webhook are
    relayed back through the trigger (see auto_relay) before the report."""
    rows = check()
    try:
        channels = channel_check()
    except Exception:  # noqa: BLE001 — the webhook half still reports
        logger.exception("telegram channel check failed")
        channels = []
    try:
        relayed = auto_relay(channels, rows)
    except Exception:  # noqa: BLE001 — the report still goes out
        logger.exception("telegram missed-post relay failed")
        relayed = {}
    for row in channels:
        outcome = relayed.get(row.get("hook_id"))
        if outcome and row.get("missed"):
            row["relayed"] = outcome.get("relayed", [])
            row["relay_errors"] = [{"message_id": mid, "error": err}
                                   for mid, err in outcome.get("errors", [])]
    failing = sum(1 for row in rows if row["status"] != "ok")
    failing += sum(1 for row in channels if row["status"] == "failing")
    rows = rows + channels
    _put_metric(failing)
    alerted = False
    if failing:
        logger.error("telegram webhook unhealthy:\n%s", summary(rows))
        alerted = _alert(rows)
    return {"checked": len(rows), "failing": failing, "alerted": alerted,
            "relayed": relayed, "triggers": rows}
