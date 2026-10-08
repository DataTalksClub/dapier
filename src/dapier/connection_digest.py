"""Scheduled connection-expiry digest: tokens about to lapse, as a daily email.

ConnectionDigestFunction (template.yaml) invokes handler() on a daily
EventBridge schedule. It reads the same connections register the console
and CLI read (records.api_list_connections rendered through public_view —
expiry comes from the credentials store, never the secrets themselves) and
emails the operator every connection whose token expires within the window
(default 48h, ``DAPIER_CONNECTION_DIGEST_HOURS``) or has already expired,
so it can be re-authenticated (``dapier connections connect <id>``) before
a workflow fails on it. A register with nothing expiring sends nothing:
the digest exists to surface upcoming lapses, not to confirm quiet days.

Each listed connection carries a one-click re-auth link — the same GET the
console Reconnect button issues, wrapped in ``/auth/login`` so a cold phone
browser signs in before provider consent — because an email is read where a
shell command is not. The base comes from ``OAUTH_CALLBACK_URL``; without it
the email falls back to the CLI instruction alone.

The console Connections register shows the same lapses on the row
(status + Reconnect). There is no send-now email button: this schedule is
the email path. Operator fire-now is CLI-only:
``dapier connections send-expiry-digest`` → POST
/api/agent/connections/expiry-digest, which calls ``send()`` and returns
what was sent (or ``{"skipped": true}``).
"""
import os
import urllib.parse
from datetime import datetime, timedelta, timezone


def _window_hours():
    """Expiry-warning horizon in hours (DAPIER_CONNECTION_DIGEST_HOURS, default 48)."""
    try:
        return max(1, int(os.environ.get("DAPIER_CONNECTION_DIGEST_HOURS") or 48))
    except (TypeError, ValueError):
        return 48


def _connections_table():
    import boto3

    return boto3.resource("dynamodb").Table(os.environ["CONNECTIONS_TABLE"])


def views(table=None):
    """Every connection as ``public_view`` metadata plus token health.

    The same rows the console register shows: the expiry is read per
    connection from the credentials store (best effort — a store hiccup
    degrades that row to the status-only view instead of failing the
    digest).
    """
    from .connections import credentials as connection_credentials
    from .connections import records as connection_records

    table = table if table is not None else _connections_table()
    rows, next_token = [], None
    while True:
        status, payload = connection_records.api_list_connections(
            table, limit=200, next_token=next_token)
        if status != 200:
            raise RuntimeError(payload.get("error") or "connections list failed")
        for item in payload.get("connections") or []:
            try:
                record = connection_credentials.get_credential_record(
                    connection_records.credential_id_for(item.get("connection_id")))
                value = record.get("value")
                stored = value if isinstance(value, dict) else {}
            except Exception:  # noqa: BLE001 — expiry is best-effort, never block the digest
                stored = {}
            rows.append(connection_records.public_view(item, stored))
        next_token = (payload.get("paging") or {}).get("next")
        if not next_token:
            return rows


def expiring(window_hours=None, *, now=None, table=None):
    """Connections whose token expires within the window, or already has.

    Each row carries ``expires_state``: ``"expired"`` — past its expiry or
    revoked, reconnection needed now — or ``"expiring"`` (inside the
    horizon). Connections without an expiring token (pasted bot tokens,
    awaiting consent) never appear. Rows sort by expiry, soonest first.
    """
    now = now or datetime.now(timezone.utc)
    hours = window_hours if window_hours is not None else _window_hours()
    deadline = now + timedelta(hours=hours)
    rows = []
    for view in views(table):
        if view.get("health") == "expired":
            rows.append({**view, "expires_state": "expired"})
            continue
        expires = view.get("token_expires_at")
        if not expires:
            continue
        try:
            when = datetime.fromisoformat(str(expires))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when <= deadline:
            rows.append({**view, "expires_state": "expiring"})
    rows.sort(key=lambda row: (row.get("token_expires_at") or "",
                               row.get("connection_id") or ""))
    return rows


def reauth_url(connection_id):
    """One-click re-auth link for one connection, or "" when unconfigured.

    ``/auth/login?next=...`` lands on the OAuth start endpoint after the
    operator sign-in (a session already present skips straight through),
    and the start endpoint is exactly the GET the console Connect button
    issues — so the link works from a cold phone browser. The base is the
    registered callback's origin; never derived from request headers, the
    digest runs on a schedule with none.
    """
    callback = os.environ.get("OAUTH_CALLBACK_URL", "").strip()
    if not callback:
        return ""
    suffix = "/oauth/callback"
    base = callback[: -len(suffix)] if callback.endswith(suffix) else callback.rstrip("/")
    start = f"/api/admin/oauth/{connection_id}/start"
    return f"{base}/auth/login?next={urllib.parse.quote(start, safe='/')}"


def render(rows, window_hours=None):
    """Subject and plain-text body for one ``expiring()`` payload.

    One line per connection with its expiry, a click-to-reconnect link,
    and the re-auth command, so the email is actionable on its own —
    phone or desktop.
    """
    hours = window_hours if window_hours is not None else _window_hours()
    count = len(rows)
    expired = sum(1 for row in rows if row.get("expires_state") == "expired")
    subject = (f"dapier connection digest: {count} connection token(s) "
               "need re-authentication")
    lines = [
        f"{count} connection token(s) expire within {hours}h"
        + (f" ({expired} already expired)" if expired else "")
        + ". Reconnect before a workflow fails on them:", "",
    ]
    for row in rows:
        connection_id = row.get("connection_id")
        name = row.get("display_name") or connection_id
        state = "EXPIRED" if row.get("expires_state") == "expired" else "expiring"
        lines.append(f"- {name} [{connection_id}] "
                     f"({row.get('provider')}): {state} at "
                     f"{row.get('token_expires_at')}")
        url = reauth_url(connection_id)
        if url:
            lines.append(f"  reconnect now (one click): {url}")
            lines.append(f"  (or: dapier connections connect {connection_id})")
        else:
            lines.append(f"  re-authenticate: dapier connections connect "
                         f"{connection_id}")
    lines.append("")
    lines.append("The console Connections page reconnects the same accounts.")
    return subject, "\n".join(lines) + "\n"


def send(window_hours=None, *, ses=None, now=None):
    """Email the operator the connections expiring in the window.

    Returns what was sent (``sent``, recipient, subject, body, connection
    ids) or ``{"skipped": True, ...}`` when nothing expires in the window —
    no SES call, no noise email.
    """
    from .engine.notify import operator_recipient, operator_sender, ses_client

    hours = window_hours if window_hours is not None else _window_hours()
    rows = expiring(hours, now=now)
    if not rows:
        return {"skipped": True, "window_hours": hours, "expiring": 0}
    subject, body = render(rows, hours)
    recipient = operator_recipient()
    if ses is None:
        ses = ses_client()
    response = ses.send_email(
        Source=operator_sender(),
        Destination={"ToAddresses": [recipient]},
        Message={
            "Subject": {"Data": subject, "Charset": "utf-8"},
            "Body": {"Text": {"Data": body, "Charset": "utf-8"}},
        },
    )
    return {
        "sent": True,
        "to": recipient,
        "subject": subject,
        "body": body,
        "message_id": response.get("MessageId"),
        "window_hours": hours,
        "connections": [row.get("connection_id") for row in rows],
    }


def handler(event, context=None):
    return send()
