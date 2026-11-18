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

``handler()`` is the TelegramHealthFunction schedule (template.yaml, every
5 minutes). It publishes ``Dapier/Triggers TelegramWebhookUnhealthy`` (a
CloudWatch alarm pages dev@ through the AlarmTopic and sends OK on
recovery; a missing datapoint — the check itself dead — also alarms) and
emails a detailed report to the operator inbox at most once an hour while
anything is failing. ``repair()`` re-registers the webhook (the same
setWebhook a trigger save performs) — the fix for ``url_mismatch`` and for
a secret the bot no longer echoes; the operator runs it explicitly through
``dapier webhooks telegram-health --repair``.
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
    from ..engine.notify import operator_recipient, operator_sender, ses_client

    now = now or _now()
    bucket = now.strftime("%Y-%m-%dT%H")
    try:
        if not seen.claim(ALERT_SCOPE, f"alert-{bucket}", ttl_days=2):
            return False
    except Exception:  # noqa: BLE001 — a store hiccup must not swallow the page
        logger.exception("telegram health alert throttle unavailable; alerting anyway")
    failing = [row for row in rows if row["status"] != "ok"]
    names = ", ".join(row["hook_id"] for row in failing)
    body = (
        "CRITICAL: Telegram is not delivering updates to Dapier.\n\n"
        "Telegram messages for the triggers below are NOT reaching Dapier, so the "
        "workflows they start (e.g. telegram-crosspost to Slack) are silently not running.\n\n"
        f"{summary(failing)}\n\n"
        "Fix:\n"
        "  dapier webhooks telegram-health            # Telegram's current view\n"
        "  dapier webhooks telegram-health --repair   # re-register the webhook (keeps the backlog)\n"
        "If last delivery error is an HTTP 5xx from Dapier, Dapier is rejecting an update: "
        "check the API Lambda logs for /hooks/telegram/<name>.\n"
        "Repeats hourly while failing; the CloudWatch alarm TelegramWebhookUnhealthy sends OK on recovery.\n"
    )
    (ses or ses_client()).send_email(
        Source=operator_sender(),
        Destination={"ToAddresses": [operator_recipient()]},
        Message={
            "Subject": {"Data": f"CRITICAL: Telegram webhook failing ({names})", "Charset": "utf-8"},
            "Body": {"Text": {"Data": body, "Charset": "utf-8"}},
        },
    )
    return True


def handler(event=None, context=None):
    """Scheduled check: metric every run, operator email while failing.

    A probe failure counts as failing — a check that cannot see Telegram
    must not report healthy."""
    rows = check()
    failing = sum(1 for row in rows if row["status"] != "ok")
    _put_metric(failing)
    alerted = False
    if failing:
        logger.error("telegram webhook unhealthy:\n%s", summary(rows))
        alerted = _alert(rows)
    return {"checked": len(rows), "failing": failing, "alerted": alerted, "triggers": rows}
