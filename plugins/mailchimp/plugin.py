"""Mailchimp connector: audience/member discovery, the stored-API-key health
check, the find/add-update/remove/unsubscribe/tag member actions, the "new
member" poll source — all behind the stored Mailchimp credential (Marketing
API v3, basic auth with the key)."""
import urllib.parse

from plugins.mailchimp.runners import mailchimp
from plugins.mailchimp.runners.mailchimp import (
    DEFAULT_CREDENTIAL_ID,
    run_mailchimp_find_member,
    run_mailchimp_remove_member,
    run_mailchimp_tag_member,
    run_mailchimp_unsubscribe_member,
    run_mailchimp_upsert_member,
)
from src.dapier.connectors import trigger_discovery
from src.dapier.connectors.registry import (
    Action,
    Connector,
    ConnectionTest,
    Discovery,
    connector,
    register,
    register_connection_test,
    register_discovery,
)
from src.dapier.connectors.trigger_discovery import (
    DEFAULT_LIMIT,
    TriggerDiscovery,
    options_from_registry,
    per_event_sample_fetch,
    register_trigger_discovery,
)
from src.dapier.connections.providers.mailchimp_api import (
    mailchimp_request,
    stored_settings,
)
from src.dapier.triggers.poll_sources import PollSource, register_source

connector(Connector(name="mailchimp", label="Mailchimp",
                    events=("subscribe", "unsubscribe", "profile", "upemail",
                            "cleaned", "campaign", "member.new"),
                    icon="mail",
                    event_info={
                        "subscribe": ("Subscribed", "Someone joins the audience"),
                        "unsubscribe": ("Unsubscribed", "Someone leaves the audience"),
                        "profile": ("Profile updated", "A subscriber changes their profile fields"),
                        "upemail": ("Email changed", "A subscriber changes their email address"),
                        "cleaned": ("Address cleaned", "Mailchimp removes an address that keeps bouncing"),
                        "campaign": ("Campaign sent", "A campaign is sent to the audience"),
                        "member.new": ("New member (poll)", "A polled audience lists a member not seen before"),
                    }))

register(Action(
    type="mailchimp_find_member",
    label="Mailchimp: find member",
    icon="mail",
    description="Find one audience member by email (Find Member). Output: "
                "{found: true, member: {email, status, merge_fields, ...}}; "
                "a miss is {found: false, member: null} — or, with Create if "
                "missing on, the member is created (status applies as "
                "status-if-new) and the output reports created: true "
                "(Find or Create Member).",
    run=lambda action, event, workflow_id, steps=None: run_mailchimp_find_member(
        action, event, steps=steps),
    required=frozenset({"list_id", "email"}),
    optional=frozenset({"create_if_missing", "status", "merge_fields",
                        "credential_id", "connection_id"}),
    fields=(
        {"key": "credential_id", "label": "Credential ID", "placeholder": "mailchimp (default)"},
        {"key": "list_id", "label": "Audience", "required": True,
         "discover": {"resource": "mailchimp.audiences"}},
        {"key": "email", "label": "Email", "type": "email", "required": True,
         "placeholder": "{sender}",
         "discover": {"resource": "mailchimp.members", "params": {"list_id": "list_id"}}},
        {"key": "create_if_missing", "label": "Create if missing", "type": "boolean",
         "default": "false",
         "help": "Create the member when the audience does not have them yet"},
        {"key": "status", "label": "Status if new", "type": "select",
         "options": ["subscribed", "pending", "unsubscribed", "cleaned"],
         "default": "subscribed",
         "help": "Only used when Create if missing is on"},
        {"key": "merge_fields", "label": "Merge fields (JSON)", "type": "textarea",
         "placeholder": '{"FNAME": "{name}"}',
         "help": "Only used when Create if missing is on"},
    ),
))

register(Action(
    type="mailchimp_upsert_member",
    label="Mailchimp",
    icon="mail",
    description="Add or update one audience member (Add/Update Member): the "
                "email's record is created with Status when missing, or "
                "updated when it exists. Merge fields is a JSON object.",
    run=lambda action, event, workflow_id, steps=None: run_mailchimp_upsert_member(
        action, event, steps=steps),
    required=frozenset({"list_id", "email"}),
    optional=frozenset({"status", "merge_fields", "credential_id", "connection_id"}),
    fields=(
        {"key": "credential_id", "label": "Credential ID", "placeholder": "mailchimp (default)"},
        {"key": "list_id", "label": "Audience", "required": True,
         "discover": {"resource": "mailchimp.audiences"}},
        {"key": "email", "label": "Email", "type": "email", "required": True,
         "placeholder": "{sender}",
         "discover": {"resource": "mailchimp.members", "params": {"list_id": "list_id"}}},
        {"key": "status", "label": "Status if new", "type": "select",
         "options": ["subscribed", "pending", "unsubscribed", "cleaned"],
         "default": "subscribed"},
        {"key": "merge_fields", "label": "Merge fields (JSON)", "type": "textarea",
         "placeholder": '{"FNAME": "{name}"}'},
    ),
))

register(Action(
    type="mailchimp_remove_member",
    label="Mailchimp: remove member",
    icon="mail",
    description="Permanently remove one audience member by email (Delete "
                "Member). A missing email is not an error: the output is "
                "{removed: false} — guard with a find-member step when the "
                "difference matters.",
    run=lambda action, event, workflow_id, steps=None: run_mailchimp_remove_member(
        action, event, steps=steps),
    required=frozenset({"list_id", "email"}),
    optional=frozenset({"credential_id", "connection_id"}),
    fields=(
        {"key": "credential_id", "label": "Credential ID", "placeholder": "mailchimp (default)"},
        {"key": "list_id", "label": "Audience", "required": True,
         "discover": {"resource": "mailchimp.audiences"}},
        {"key": "email", "label": "Email", "type": "email", "required": True,
         "placeholder": "{sender}",
         "discover": {"resource": "mailchimp.members", "params": {"list_id": "list_id"}}},
    ),
))

register(Action(
    type="mailchimp_unsubscribe_member",
    label="Mailchimp: unsubscribe member",
    icon="mail",
    description="Unsubscribe one audience member by email (Update Member "
                "status, Zapier's Unsubscribe Member) — reversible: the "
                "member stays on the audience with status unsubscribed, "
                "unlike the permanent remove. A missing email is not an "
                "error: the output is {unsubscribed: false}.",
    run=lambda action, event, workflow_id, steps=None: run_mailchimp_unsubscribe_member(
        action, event, steps=steps),
    required=frozenset({"list_id", "email"}),
    optional=frozenset({"credential_id", "connection_id"}),
    fields=(
        {"key": "credential_id", "label": "Credential ID", "placeholder": "mailchimp (default)"},
        {"key": "list_id", "label": "Audience", "required": True,
         "discover": {"resource": "mailchimp.audiences"}},
        {"key": "email", "label": "Email", "type": "email", "required": True,
         "placeholder": "{sender}",
         "discover": {"resource": "mailchimp.members", "params": {"list_id": "list_id"}}},
    ),
))

register(Action(
    type="mailchimp_tag_member",
    label="Mailchimp: tag member",
    icon="mail",
    description="Add or remove one tag on an audience member (Member Tags): "
                "Add applies the tag, Remove sets it inactive. The member "
                "must exist — upsert it first when unsure.",
    run=lambda action, event, workflow_id, steps=None: run_mailchimp_tag_member(
        action, event, steps=steps),
    required=frozenset({"list_id", "email", "tag"}),
    optional=frozenset({"tag_action", "credential_id", "connection_id"}),
    fields=(
        {"key": "credential_id", "label": "Credential ID", "placeholder": "mailchimp (default)"},
        {"key": "list_id", "label": "Audience", "required": True,
         "discover": {"resource": "mailchimp.audiences"}},
        {"key": "email", "label": "Email", "type": "email", "required": True,
         "placeholder": "{sender}",
         "discover": {"resource": "mailchimp.members", "params": {"list_id": "list_id"}}},
        {"key": "tag", "label": "Tag", "required": True, "placeholder": "digest-readers"},
        {"key": "tag_action", "label": "Operation", "type": "select",
         "options": ["add", "remove"], "default": "add"},
    ),
))


def _run_audiences(connection, params, *, transport=None):
    api_key, server = stored_settings(connection)
    status, data = mailchimp_request(
        "GET", f"https://{server}.api.mailchimp.com/3.0/lists?count=100",
        api_key, transport=transport)
    if status >= 300:
        raise RuntimeError(f"Mailchimp lists returned HTTP {status}")
    return [
        {"id": entry.get("id"), "name": entry.get("name"),
         "members": (entry.get("stats") or {}).get("member_count")}
        for entry in data.get("lists") or []
        if entry.get("id")
    ]


register_discovery(Discovery(
    name="audiences",
    connector="mailchimp",
    label="Audiences",
    description="Mailchimp audiences (lists) on the account, with member counts",
    run=_run_audiences,
))


def _run_members(connection, params, *, transport=None):
    api_key, server = stored_settings(connection)
    list_id = str(params.get("list_id") or "").strip()
    status, data = mailchimp_request(
        "GET",
        f"https://{server}.api.mailchimp.com/3.0/lists/{list_id}/members?count=100",
        api_key, transport=transport)
    if status >= 300:
        raise RuntimeError(f"Mailchimp members returned HTTP {status}")
    return [
        {"id": entry.get("email_address") or entry.get("id"),
         "name": entry.get("email_address"),
         "email": entry.get("email_address"),
         "status": entry.get("status")}
        for entry in data.get("members") or []
        if entry.get("email_address") or entry.get("id")
    ]


register_discovery(Discovery(
    name="members",
    connector="mailchimp",
    label="Audience members",
    description="Members of one audience, up to 100",
    params=(
        {"key": "list_id", "label": "Audience", "type": "text", "required": True,
         "help": "Audience ID from the audiences list"},
    ),
    run=_run_members,
))


def _run_test(connection):
    """Ping the Marketing API with the stored key (never raises)."""
    try:
        api_key, server = stored_settings(connection)
        status, data = mailchimp_request(
            "GET", f"https://{server}.api.mailchimp.com/3.0/ping", api_key)
    except Exception as exc:
        return {"ok": False, "detail": f"Mailchimp key check failed: {exc}"}
    if status >= 300:
        detail = ""
        if isinstance(data, dict):
            detail = str(data.get("detail") or data.get("title") or "")
        return {"ok": False,
                "detail": f"Mailchimp ping failed: {detail or f'HTTP {status}'}"}
    return {
        "ok": True,
        "detail": f"Mailchimp API key verified (datacenter {server})",
        "identity": {"server": server},
    }


register_connection_test(ConnectionTest(connector="mailchimp", run=_run_test))


# --- poll source: "New Member" on the poll-trigger schedule --------------------
#
# The webhook types above are push — Mailchimp POSTs them (the raw webhook
# registration calls live in core connections.providers.mailchimp_api, where
# triggers.hook_triggers reaches them without importing plugin code) — but
# Zapier's other audience staple, "New Subscriber", has no webhook: it is a
# poll against the members listing. The ``mailchimp.members`` source lists
# the audience through the Marketing API on the poll schedule with the
# stored key (the named ``connection_id`` when the poll carries one, else
# the shared ``mailchimp`` credential) and publishes ``mailchimp``/
# ``member.new`` events so workflows match the chip while staying scoped
# through the poll-name filter.
#
# The webhook types above are push — Mailchimp POSTs them — but Zapier's
# other audience staple, "New Subscriber", has no webhook: it is a poll
# against the members listing. The ``mailchimp.members`` source lists the
# audience through the Marketing API on the poll schedule with the stored
# key (the named ``connection_id`` when the poll carries one, else the
# shared ``mailchimp`` credential) and publishes ``mailchimp``/``member.new``
# events so workflows match the chip while staying scoped through the
# poll-name filter.

MAILCHIMP_POLL_PAGE_SIZE = 1000  # the Marketing API's members-page maximum

# Older than any Mailchimp last_changed: the chip's live member sample pulls
# against this watermark, so the audience's newest member answers before the
# trigger is even past its first (seeding) fire.
MAILCHIMP_EPOCH_CURSOR = "0000-01-01T00:00:00+00:00"


def _mailchimp_poll_validate(body):
    """Save-time fetch spec: ``list_id`` (required — the audience to watch)
    plus the fetch defaults every stored mailchimp poll carries."""
    from src.dapier.triggers.email_triggers import TriggerError

    body = body if isinstance(body, dict) else {}
    list_id = str(body.get("list_id") or "").strip()
    if not list_id:
        raise TriggerError("list_id is required: name the audience to watch")
    return {
        "list_id": list_id,
        "cursor_mode": "next_cursor",
        "id_path": "id",
        "url": "",
    }


def _poll_api_key(item):
    """The (api_key, server) pair behind a stored poll: the named mailchimp
    connection when one is stored, else the shared ``mailchimp`` credential."""
    from src.dapier.engine.actions import base

    connection = {}
    connection_id = str(item.get("connection_id") or "").strip()
    if connection_id:
        connection = base._connected_connection(connection_id)
    return stored_settings(connection)


def _mailchimp_member_out(entry):
    """One member listing entry flattened to the event data shape."""
    merges = entry.get("merge_fields") if isinstance(entry.get("merge_fields"), dict) else {}
    return {
        "id": entry.get("id"),
        "email": entry.get("email_address"),
        "status": entry.get("status"),
        "last_changed": str(entry.get("last_changed") or ""),
        "first_name": merges.get("FNAME") or "",
        "last_name": merges.get("LNAME") or "",
    }


def _list_audience_members(api_key, server, list_id, *, transport=None):
    """The audience's members, newest ``last_changed`` first, in the event
    data shape; raises ``RuntimeError`` on a failed listing."""
    params = urllib.parse.urlencode({
        "count": MAILCHIMP_POLL_PAGE_SIZE,
        "sort_field": "last_changed",
        "sort_dir": "DESC",
    })
    status, data = mailchimp.mailchimp_request(
        "GET", f"https://{server}.api.mailchimp.com/3.0/lists/{list_id}/members?{params}",
        api_key, transport=transport)
    if status >= 300:
        detail = str(data.get("detail") or data.get("title") or "") if isinstance(data, dict) else ""
        raise RuntimeError("mailchimp poll failed: members listing returned "
                           f"HTTP {status}" + (f": {detail}" if detail else ""))
    return [member for member in (_mailchimp_member_out(entry)
                                  for entry in data.get("members") or [])
            if member["id"] and member["last_changed"]]


def _mailchimp_poll_fetch(item, cursor=None, *, transport=None):
    """One poll page as ``(items, next_cursor)`` for ``triggers.poll_sources``.

    The cursor is the composite ``last_changed|id`` watermark, which text
    comparison orders chronologically while the member id keeps same-second
    changes distinct (the zoom and s3 sources compose their watermarks for
    the same reason). The first fire seeds it at the audience's newest
    member and emits nothing — existing members are history, not news.
    Later fires emit members changed strictly after it, oldest first. The
    seen-set then dedupes the member id, so a profile edit inside the
    dedupe window never re-fires an already-seen member — the event is
    ``member.new``, not ``member.updated``. Raises ``RuntimeError`` on a
    failed fetch, like every poll source.
    """
    api_key, server = _poll_api_key(item)
    list_id = str(item.get("list_id") or "").strip()
    if not list_id:
        raise RuntimeError("poll source 'mailchimp.members' needs a stored list_id")
    members = _list_audience_members(api_key, server, list_id, transport=transport)
    entries = sorted(
        ((f"{member['last_changed']}|{member['id']}", member) for member in members),
        key=lambda pair: pair[0])
    if cursor is None:
        # First fire: seed the watermark at the newest existing member
        # (None on an empty audience) without emitting anything.
        return [], (entries[-1][0] if entries else None)
    watermark = str(cursor)
    fresh = [member for key, member in entries if key > watermark]
    return fresh, (f"{fresh[-1]['last_changed']}|{fresh[-1]['id']}" if fresh else watermark)


def _mailchimp_poll_view(item):
    """The provider params ``public_view`` shows beside ``source``."""
    return {"list_id": item.get("list_id")}


register_source(PollSource(
    name="mailchimp.members", connector="mailchimp", event="member.new",
    label="Mailchimp", validate=_mailchimp_poll_validate,
    fetch=_mailchimp_poll_fetch, view=_mailchimp_poll_view))


def _stored_mailchimp_poll(name):
    """The stored poll trigger named by ``event`` when it watches the
    mailchimp.members source, or None. Poll ids cannot contain dots and
    event names do, so a per-event ask never matches a poll; every storage
    hiccup folds into the same None — sampling never raises for want of
    infrastructure (see docs/connector-coverage-audit.md)."""
    from src.dapier.triggers import poll_triggers

    name = str(name or "").strip().lower()
    if not name or "." in name:
        return None
    try:
        item = poll_triggers.get_item(name)
    except Exception:
        return None
    if not item or str(item.get("source") or "") != "mailchimp.members":
        return None
    return item


# --- trigger discovery: one documented payload per Mailchimp webhook type ------
#
# The events here are exactly the types Mailchimp POSTs to a registered
# webhook URL (the same set the Mailchimp chip declares in connectors.triggers)
# and the shape triggers.intake.mailchimp_webhooks publishes: the parsed
# webhook body — the type plus its data object — so a pulled sample is exactly
# what a real delivery carries. The registration `ping` (sent once when the
# webhook is saved) is not a workflow event: intake answers it 200 and the
# chip does not declare it. The event field of a discovery request picks the
# payload; history answers only when its replayed envelope carries the asked
# type — see per_event_sample_fetch.

_MERGES = {
    "EMAIL": "reader@example.test",
    "FNAME": "Reader",
    "LNAME": "Example",
    "INTERESTS": "",
}

_MAILCHIMP_EVENT_SAMPLES = {
    "subscribe": {
        "type": "subscribe",
        "data": {
            "list_id": "abc123",
            "email": "reader@example.test",
            "merges": dict(_MERGES),
            "ip_opt": "203.0.113.7",
            "ip_signup": "203.0.113.7",
        },
    },
    "unsubscribe": {
        "type": "unsubscribe",
        "data": {
            "action": "unsub",
            "list_id": "abc123",
            "email": "reader@example.test",
            "campaign_id": "f8a2c1d4e5",
            "reason": "manual",
        },
    },
    "profile": {
        "type": "profile",
        "data": {
            "list_id": "abc123",
            "email": "reader@example.test",
            "merges": dict(_MERGES, FNAME="Reade"),
            "changes": [{"name": "FNAME", "old": "Reader", "new": "Reade"}],
        },
    },
    "upemail": {
        "type": "upemail",
        "data": {
            "list_id": "abc123",
            "old_email": "reader@example.test",
            "new_email": "reader-new@example.test",
        },
    },
    "cleaned": {
        "type": "cleaned",
        "data": {
            "list_id": "abc123",
            "campaign_id": "f8a2c1d4e5",
            "email": "bounced@example.test",
            "reason": "hard",
        },
    },
    "campaign": {
        "type": "campaign",
        "data": {
            "id": "f8a2c1d4e5",
            "list_id": "abc123",
            "status": "sent",
            "title": "September digest",
            "subject": "What shipped this month",
            "send_time": "2026-09-27T10:00:00+00:00",
        },
    },
    # Not a Mailchimp webhook type: the member.new event comes from the
    # mailchimp.members poll source (below), so its documented sample is the
    # poll item's own shape, not a webhook envelope.
    "member.new": {
        "id": "c9f2a1b7d4e5f6038a17",
        "email": "reader@example.test",
        "status": "subscribed",
        "last_changed": "2026-09-27T10:00:00+00:00",
        "first_name": "Reader",
        "last_name": "Example",
    },
}


_MAILCHIMP_PER_EVENT_SAMPLES = per_event_sample_fetch(
    "mailchimp", "subscribe", _MAILCHIMP_EVENT_SAMPLES)


def _fetch_mailchimp_sample(event=None, connection_id=None, limit=DEFAULT_LIMIT):
    """The Mailchimp chip's sample pull.

    An ask that names a stored mailchimp.members poll (``event`` carries the
    poll id — no dots) prefers the audience's newest member right now
    (``source: "live"`` — the poll's own fetch against the epoch watermark,
    no stored cursor read or advanced) and falls back to the documented
    ``member.new`` example when the poll has nothing to show or the fetch
    fails. Every other ask — the webhook-type events, a dotted event name,
    no poll at all — runs the standard chain: recorded history carrying the
    asked type, else the documented per-type payload. A sample pull shows
    the payload shape, it never raises.
    """
    item = _stored_mailchimp_poll(event)
    if item is not None:
        from src.dapier.triggers import poll_triggers

        try:
            members, _next_cursor = _mailchimp_poll_fetch(item, MAILCHIMP_EPOCH_CURSOR)
            envelope = (poll_triggers.event_for(item, members[-1])
                        if members else None)
        except Exception:
            envelope = None
        if envelope is not None:
            return {
                "sample": trigger_discovery.as_sample(envelope),
                "source": "live",
                "connection_id": item.get("connection_id") or connection_id,
            }
        return _MAILCHIMP_PER_EVENT_SAMPLES("member.new", connection_id, limit)
    return _MAILCHIMP_PER_EVENT_SAMPLES(event, connection_id, limit)


register_trigger_discovery(TriggerDiscovery(
    connector="mailchimp", label="Mailchimp", kind="sample", resource="",
    fetch=_fetch_mailchimp_sample))


# --- trigger discovery: audience options for the action's list_id field --------


def _fetch_audience_options(event=None, connection_id=None, limit=DEFAULT_LIMIT):
    """Audience options via the registry listing (the stored Mailchimp API
    key when no connection record exists).

    ``mailchimp`` is a key credential, not an OAuth account: the pseudo
    account of that name (api.discovery.PSEUDO_CONNECTION_PROVIDERS) has no
    connection record, so it resolves like an absent id — first connected
    mailchimp connection if one exists, else the shared ``mailchimp``
    credential. The value is the audience id the actions store as
    ``list_id``, labeled with the audience name and its member count.
    """
    if connection_id and str(connection_id).strip().lower() == "mailchimp":
        connection_id = None  # the pseudo account resolves through the fallback
    return options_from_registry(
        "mailchimp.audiences", connection_id, limit, provider="mailchimp",
        credential_fallback="mailchimp", option_of=_audience_option)


def _audience_option(item):
    """``{value: list_id, label: "Name (N members)"}`` for one audience."""
    name = str(item.get("name") or item.get("id"))
    members = item.get("members")
    return {"value": item.get("id"),
            "label": f"{name} ({members} members)" if members is not None else name}


register_trigger_discovery(TriggerDiscovery(
    connector="mailchimp", label="Mailchimp", kind="options", resource="mailchimp.audiences",
    fetch=_fetch_audience_options))


def _fetch_member_options(event=None, connection_id=None, limit=DEFAULT_LIMIT):
    """Member options of one audience via the registry listing; the list id
    rides in ``event`` (trigger_discovery.listing_params). Same pseudo-account
    resolution as the audience options; the value is the member's email
    address, labeled with it and the subscription status."""
    if connection_id and str(connection_id).strip().lower() == "mailchimp":
        connection_id = None  # the pseudo account resolves through the fallback

    def option_of(item):
        email = item.get("email") or item.get("id")
        status = item.get("status")
        return {"value": email,
                "label": f"{email} ({status})" if status else email}

    return options_from_registry(
        "mailchimp.members", connection_id, limit, provider="mailchimp",
        credential_fallback="mailchimp",
        params=trigger_discovery.listing_params(event, ("list_id",)),
        option_of=option_of)


register_trigger_discovery(TriggerDiscovery(
    connector="mailchimp", label="Mailchimp", kind="options", resource="mailchimp.members",
    fetch=_fetch_member_options))
