"""Mailchimp Marketing API v3 actions: find one audience member by email and
add or update one (upsert), authenticated with the stored Mailchimp API key.

The credential is ``{"apiKey": "…-us12", "server": "us12"}`` — the datacenter
suffix rides in the key, and the API root is derived from it. Every request
is basic auth with an arbitrary username and the API key as the password.
"""

import base64
import hashlib
import json

from ...connections import credentials
from . import base
from .templating import render

DEFAULT_CREDENTIAL_ID = "mailchimp"
STATUSES = ("subscribed", "pending", "unsubscribed", "cleaned")
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


def mailchimp_request(method, url, api_key, *, payload=None, transport=None):
    """One Marketing API call: ``(status, parsed body)``, never raising on
    HTTP errors — callers turn status codes into outputs (404 = not found)."""
    transport = transport or base._default_transport
    headers = {
        "authorization": "Basic " + base64.b64encode(f"anystring:{api_key}".encode()).decode(),
        "accept": "application/json",
    }
    body = None
    if payload is not None:
        headers["content-type"] = "application/json"
        body = json.dumps(payload, separators=(",", ":")).encode()
    try:
        status, raw = transport(method, url, headers=headers, body=body, timeout=15)
    except Exception as exc:
        raise RuntimeError(f"Mailchimp unreachable: {type(exc).__name__}") from None
    if isinstance(raw, dict):
        data = raw
    else:
        try:
            data = json.loads(raw) if raw else {}
        except (ValueError, TypeError):
            data = {}
    return status, data if isinstance(data, dict) else {}


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


def run_mailchimp_find_member(action, event, *, transport=None, steps=None):
    """Find one audience member by email; a miss is ``found: False``, not an
    error."""
    list_id = _rendered(action, "list_id", event, steps)
    email = _rendered(action, "email", event, steps)
    if not list_id or not email:
        raise ValueError("mailchimp_find_member requires list_id and email")
    status, member = _call("GET", _api_path(list_id, email), action, transport=transport)
    if status == 404:
        return {"found": False, "member": None}
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
