"""dropbox_upload / dropbox_delete actions."""
import json
import os
import urllib.request

from ...connections import tokens
from . import base

DROPBOX_UPLOAD_URL = "https://content.dropboxapi.com/2/files/upload"
DROPBOX_DOWNLOAD_URL = "https://content.dropboxapi.com/2/files/download"
DROPBOX_DELETE_URL = "https://api.dropboxapi.com/2/files/delete_v2"


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
    for file in _upload_files(action, event.get("data", {})):
        path = f"{folder}/{base._safe_filename(file['filename'])}"
        _dropbox_upload(access_token, path, base._s3_body(file["s3"]), transport=transport)

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
