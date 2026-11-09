"""The trigger inbox: list, get, replay."""
from ... import http
from ...triggers import inbox
from ...auth import session


def list_inbox(event):
    """Trigger inbox: every inbound event, matched or not.

"""
    query = event.get("queryStringParameters") or {}
    status, payload = inbox.api_list(
        query.get("connector"), query.get("limit", 25),
        next_token=query.get("next") or None,
        outcome=query.get("outcome") or None,
    )
    return http._json_response(status, payload)


def get_inbox_event(inbox_id):
    """One inbox event: the stored envelope and the workflows that matched."""
    status, payload = inbox.api_get(inbox_id)
    return http._json_response(status, payload)


def replay_inbox_event(inbox_id, operator):
    """Re-send an inbox event through the engine (fresh id, same data)."""
    status, payload = inbox.api_replay(inbox_id)
    if status == 202:
        session._audit_event(inbox_id, "triggers.inbox-replay",
                             operator or "unknown", outcome="ok")
    return http._json_response(status, payload)


