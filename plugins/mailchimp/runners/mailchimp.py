"""Mailchimp Marketing API v3 actions: find one audience member by email and
add or update one (upsert), authenticated with the stored Mailchimp API key.

The credential is ``{"apiKey": "…-us12", "server": "us12"}`` — the datacenter
suffix rides in the key, and the API root is derived from it. Every request
is basic auth with an arbitrary username and the API key as the password.
"""

import hashlib
import json

from src.dapier.connections import credentials
from src.dapier.connections.providers.mailchimp_api import mailchimp_request
from src.dapier.engine.actions import base
from src.dapier.engine.actions.templating import render

DEFAULT_CREDENTIAL_ID = "mailchimp"
STATUSES = ("subscribed", "pending", "unsubscribed", "cleaned")
TAG_ACTIONS = ("add", "remove")
API_ROOT = "https://{server}.api.mailchimp.com/3.0"


def _api_settings(action):
    """The (api_key, server) pair behind the action's credential."""
    credential_id = str(action.get("credential_id") or DEFAULT_CREDENTIAL_ID).strip()
    if action.get("connection_id"):
        record = base._connected_connection(action["connection_id"])
        credential_id = str(record.get("credential_id") or credential_id)
    try:
        secret = credentials.get_credential(credential_id)
    except KeyError:
        raise ValueError(f"credential {credential_id} is not configured") from None
    api_key = str(secret.get("apiKey") or secret.get("api_key") or "").strip()
    server = str(secret.get("server") or "").strip()
    if not api_key:
        raise ValueError(f"credential {credential_id} does not contain a Mailchimp API key")
    if not server:
        server = api_key.rsplit("-", 1)[-1] if "-" in api_key else ""
    if not server:
        raise ValueError("the Mailchimp API key needs its datacenter suffix (e.g. abc123-us12)")
    return api_key, server


def _call(method, path, action, *, payload=None, transport=None):
    api_key, server = _api_settings(action)
    return mailchimp_request(method, API_ROOT.format(server=server) + path, api_key,
                             payload=payload, transport=transport)


def _api_path(list_id, email):
    digest = hashlib.md5(str(email).strip().lower().encode()).hexdigest()
    return f"/lists/{list_id}/members/{digest}"


def _rendered(action, key, event, steps):
    return render(str(action.get(key) or ""), event, steps).strip()


def _detail(member):
    text = str((member or {}).get("detail") or (member or {}).get("title") or "").strip()
    return text or "no detail"


def _member_out(member):
    return {
        "id": member.get("id"),
        "email": member.get("email_address"),
        "status": member.get("status"),
        "merge_fields": member.get("merge_fields") or {},
        "list_id": member.get("list_id"),
        "unique_email_id": member.get("unique_email_id"),
    }


def _flag(action, key):
    """Designer boolean fields arrive as "true"/"false" strings."""
    value = action.get(key)
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return bool(value)


def run_mailchimp_find_member(action, event, *, transport=None, steps=None):
    """Find one audience member by email; a miss is ``found: False``, not an
    error. ``create_if_missing`` turns the miss into Zapier's Find or Create
    Member: the upsert path creates the member from ``status`` (as
    status_if_new) and ``merge_fields`` and reports ``created: True``; a hit
    never touches the member."""
    list_id = _rendered(action, "list_id", event, steps)
    email = _rendered(action, "email", event, steps)
    if not list_id or not email:
        raise ValueError("mailchimp_find_member requires list_id and email")
    status, member = _call("GET", _api_path(list_id, email), action, transport=transport)
    if status == 404:
        if not _flag(action, "create_if_missing"):
            return {"found": False, "member": None}
        status_if_new = _rendered(action, "status", event, steps).lower() or "subscribed"
        if status_if_new not in STATUSES:
            raise ValueError(f"status must be one of: {', '.join(STATUSES)}")
        payload = {"email_address": email, "status_if_new": status_if_new}
        merge_raw = _rendered(action, "merge_fields", event, steps)
        if merge_raw:
            try:
                merge = json.loads(merge_raw)
            except ValueError:
                raise ValueError("merge_fields must be a JSON object") from None
            if not isinstance(merge, dict):
                raise ValueError("merge_fields must be a JSON object")
            payload["merge_fields"] = merge
        status, member = _call("PUT", _api_path(list_id, email), action,
                               payload=payload, transport=transport)
        if status >= 300:
            raise RuntimeError(f"Mailchimp member create returned HTTP {status}: {_detail(member)}")
        return {"found": True, "member": _member_out(member), "created": True}
    if status >= 300:
        raise RuntimeError(f"Mailchimp member lookup returned HTTP {status}: {_detail(member)}")
    return {"found": True, "member": _member_out(member)}


def run_mailchimp_upsert_member(action, event, *, transport=None, steps=None):
    """Add the member or update the existing one (PUT on the email's md5
    path); Status applies to members the audience does not have yet."""
    list_id = _rendered(action, "list_id", event, steps)
    email = _rendered(action, "email", event, steps)
    if not list_id or not email:
        raise ValueError("mailchimp_upsert_member requires list_id and email")
    status_if_new = _rendered(action, "status", event, steps).lower() or "subscribed"
    if status_if_new not in STATUSES:
        raise ValueError(f"status must be one of: {', '.join(STATUSES)}")
    payload = {"email_address": email, "status_if_new": status_if_new}
    merge_raw = _rendered(action, "merge_fields", event, steps)
    if merge_raw:
        try:
            merge = json.loads(merge_raw)
        except ValueError:
            raise ValueError("merge_fields must be a JSON object") from None
        if not isinstance(merge, dict):
            raise ValueError("merge_fields must be a JSON object")
        payload["merge_fields"] = merge
    status, member = _call("PUT", _api_path(list_id, email), action,
                           payload=payload, transport=transport)
    if status >= 300:
        raise RuntimeError(f"Mailchimp upsert returned HTTP {status}: {_detail(member)}")
    return {"updated": True, "member": _member_out(member)}


def run_mailchimp_remove_member(action, event, *, transport=None, steps=None):
    """Permanently remove one audience member by email (DELETE on the email's
    md5 path); a missing member is ``removed: False``, not an error."""
    list_id = _rendered(action, "list_id", event, steps)
    email = _rendered(action, "email", event, steps)
    if not list_id or not email:
        raise ValueError("mailchimp_remove_member requires list_id and email")
    status, member = _call("DELETE", _api_path(list_id, email), action,
                           transport=transport)
    if status == 404:
        return {"removed": False, "email": email}
    if status >= 300:
        raise RuntimeError(f"Mailchimp member removal returned HTTP {status}: {_detail(member)}")
    return {"removed": True, "email": email}


def run_mailchimp_unsubscribe_member(action, event, *, transport=None, steps=None):
    """Unsubscribe one audience member by email (PATCH status → unsubscribed,
    Zapier's Unsubscribe Member) — the reversible counterpart of the permanent
    remove: the member stays on the audience. A missing member is
    ``unsubscribed: False``, not an error. Output carries the member's new
    status for the next step."""
    list_id = _rendered(action, "list_id", event, steps)
    email = _rendered(action, "email", event, steps)
    if not list_id or not email:
        raise ValueError("mailchimp_unsubscribe_member requires list_id and email")
    status, member = _call("PATCH", _api_path(list_id, email), action,
                           payload={"status": "unsubscribed"}, transport=transport)
    if status == 404:
        return {"unsubscribed": False, "email": email}
    if status >= 300:
        raise RuntimeError(f"Mailchimp unsubscribe returned HTTP {status}: {_detail(member)}")
    return {"unsubscribed": True, "email": email, "member": _member_out(member)}


def run_mailchimp_tag_member(action, event, *, transport=None, steps=None):
    """Add or remove one tag on an audience member (POST on the email's
    ``tags`` subresource); the member must exist."""
    list_id = _rendered(action, "list_id", event, steps)
    email = _rendered(action, "email", event, steps)
    tag = _rendered(action, "tag", event, steps)
    if not list_id or not email or not tag:
        raise ValueError("mailchimp_tag_member requires list_id, email and tag")
    tag_action = _rendered(action, "tag_action", event, steps).lower() or "add"
    if tag_action not in TAG_ACTIONS:
        raise ValueError(f"tag_action must be one of: {', '.join(TAG_ACTIONS)}")
    tag_status = "active" if tag_action == "add" else "inactive"
    status, member = _call("POST", _api_path(list_id, email) + "/tags", action,
                           payload={"tags": [{"name": tag, "status": tag_status}]},
                           transport=transport)
    if status >= 300:
        raise RuntimeError(f"Mailchimp tag update returned HTTP {status}: {_detail(member)}")
    return {"tagged": True, "email": email, "tag": tag, "tag_status": tag_status}
