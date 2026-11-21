"""Operator Google Docs proxy. Credentials remain on the server."""
import json
import re
from urllib.parse import quote

from ...connections import discovery as provider, tokens
from . import google_accounts
from .common import _LateBinding, _json_response

require_operator = _LateBinding("require_operator")
audit = _LateBinding("audit")
DOCS = "https://docs.googleapis.com/v1/documents/"


def _call(account, document_id, method, body=None):
    connection_id = google_accounts.resolve_account("docs", account)
    from .. import discovery as discovery_api
    connection, error = discovery_api._load(connection_id, None)
    if error:
        raise google_accounts.GoogleAccountError("Docs connection not found", status=404)
    try:
        token, _info = tokens.get_access_token(connection)
    except Exception as exc:
        raise google_accounts.GoogleAccountError(
            f"Docs token unavailable: {type(exc).__name__}", status=502) from None
    url = DOCS + quote(document_id, safe="")
    url += "?includeTabsContent=true" if method == "GET" else ":batchUpdate"
    return provider._request(method, url, token, body)


def route_gdocs(event, method, path):
    match = re.fullmatch(r"/api/agent/gdocs/documents/([A-Za-z0-9_-]+)(/batch-update)?", path)
    if not match or (method, bool(match.group(2))) not in (("GET", False), ("POST", True)):
        return None
    action = "gdocs.get" if method == "GET" else "gdocs.batch_update"
    subject, error = require_operator(event, action)
    if error:
        return error
    query = event.get("queryStringParameters") or {}
    try:
        body = json.loads(event.get("body") or "{}") if method == "POST" else {}
        if not isinstance(body, dict):
            raise ValueError("Expected JSON object")
        account = query.get("account") if method == "GET" else body.get("account")
        payload = None
        if method == "POST":
            requests = body.get("requests")
            revision = body.get("required_revision_id")
            if not isinstance(requests, list) or not requests or not all(isinstance(x, dict) and x for x in requests):
                raise ValueError("requests must be a nonempty array of request objects")
            if not isinstance(revision, str) or not revision.strip():
                raise ValueError("required_revision_id is required; read the document before editing")
            payload = {"requests": requests, "writeControl": {"requiredRevisionId": revision}}
        result = _call(account, match.group(1), method, payload)
    except (ValueError, TypeError) as exc:
        return _json_response(400, {"error": str(exc)})
    except google_accounts.GoogleAccountError as exc:
        return _json_response(exc.status, {"error": str(exc)})
    except provider.DiscoveryError as exc:
        status = re.search(r"HTTP (\d{3})", str(exc))
        return _json_response(int(status.group(1)) if status else 502, {"error": str(exc)})
    audit.emit(match.group(1), action, subject, outcome="ok")
    return _json_response(200, result)
