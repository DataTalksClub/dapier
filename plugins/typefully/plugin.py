"""Typefully v2 ops using the write-only Dapier credential (never echoed)."""
import json
from urllib.parse import urlencode

from src.dapier.connections import credentials
from src.dapier.connectors.registry import Discovery, ConnectionTest, register_discovery, register_connection_test
from src.dapier.engine.actions import base

API_ROOT = "https://api.typefully.com/v2"
DEFAULT_SOCIAL_SET_ID = 182343


def request(path, connection=None, *, method="GET", body=None, transport=None):
    secret = credentials.get_credential((connection or {}).get("credential_id") or "typefully")
    key = secret.get("api_key")
    if not isinstance(key, str) or not key.strip():
        raise ValueError("Typefully API key is not configured")
    headers = {"authorization": "Bearer " + key, "accept": "application/json"}
    payload = None
    if body is not None:
        headers["content-type"] = "application/json"
        payload = json.dumps(body).encode()
    try:
        status, raw = (transport or base._default_transport)(
            method, API_ROOT + path, headers=headers, body=payload, timeout=30)
    except Exception:
        raise ValueError("Typefully is unreachable") from None
    if status not in (200, 201):
        raise ValueError(f"Typefully returned HTTP {status}")
    if status == 204 or raw in (None, b"", ""):
        return {}
    try:
        data = raw if isinstance(raw, dict) else json.loads(raw)
    except (ValueError, TypeError):
        raise ValueError("Typefully returned invalid JSON") from None
    if not isinstance(data, dict):
        raise ValueError("Typefully returned an invalid response")
    return data


def _social_set_id(params):
    raw = (params or {}).get("social_set_id")
    if raw in (None, ""):
        return DEFAULT_SOCIAL_SET_ID
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError("social_set_id must be a positive integer") from None
    if value < 1:
        raise ValueError("social_set_id must be a positive integer")
    return value


def social_sets(connection, params):
    # Construct each offset locally; never forward credentials to a response URL.
    items = []
    for offset in range(0, 10000, 50):
        data = request(f"/social-sets?limit=50&offset={offset}", connection)
        page = data.get("results")
        if not isinstance(page, list):
            raise ValueError("Typefully returned invalid social sets")
        for item in page:
            if isinstance(item, dict) and item.get("id") is not None:
                items.append({"id": str(item["id"]), "name": item.get("name") or item.get("username") or str(item["id"]),
                              "username": item.get("username"), "team": item.get("team")})
        if not data.get("next"):
            return items
        if not page:
            raise ValueError("Typefully returned an empty page with more results")
    raise ValueError("Typefully social set listing exceeded the pagination limit")


def list_drafts(connection, params):
    social_set_id = _social_set_id(params)
    status = (params or {}).get("status")
    results = []
    for offset in range(0, 10000, 50):
        query = {"limit": 50, "offset": offset}
        if status:
            query["status"] = status
        data = request(f"/social-sets/{social_set_id}/drafts?" + urlencode(query), connection)
        page = data.get("results")
        if not isinstance(page, list):
            raise ValueError("Typefully returned invalid drafts")
        results.extend(page)
        if not data.get("next"):
            break
        if not page:
            raise ValueError("Typefully returned an empty drafts page with more results")
    else:
        raise ValueError("Typefully draft listing exceeded the pagination limit")
    items = []
    for item in results:
        if not isinstance(item, dict) or item.get("id") is None:
            continue
        preview = item.get("preview") or item.get("draft_title") or str(item["id"])
        items.append({
            "id": str(item["id"]),
            "name": str(preview)[:120],
            "status": item.get("status"),
            "private_url": item.get("private_url"),
            "scheduled_date": item.get("scheduled_date"),
            "social_set_id": item.get("social_set_id") or social_set_id,
        })
    return items


def drafts_export(connection, params):
    """Full draft list payload for the agent/CLI (not discovery item shape)."""
    social_set_id = _social_set_id(params)
    status = (params or {}).get("status")
    results = []
    for offset in range(0, 10000, 50):
        query = {"limit": 50, "offset": offset}
        if status:
            query["status"] = status
        data = request(f"/social-sets/{social_set_id}/drafts?" + urlencode(query), connection)
        page = data.get("results")
        if not isinstance(page, list):
            raise ValueError("Typefully returned invalid drafts")
        results.extend(page)
        if not data.get("next"):
            return {"results": results, "count": len(results), "social_set_id": social_set_id}
        if not page:
            raise ValueError("Typefully returned an empty drafts page with more results")
    raise ValueError("Typefully draft listing exceeded the pagination limit")


def get_draft(connection, params):
    social_set_id = _social_set_id(params)
    try:
        draft_id = int((params or {}).get("draft_id"))
    except (TypeError, ValueError):
        raise ValueError("draft_id must be a positive integer") from None
    if draft_id < 1:
        raise ValueError("draft_id must be a positive integer")
    return request(f"/social-sets/{social_set_id}/drafts/{draft_id}", connection)


def get_queue(connection, params):
    social_set_id = _social_set_id(params)
    start_date = (params or {}).get("start_date")
    end_date = (params or {}).get("end_date")
    if not start_date or not end_date:
        raise ValueError("queue requires start_date and end_date (YYYY-MM-DD)")
    query = urlencode({"start_date": start_date, "end_date": end_date})
    return request(f"/social-sets/{social_set_id}/queue?" + query, connection)


def create_unscheduled_draft(connection, params):
    """Create a plain draft. Rejects schedule/publish fields (explicit approval required)."""
    social_set_id = _social_set_id(params)
    body = (params or {}).get("body")
    if not isinstance(body, dict):
        raise ValueError("create-draft requires a JSON body with platforms")
    for forbidden in ("publish_at", "plan_at"):
        if forbidden in body and body[forbidden] is not None:
            raise ValueError(f"{forbidden} requires explicit approval; omit it for an unscheduled draft")
    if "platforms" not in body or not isinstance(body.get("platforms"), dict):
        raise ValueError("platforms object is required")
    return request(f"/social-sets/{social_set_id}/drafts", connection, method="POST", body=body)


def health(connection):
    try:
        user = request("/me", connection)
        if not user.get("id"):
            raise ValueError("Typefully returned no user identity")
        return {"ok": True, "detail": "Typefully API key verified", "identity": {
            "id": user["id"], "name": user.get("name"), "email": user.get("email")}}
    except Exception:
        # Provider bodies and transport errors can contain secrets; never echo them.
        return {"ok": False, "detail": "Typefully key check failed; check the saved key and provider availability"}


register_discovery(Discovery(name="social_sets", connector="typefully", label="Social sets",
    description="Typefully social accounts available to the saved API key", run=social_sets))
register_discovery(Discovery(name="drafts", connector="typefully", label="Drafts",
    description="Drafts for a social set (default DTC 182343)", run=list_drafts,
    params=(
        {"key": "social_set_id", "label": "Social set id", "type": "text", "required": False,
         "default": str(DEFAULT_SOCIAL_SET_ID), "help": "Defaults to DataTalksClub 182343"},
        {"key": "status", "label": "Status", "type": "text", "required": False,
         "help": "draft|scheduled|planned|published|error|publishing"},
    )))
register_connection_test(ConnectionTest(connector="typefully", run=health))


from src.dapier.connectors.trigger_discovery import (
    TriggerDiscovery, register_trigger_discovery, options_from_registry, DEFAULT_LIMIT,
)


def social_set_options(event=None, connection_id=None, limit=DEFAULT_LIMIT):
    if connection_id == "typefully":
        connection_id = None
    return options_from_registry("typefully.social_sets", connection_id, limit,
        provider="typefully", credential_fallback="typefully")


register_trigger_discovery(TriggerDiscovery(connector="typefully", label="Typefully",
    kind="options", resource="typefully.social_sets", fetch=social_set_options))
