"""dropbox_upload / dropbox_delete / dropbox_find actions."""
import json
import os
import urllib.request

from ...connections import tokens
from . import base
from .templating import render

DROPBOX_UPLOAD_URL = "https://content.dropboxapi.com/2/files/upload"
DROPBOX_DOWNLOAD_URL = "https://content.dropboxapi.com/2/files/download"
DROPBOX_DELETE_URL = "https://api.dropboxapi.com/2/files/delete_v2"
DROPBOX_SEARCH_URL = "https://api.dropboxapi.com/2/files/search"
DROPBOX_CREATE_FOLDER_URL = "https://api.dropboxapi.com/2/files/create_folder_v2"


def _dropbox_connection(connection_id):
    return base._connected_connection(connection_id)

def _upload_files(action, data):
    """Return the ``{s3, filename}`` files the action should upload."""
    if action.get("source") == "output":
        output = data.get("output") or {}
        if not (output.get("bucket") and output.get("key")):
            raise ValueError("render output does not reference a stored file")
        name = action.get("filename") or str(output["key"]).split("/")[-1]
        return [{"s3": output, "filename": name}]
    files = [
        {"s3": attachment["s3"], "filename": attachment.get("filename") or "attachment"}
        for attachment in data.get("attachments") or []
        if isinstance(attachment.get("s3"), dict)
        and attachment["s3"].get("bucket") and attachment["s3"].get("key")
    ]
    if not files:
        raise ValueError("email has no stored attachments to upload")
    return files

def _dropbox_upload(access_token, path, payload, *, transport=None):
    transport = transport or base._default_transport
    headers = {
        "authorization": f"Bearer {access_token}",
        "dropbox-api-arg": json.dumps(
            {"path": path, "mode": "add", "autorename": True, "mute": False},
            separators=(",", ":"),
        ),
        "content-type": "application/octet-stream",
    }
    try:
        status, raw = transport(
            "POST", DROPBOX_UPLOAD_URL, headers=headers, body=payload, timeout=15,
        )
    except Exception as exc:
        raise RuntimeError(f"dropbox upload unreachable: {type(exc).__name__}")
    if status >= 300:
        tag = ""
        try:
            error = json.loads(raw.decode() or "{}").get("error")
            if isinstance(error, dict):
                tag = error.get(".tag") or ""
                nested = error.get(tag)
                if isinstance(nested, dict) and nested.get(".tag"):
                    tag = f"{tag}/{nested['.tag']}"
        except (ValueError, UnicodeDecodeError):
            pass
        raise RuntimeError(f"dropbox upload returned HTTP {status}{f' ({tag})' if tag else ''}")

def run_dropbox_upload(action, event, transport=None):
    connection = _dropbox_connection(action["connection_id"])
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    stripped = str(action.get("folder") or "").strip("/")
    folder = f"/{stripped}" if stripped else ""
    uploaded = []
    for file in _upload_files(action, event.get("data", {})):
        path = f"{folder}/{base._safe_filename(file['filename'])}"
        _dropbox_upload(access_token, path, base._s3_body(file["s3"]), transport=transport)
        uploaded.append(path)
    return {"uploaded": uploaded}

def _dropbox_rpc(url, access_token, payload, *, transport=None, unreachable="dropbox call unreachable"):
    transport = transport or base._default_transport
    headers = {
        "authorization": f"Bearer {access_token}",
        "content-type": "application/json",
    }
    try:
        status, raw = transport(
            "POST", url, headers=headers, body=json.dumps(payload).encode(), timeout=15,
        )
    except Exception as exc:
        raise RuntimeError(f"{unreachable}: {type(exc).__name__}")
    if status >= 300:
        tag = ""
        try:
            error = json.loads(raw.decode() or "{}").get("error")
            if isinstance(error, dict):
                tag = error.get(".tag") or ""
                nested = error.get(tag)
                if isinstance(nested, dict) and nested.get(".tag"):
                    tag = f"{tag}/{nested['.tag']}"
        except (ValueError, UnicodeDecodeError):
            pass
        raise RuntimeError(f"dropbox call returned HTTP {status}{f' ({tag})' if tag else ''}")
    return raw

def _dropbox_download(access_token, path, *, transport=None):
    transport = transport or base._default_transport
    headers = {
        "authorization": f"Bearer {access_token}",
        "dropbox-api-arg": json.dumps({"path": path}, separators=(",", ":")),
    }
    try:
        status, raw = transport(
            "POST", DROPBOX_DOWNLOAD_URL, headers=headers, body=b"", timeout=15,
        )
    except Exception as exc:
        raise RuntimeError(f"dropbox download unreachable: {type(exc).__name__}")
    if status >= 300:
        raise RuntimeError(f"dropbox download returned HTTP {status}")
    return raw

def run_dropbox_delete(action, event, transport=None):
    """Delete the processed file from Dropbox; run only after intake succeeds."""
    connection = _dropbox_connection(action["connection_id"])
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    path = action.get("path") or event.get("data", {}).get("path")
    if not path:
        raise ValueError("dropbox_delete requires a file path")
    _dropbox_rpc(
        DROPBOX_DELETE_URL, access_token, {"path": path},
        transport=transport, unreachable="dropbox delete unreachable",
    )
    return {"deleted": path}


# A find miss, same shape family as sheets_find_row's _NOT_FOUND.
_NOT_FOUND = {"found": False, "created": False, "item": None}


def run_dropbox_find(action, event, transport=None, steps=None):
    """Find a file or folder by name under a folder (Zapier's Find File /
    Find Folder).

    ``query`` is rendered against the event and searched with Dropbox's
    files/search under ``path`` (the connection's root when unset),
    filename matches only, active files; the first match of the requested
    ``kind`` wins. A miss is a ``found: False`` result, not an error —
    branch on ``{steps.<id>.output.found}`` with a condition step. With
    ``create_if_missing`` a folder find creates the named folder instead
    of returning empty-handed and reports ``created: True``.
    """
    connection = _dropbox_connection(action["connection_id"])
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    query = _rendered_field(action, "query", event, steps)
    if not query:
        raise ValueError("dropbox_find requires a query")
    kind = str(action.get("kind") or "any").strip().lower() or "any"
    if kind not in ("any", "file", "folder"):
        raise ValueError("dropbox_find kind must be one of: any, file, folder")
    path = _rendered_field(action, "path", event, steps)
    if not path:
        path = str(connection.get("root_path") or "")

    raw = _dropbox_rpc(
        DROPBOX_SEARCH_URL, access_token,
        {"query": query,
         "options": {"path": path, "max_results": 50,
                     "file_status": "active", "filename_only": True}},
        transport=transport, unreachable="dropbox search unreachable",
    )
    try:
        data = json.loads(raw.decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        raise RuntimeError("dropbox search returned an unreadable body")
    for match in data.get("matches") or []:
        metadata = (match or {}).get("metadata") or {}
        if not metadata.get("id"):
            continue
        tag = metadata.get(".tag") or ""
        if kind in ("file", "folder") and tag != kind:
            continue
        return {"found": True, "created": False, "item": _found_item(metadata)}

    if not action.get("create_if_missing"):
        return dict(_NOT_FOUND)
    if kind != "folder":
        raise ValueError("dropbox_find create_if_missing applies only to folder finds")
    raw = _dropbox_rpc(
        DROPBOX_CREATE_FOLDER_URL, access_token,
        {"path": _join_path(path, query)},
        transport=transport, unreachable="dropbox create-folder unreachable",
    )
    try:
        created = json.loads(raw.decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        raise RuntimeError("dropbox create-folder returned an unreadable body")
    metadata = created.get("metadata") or {}
    return {
        "found": False,
        "created": True,
        "item": _found_item(metadata) if metadata.get("id") else None,
    }


def _rendered_field(action, key, event, steps):
    value = action.get(key)
    if isinstance(value, str):
        value = render(value, event, steps)
    return str(value or "").strip()


def _join_path(root, name):
    folder = str(root or "").rstrip("/")
    return f"{folder}/{name}" if folder else f"/{name}"


def _found_item(metadata):
    """The step-output shape of one found entry (id/name/path + file facts)."""
    name = str(metadata.get("name") or metadata.get("path_display") or metadata["id"])
    item = {
        "id": str(metadata["id"]),
        "name": name,
        "path": str(metadata.get("path_display") or metadata.get("path_lower") or name),
        "tag": metadata.get(".tag"),
    }
    if metadata.get(".tag") == "file":
        item["size"] = metadata.get("size")
        item["modified"] = str(metadata.get("server_modified") or "")
    return item
