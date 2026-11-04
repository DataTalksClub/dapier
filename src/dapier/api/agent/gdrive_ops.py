"""Google Drive operations behind the ``/api/agent/gdrive/*`` routes (the
`dapier gdrive` CLI's backend; the connection token stays server-side).

Same shape as ``gmail_ops``: the account is one Google connection picked by
``google_accounts.resolve_account``, every provider call rides the shared
``_request`` transport seam, failures surface as ``GDriveOpsError`` with the
provider's redacted detail, and the connection's granted scopes are honored
as-is — a read-only Drive grant fails on writes with Drive's own 403,
relayed with the hint to widen the grant.

Downloads and uploads move bytes as base64 inside the route's JSON body:
the agent API is JSON end to end (the CLI's thin client has no binary
channel), so ``download``/``export`` return ``data_b64`` the CLI decodes to
a file, and ``upload``/``update`` accept ``content_b64`` and re-multipart it
to Drive. Shared drives ride ``supportsAllDrives`` everywhere.
"""

import base64
import json
import re
from urllib.parse import quote, urlencode

from ...connections import discovery as provider
from ...connections import tokens
from . import google_accounts

DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"
DRIVE_UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 200

FILE_FIELDS = ("id,name,mimeType,size,createdTime,modifiedTime,webViewLink,"
               "iconLink,trashed,parents,owners(displayName,emailAddress),"
               "capabilities(canEdit,canShare),fileExtension,fullFileExtension")

# Google-native mime → default export target (the download/export fallbacks).
EXPORT_DEFAULTS = {
    "application/vnd.google-apps.document": "application/pdf",
    "application/vnd.google-apps.presentation": "application/pdf",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.drawing": "image/png",
}
FOLDER_MIME = "application/vnd.google-apps.folder"


class GDriveOpsError(Exception):
    """Operator-facing Drive failure; message must never carry a token."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _status_from(detail):
    match = re.search(r"HTTP (\d{3})", str(detail))
    return int(match.group(1)) if match else 502


def _scope_hint(status, detail):
    if status == 403 and ("insufficient" in detail.lower() or "the granted scopes" in detail.lower()):
        return (" — the connection lacks the Drive scope for this operation; "
                "update its scopes (dapier connections edit --scopes) and reconnect")
    return ""


def _connection_and_token(account):
    """The resolved Google connection and a live bearer token for it."""
    try:
        connection_id = google_accounts.resolve_account("drive", account)
    except google_accounts.GoogleAccountError as exc:
        raise GDriveOpsError(str(exc), status=exc.status) from None
    from .. import discovery as discovery_api

    connection, error = discovery_api._load(connection_id, None)
    if error:
        raise GDriveOpsError("Drive connection not found", status=404)
    try:
        token, _info = tokens.get_access_token(connection)
    except Exception as exc:
        raise GDriveOpsError(
            f"Drive token unavailable for {connection_id}: {type(exc).__name__}",
            status=502) from None
    return connection_id, connection, token


def _call(token, method, url, payload=None):
    """One Drive REST call (JSON); DiscoveryError relays provider detail."""
    try:
        return provider._request(method, url, token, payload)
    except provider.DiscoveryError as exc:
        status = _status_from(str(exc))
        raise GDriveOpsError(
            f"Drive {method} failed: {exc}{_scope_hint(status, str(exc))}",
            status=status) from None


def _b64_decode(text):
    text = str(text or "")
    try:
        return base64.b64decode(text)
    except (ValueError, TypeError):
        raise GDriveOpsError("content is not valid base64") from None


def _b64_encode(data):
    return base64.b64encode(data).decode("ascii")


def _file_view(item):
    if not isinstance(item, dict):
        return {}
    owners = [owner for owner in item.get("owners") or [] if isinstance(owner, dict)]
    return {
        "id": item.get("id"),
        "name": item.get("name"),
        "mime_type": item.get("mimeType"),
        "size": int(item["size"]) if str(item.get("size") or "").isdigit() else None,
        "created_time": item.get("createdTime"),
        "modified_time": item.get("modifiedTime"),
        "url": item.get("webViewLink"),
        "trashed": bool(item.get("trashed")),
        "parents": item.get("parents") or [],
        "owners": [owner.get("emailAddress") or owner.get("displayName")
                   for owner in owners],
        "can_edit": bool((item.get("capabilities") or {}).get("canEdit")),
        "can_share": bool((item.get("capabilities") or {}).get("canShare")),
    }


def _multipart_body(metadata, content, boundary):
    """A Drive multipart/related body: metadata JSON part + the raw bytes."""
    head = (
        f"--{boundary}\r\n"
        "content-type: application/json; charset=UTF-8\r\n\r\n"
        + json.dumps(metadata) + "\r\n"
        f"--{boundary}\r\n"
        "content-type: "
        + (metadata.get("mimeType") or "application/octet-stream") + "\r\n\r\n"
    ).encode("utf-8")
    return head + content + f"\r\n--{boundary}--\r\n".encode("utf-8")


def _transport(method, url, token, body=None, *, timeout=15, headers=None):
    """One raw transport call: (status, bytes). urllib raises HTTPError for
    non-2xx; recovered to its status/body so provider errors keep their
    detail (the fake transports tests inject already return statuses)."""
    call_headers = {"authorization": f"Bearer {token}"}
    call_headers.update(headers or {})
    try:
        return provider._default_transport(method, url, headers=call_headers,
                                           body=body, timeout=timeout)
    except Exception as exc:
        status = getattr(exc, "code", None)
        if not isinstance(status, int):
            raise GDriveOpsError(
                f"Drive request unreachable: {type(exc).__name__}", status=502) from None
        raw = exc.read() if hasattr(exc, "read") else b""
        return status, raw


def _upload_call(token, method, url, metadata, content=None):
    """One multipart upload (uploadType=multipart) through the transport."""
    boundary = "dapier-drive-multipart"
    headers = {
        "content-type": f"multipart/related; boundary={boundary}",
    }
    if content is None:
        body = json.dumps(metadata).encode("utf-8")
        headers["content-type"] = "application/json"
    else:
        body = _multipart_body(metadata, content, boundary)
    status, raw = _transport(method, url, token, body, timeout=30, headers=headers)
    if status >= 300:
        detail = provider._error_detail(status, raw)
        raise GDriveOpsError(
            f"Drive upload failed: HTTP {status}{': ' + detail if detail else ''}",
            status=status)
    try:
        return json.loads(raw.decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        return {}


# --- reads ---------------------------------------------------------------------


def accounts(account=None):
    found = google_accounts.list_accounts("drive")
    return {"accounts": found, "count": len(found)}


def list_files(account, query=None, folder_id=None, max_results=DEFAULT_PAGE_SIZE,
               page_token=None):
    """Search/list files (trashed=false unless the query says otherwise)."""
    _connection_id, _connection, token = _connection_and_token(account)
    clauses = []
    if folder_id:
        clauses.append(f"'{folder_id}' in parents")
    if query:
        clauses.append(f"({query})")
    params = {
        "pageSize": max(1, min(int(max_results or DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE)),
        "orderBy": "modifiedTime desc",
        "fields": f"nextPageToken,files({FILE_FIELDS})",
        "supportsAllDrives": "true",
        "includeItemsFromAllDrives": "true",
    }
    if clauses:
        params["q"] = " and ".join(clauses)
    if page_token:
        params["pageToken"] = str(page_token)
    data = _call(token, "GET", DRIVE_FILES_URL + "?" + urlencode(params))
    files = [_file_view(item) for item in data.get("files") or []
             if isinstance(item, dict) and item.get("id")]
    return {"files": files, "count": len(files),
            "next_page_token": data.get("nextPageToken"), "account": _connection_id}


def get_file(account, file_id):
    _connection_id, _connection, token = _connection_and_token(account)
    params = {"fields": FILE_FIELDS, "supportsAllDrives": "true"}
    data = _call(token, "GET",
                 f"{DRIVE_FILES_URL}/{quote(str(file_id), safe='')}?" + urlencode(params))
    return _file_view(data)


def download(account, file_id, export_mime=None):
    """The file's bytes: Google-native files export (default mime per type),
    everything else downloads raw — as base64 for the JSON route."""
    _connection_id, _connection, token = _connection_and_token(account)
    quoted = quote(str(file_id), safe="")
    meta = get_file(account, file_id)
    mime = meta.get("mime_type")
    if mime == FOLDER_MIME:
        raise GDriveOpsError("that id is a folder; folders have no content to download",
                             status=400)
    if mime in EXPORT_DEFAULTS or export_mime:
        target = export_mime or EXPORT_DEFAULTS.get(mime)
        url = f"{DRIVE_FILES_URL}/{quoted}/export?" + urlencode({"mimeType": target})
    else:
        url = f"{DRIVE_FILES_URL}/{quoted}?alt=media&supportsAllDrives=true"
    status, raw = _transport("GET", url, token, timeout=60)
    if status >= 300:
        detail = provider._error_detail(status, raw)
        raise GDriveOpsError(
            f"Drive download failed: HTTP {status}{': ' + detail if detail else ''}",
            status=status)
    return {
        "file_id": meta.get("id"),
        "name": meta.get("name"),
        "mime_type": export_mime or EXPORT_DEFAULTS.get(mime) or mime,
        "exported": bool(export_mime or mime in EXPORT_DEFAULTS),
        "size": len(raw),
        "data_b64": _b64_encode(raw),
    }


# --- mutations ------------------------------------------------------------------


def upload(account, name, content_b64, mime_type=None, folder_id=None):
    """One multipart files.create: metadata plus the decoded content."""
    _connection_id, _connection, token = _connection_and_token(account)
    content = _b64_decode(content_b64)
    metadata = {"name": str(name).strip(), "mimeType": mime_type or "application/octet-stream"}
    if not metadata["name"]:
        raise GDriveOpsError("a file name is required")
    if folder_id:
        metadata["parents"] = [folder_id]
    url = DRIVE_UPLOAD_URL + "?" + urlencode({"uploadType": "multipart",
                                              "supportsAllDrives": "true",
                                              "fields": FILE_FIELDS})
    data = _upload_call(token, "POST", url, metadata, content)
    return _file_view(data)


def update(account, file_id, name=None, content_b64=None, mime_type=None):
    """PATCH one file: metadata and/or content (a media PATCH replaces it)."""
    _connection_id, _connection, token = _connection_and_token(account)
    metadata = {}
    if name is not None and str(name).strip():
        metadata["name"] = str(name).strip()
    if mime_type:
        metadata["mimeType"] = mime_type
    content = _b64_decode(content_b64) if content_b64 else None
    if content is None and not metadata:
        raise GDriveOpsError("update needs a new name and/or content")
    quoted = quote(str(file_id), safe="")
    url = (f"{DRIVE_UPLOAD_URL}/{quoted}?" + urlencode({
        "uploadType": "multipart", "supportsAllDrives": "true",
        "fields": FILE_FIELDS}))
    data = _upload_call(token, "PATCH", url, metadata, content)
    return _file_view(data)


def copy(account, file_id, name=None):
    _connection_id, _connection, token = _connection_and_token(account)
    metadata = {"name": name} if name else {}
    url = (f"{DRIVE_FILES_URL}/{quote(str(file_id), safe='')}/copy?" + urlencode({
        "supportsAllDrives": "true", "fields": FILE_FIELDS}))
    return _file_view(_call(token, "POST", url, metadata))


def move(account, file_id, add_parent=None, remove_parent=None):
    """Move between folders (files.update with addParents/removeParents)."""
    _connection_id, _connection, token = _connection_and_token(account)
    if not add_parent and not remove_parent:
        raise GDriveOpsError("move needs --add-folder and/or --remove-folder")
    params = {"supportsAllDrives": "true", "fields": FILE_FIELDS}
    if add_parent:
        params["addParents"] = add_parent
    if remove_parent:
        params["removeParents"] = remove_parent
    url = f"{DRIVE_FILES_URL}/{quote(str(file_id), safe='')}?" + urlencode(params)
    return _file_view(_call(token, "PATCH", url, {}))


def share(account, file_id, role="reader", share_type="user", email_address=None):
    """Create one permission (permissions.create) — the share verb."""
    _connection_id, _connection, token = _connection_and_token(account)
    if share_type in ("user", "group") and not email_address:
        raise GDriveOpsError(f"share type {share_type} needs --email")
    if role not in ("reader", "commenter", "writer", "organizer", "owner"):
        raise GDriveOpsError(f"role must be reader/commenter/writer, not {role!r}")
    url = (f"{DRIVE_FILES_URL}/{quote(str(file_id), safe='')}/permissions?" + urlencode({
        "supportsAllDrives": "true"}))
    permission = {"role": role, "type": share_type}
    if email_address:
        permission["emailAddress"] = email_address
    data = _call(token, "POST", url, permission)
    return {"file_id": file_id, "permission_id": data.get("id"),
            "role": data.get("role"), "type": data.get("type"),
            "email_address": email_address}


def trash(account, file_id, permanent=False):
    """Trash (recoverable) by default; --permanent destroys the file."""
    _connection_id, _connection, token = _connection_and_token(account)
    quoted = quote(str(file_id), safe='')
    if permanent:
        url = f"{DRIVE_FILES_URL}/{quoted}?supportsAllDrives=true"
        status, raw = _transport("DELETE", url, token)
        if status >= 300:
            detail = provider._error_detail(status, raw)
            raise GDriveOpsError(f"Drive delete failed: HTTP {status}{': ' + detail if detail else ''}",
                                 status=status)
        return {"file_id": file_id, "permanent": True}
    url = f"{DRIVE_FILES_URL}/{quoted}?supportsAllDrives=true&fields={FILE_FIELDS}"
    data = _call(token, "PATCH", url, {"trashed": True})
    return {"file_id": data.get("id"), "name": data.get("name"),
            "trashed": bool(data.get("trashed")), "permanent": False}


def restore(account, file_id):
    """Un-trash a file (files.update with trashed: false)."""
    _connection_id, _connection, token = _connection_and_token(account)
    url = (f"{DRIVE_FILES_URL}/{quote(str(file_id), safe='')}"
           f"?supportsAllDrives=true&fields={FILE_FIELDS}")
    data = _call(token, "PATCH", url, {"trashed": False})
    return {"file_id": data.get("id"), "name": data.get("name"),
            "trashed": bool(data.get("trashed"))}


def create_folder(account, name, parent_id=None):
    _connection_id, _connection, token = _connection_and_token(account)
    name = str(name or "").strip()
    if not name:
        raise GDriveOpsError("a folder name is required")
    metadata = {"name": name, "mimeType": FOLDER_MIME}
    if parent_id:
        metadata["parents"] = [parent_id]
    url = DRIVE_FILES_URL + "?" + urlencode({"supportsAllDrives": "true",
                                             "fields": FILE_FIELDS})
    return _file_view(_call(token, "POST", url, metadata))
