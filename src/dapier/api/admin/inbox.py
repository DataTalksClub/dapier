"""The trigger inbox: list, get, replay."""
from ... import http
from ...triggers import inbox
from ...auth import session


def list_inbox(event, visible=None):
    """Trigger inbox: every inbound event, matched or not.

    ``visible`` (G17 auth.visibility) read-filters the list for
    non-operators: events that matched at least one workflow they own;
    unmatched events stay visible to everyone."""
    query = event.get("queryStringParameters") or {}
    status, payload = inbox.api_list(
        query.get("connector"), query.get("limit", 25),
        next_token=query.get("next") or None,
        visible=visible,
    )
    return http._json_response(status, payload)


def get_inbox_event(inbox_id, visible=None):
    """One inbox event: the stored envelope and the workflows that matched.

    ``visible`` scopes the single read exactly as the list does: a hidden
    event answers like a missing one."""
    status, payload = inbox.api_get(inbox_id, visible=visible)
    return http._json_response(status, payload)


def replay_inbox_event(inbox_id, operator):
    """Re-send an inbox event through the engine (fresh id, same data)."""
    status, payload = inbox.api_replay(inbox_id)
    if status == 202:
        session._audit_event(inbox_id, "triggers.inbox-replay",
                             operator or "unknown", outcome="ok")
    return http._json_response(status, payload)


