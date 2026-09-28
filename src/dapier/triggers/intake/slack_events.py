"""Verify Slack Events API deliveries and publish workflow events.

Connection-scoped like Zoom (``/hooks/slack/{connection_id}``): each Slack
connection gets its own Request URL, so one Slack app can serve one workspace
per connection. Slack signs every delivery — the ``url_verification``
handshake included — as ``v0=<hmac-sha256(signing_secret,
"v0:{timestamp}:{raw body}")>`` echoed in ``X-Slack-Signature`` with a
``X-Slack-Request-Timestamp``; stale (5 min) or unsigned deliveries are
rejected. The signing secret is an app-level value, so it is pasted into the
connection alongside the bot token (see ``connections.importing``).

Published events are the four workflow-facing names in ``EVENTS``, each
mirroring one Zapier-style trigger chip:

- ``message.received`` — the ``message.*`` family (including
  ``message.channels/groups/im/mpim`` and edit subtypes),
- ``app.mention`` — ``app_mention`` (@-mentions of the bot),
- ``reaction.added`` — ``reaction_added``,
- ``member.joined`` — ``member_joined_channel``.

Backward compatibility: until the variety change ``app_mention`` was folded
into ``message.received``; going forward it publishes **only**
``app.mention``. Stored workflows that relied on mentions firing
``message.received`` must re-filter on ``app.mention`` — while every
``message.*`` delivery keeps publishing ``message.received`` exactly as
before (same name, same envelope, same dedup identity), so existing
message workflows keep working unchanged.

Every other subscribed event answers ``accepted: False`` so Slack stops
retrying it. Posts made by apps (``bot_id`` present) never publish: a
workflow that posts into the channel it listens on would otherwise loop
forever. Everything else — channel, user, text, ts, reaction, inviter,
plus the raw event — travels in the envelope for templates and trigger
filters.
"""

import hashlib
import hmac
import json
import time

from ...connections import credentials
from ...connections import records

SLACK_EVENT = "message.received"
EVENTS = ("message.received", "app.mention", "reaction.added", "member.joined")

# Slack event type -> the workflow-facing event name it publishes.
_EVENT_NAMES = {
    "app_mention": "app.mention",
    "reaction_added": "reaction.added",
    "member_joined_channel": "member.joined",
}


def event_name(slack_type):
    """The published event for one Slack event type, or None when dapier does
    not subscribe to it (``message.*`` all fold into ``message.received``)."""
    name = str(slack_type or "")
    if name.startswith("message"):
        return SLACK_EVENT
    return _EVENT_NAMES.get(name)


def verify(headers, body, secret, *, now=None):
    """Slack signs v0:<timestamp>:<raw body>; reject stale deliveries."""
    normalized = {str(key).lower(): str(value) for key, value in (headers or {}).items()}
    timestamp = normalized.get("x-slack-request-timestamp", "")
    signature = normalized.get("x-slack-signature", "")
    try:
        sent = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs(int(now if now is not None else time.time()) - sent) > 300:
        return False
    expected = "v0=" + hmac.new(secret.encode(), b"v0:" + timestamp.encode() + b":" + body,
                                hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def event_data(payload, connection_id):
    """The published event data for one Events API callback.

    Shared by the ingress hook (``api.router``) and live trigger discovery,
    so a pulled sample is exactly the shape a real delivery publishes. The
    raw Slack event rides along under ``event`` for fields templates need
    beyond the flattened ones.
    """
    event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
    item = event.get("item") if isinstance(event.get("item"), dict) else {}
    return {
        "connection_id": connection_id,
        "team_id": payload.get("team_id"),
        "event_id": payload.get("event_id"),
        "event_time": payload.get("event_time"),
        "type": event.get("type"),
        "subtype": event.get("subtype"),
        # reaction_added nests the channel inside ``item``; everything else
        # has it at the top level.
        "channel_id": event.get("channel") or item.get("channel"),
        "user_id": event.get("user"),
        "text": event.get("text"),
        "ts": event.get("ts") or event.get("event_ts"),
        "thread_ts": event.get("thread_ts"),
        "reaction": event.get("reaction"),
        "item_ts": item.get("ts"),
        "inviter": event.get("inviter"),
        "event": event,
    }


def handle(connection_id, headers, body, *, connections_table, publish, now=None):
    """Return (HTTP status, JSON body) for a connection-specific Events URL."""
    try:
        connection_id = records.validate_connection_id(connection_id)
    except records.ConnectionError:
        return 404, {"error": "not found"}
    connection = records.get_connection(connections_table, connection_id)
    if not connection or connection.get("provider") != "slack" or connection.get("status") == "revoked":
        return 404, {"error": "not found"}
    try:
        secret = credentials.get_credential(connection["credential_id"])["signing_secret"]
    except (KeyError, TypeError):
        return 503, {"error": "Slack Events signing secret is not configured"}
    if not verify(headers, body, secret, now=now):
        return 401, {"error": "invalid signature"}
    try:
        message = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return 400, {"error": "invalid JSON"}
    if not isinstance(message, dict):
        return 400, {"error": "invalid Slack event"}
    if message.get("type") == "url_verification":
        challenge = message.get("challenge")
        if not isinstance(challenge, str) or not challenge or len(challenge) > 512:
            return 400, {"error": "invalid challenge"}
        return 200, {"challenge": challenge}
    if message.get("type") != "event_callback":
        return 200, {"accepted": False}
    event = message.get("event")
    if not isinstance(event, dict):
        return 200, {"accepted": False}
    if event.get("bot_id"):
        # App posts never publish: a workflow listening on the channel it
        # posts into would loop on its own messages otherwise.
        return 200, {"accepted": False}
    event_type = str(event.get("type") or "")
    name = event_name(event_type)
    if name is None:
        return 200, {"accepted": False}
    team_id = message.get("team_id")
    if team_id:
        try:
            records.check_binding(connection, team_id)
        except records.BindingError:
            return 403, {"error": "Slack workspace does not match connection"}
    event_id = "slack:" + hashlib.sha256(
        f"{connection_id}:{message.get('event_id')}".encode()).hexdigest()[:32]
    publish("slack", name,
            {"connection_id": connection_id, **event_data(message, connection_id)},
            source=connection_id, event_id=event_id)
    return 200, {"accepted": True}
