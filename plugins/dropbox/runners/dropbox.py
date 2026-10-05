"""dropbox_upload / dropbox_delete / dropbox_find / dropbox_read_file /
dropbox_get_temp_link / dropbox_create_folder / dropbox_move / dropbox_copy
actions."""
import json
import mimetypes
import os
import urllib.request
import urllib.error

from src.dapier.connections import tokens
from src.dapier.engine.actions import base
from src.dapier.engine.actions.templating import render

DROPBOX_UPLOAD_URL = "https://content.dropboxapi.com/2/files/upload"
DROPBOX_DELETE_URL = "https://api.dropboxapi.com/2/files/delete_v2"
DROPBOX_SEARCH_URL = "https://api.dropboxapi.com/2/files/search"
DROPBOX_CREATE_FOLDER_URL = "https://api.dropboxapi.com/2/files/create_folder_v2"
DROPBOX_MOVE_URL = "https://api.dropboxapi.com/2/files/move_v2"
DROPBOX_COPY_URL = "https://api.dropboxapi.com/2/files/copy_v2"

# The connection lookup and the content download live in core
# connections.providers.dropbox_api (engine.actions.dataops calls them on
# the DataOps forwarding path; core must not import plugin code).
from src.dapier.connections.providers.dropbox_api import (  # noqa: E402
    dropbox_connection as _dropbox_connection,
    dropbox_download as _dropbox_download,
)

def _upload_files(action, data):
    """Return the ``{s3, filename}`` files the action should upload."""
    if action.get("source") == "output":
        output = data.get("output") or {}
        if not (output.get("bucket") and output.get("key")):
            raise ValueError("render output does not reference a stored file")
        name = action.get("filename") or str(output["key"]).split("/")[-1]
        return [{"s3": output, "filename": name}]
    excluded = _excluded_content_types(action)
    files = [
        {"s3": attachment["s3"], "filename": attachment.get("filename") or "attachment"}
        for attachment in data.get("attachments") or []
        if isinstance(attachment.get("s3"), dict)
        and attachment["s3"].get("bucket") and attachment["s3"].get("key")
        and _content_type(attachment) not in excluded
    ]
    if not files:
        raise ValueError("email has no stored attachments to upload")
    return files

def _content_type(attachment):
    return str((attachment or {}).get("content_type") or "").split(";")[0].strip().lower()

def _excluded_content_types(action):
    """Content types the upload skips, from a list or comma-separated string;
    matching is on the MIME type without parameters, case-insensitive."""
    raw = action.get("exclude_content_types") or []
    if isinstance(raw, str):
        raw = raw.split(",")
    return {str(item).split(";")[0].strip().lower() for item in raw if str(item).strip()}

def _conflict_tag(raw):
    """Dropbox error `.tag`, nested as `path/conflict` when present."""
    try:
        error = json.loads(raw.decode() or "{}").get("error")
        if isinstance(error, dict):
            tag = error.get(".tag") or ""
            nested = error.get(tag)
            if isinstance(nested, dict) and nested.get(".tag"):
                return f"{tag}/{nested['.tag']}"
            return tag
    except (ValueError, UnicodeDecodeError):
        pass
    return ""


def _dropbox_upload(access_token, path, payload, *, transport=None,
                    overwrite=False, autorename=True, strict_conflict=False,
                    skip_existing=False):
    transport = transport or base._default_transport
    headers = {
        "authorization": f"Bearer {access_token}",
        "dropbox-api-arg": json.dumps(
            {"path": path, "mode": "overwrite" if overwrite else "add",
             "autorename": autorename, "mute": False,
             **({"strict_conflict": True} if strict_conflict else {})},
            separators=(",", ":"),
        ),
        "content-type": "application/octet-stream",
    }
    try:
        status, raw = transport(
            "POST", DROPBOX_UPLOAD_URL, headers=headers, body=payload, timeout=15,
        )
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    except Exception as exc:
        raise RuntimeError(f"dropbox upload unreachable: {type(exc).__name__}")
    if status >= 300:
        tag = _conflict_tag(raw)
        if skip_existing and status == 409 and tag.startswith("path"):
            return {"path_display": path, "already_exists": True}
        raise RuntimeError(f"dropbox upload returned HTTP {status}{f' ({tag})' if tag else ''}")
    try:
        return json.loads(raw.decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        raise RuntimeError("dropbox upload returned an unreadable body") from None


def _boolean(action, key, default=False):
    value = action.get(key, default)
    if isinstance(value, str):
        if value.strip().lower() not in ("true", "false"):
            raise ValueError(f"{key} must be true or false")
        return value.strip().lower() == "true"
    return bool(value)


def run_dropbox_upload(action, event, transport=None, steps=None):
    connection = _dropbox_connection(action["connection_id"])
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    stripped = render(str(action.get("folder") or ""), event, steps).strip("/")
    folder = f"/{stripped}" if stripped else ""
    uploaded = []
    files = _upload_files(action, event.get("data", {}))
    selection = action.get("attachment_selection", "all")
    if selection not in ("all", "single", "first"):
        raise ValueError("attachment_selection must be all, single or first")
    if selection == "single" and len(files) != 1:
        raise ValueError("single attachment selection requires exactly one stored attachment")
    if selection == "first":
        files = files[:1]
    override = render(str(action.get("filename") or ""), event, steps)
    if "filename" in action and not override.strip():
        raise ValueError("dropbox_upload filename rendered empty")
    if override and len(files) > 1:
        raise ValueError("filename override requires selecting a single file")
    skip_existing = _boolean(action, "skip_existing")
    existed = []
    for file in files:
        path = f"{folder}/{base._safe_filename(override or file['filename'])}"
        metadata = _dropbox_upload(access_token, path, base._s3_body(file["s3"]), transport=transport,
                                   overwrite=_boolean(action, "overwrite"),
                                   autorename=_boolean(action, "autorename", True),
                                   strict_conflict=_boolean(action, "strict_conflict"),
                                   skip_existing=skip_existing)
        uploaded.append(metadata.get("path_display") or metadata.get("path_lower") or path)
        if metadata.get("already_exists"):
            existed.append(uploaded[-1])
    return {"uploaded": uploaded, "already_exists": bool(existed) and len(existed) == len(uploaded)}

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
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
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


def run_dropbox_read_file(action, event, *, transport=None, steps=None, s3_client=None):
    """Download one file and stage it for the steps that follow (Zapier's
    Read File).

    ``path`` renders against the event and falls back to the triggering
    file's path, so a dropbox file.created → read_file chain needs nothing
    but the connection. The bytes land in the render-artifacts bucket under
    ``dropbox/<event id>/<step id>/<filename>`` and the output names the
    staged ref: ``{filename, size, content_type, bucket, key, path}``. Pair
    with Amazon S3's ``source_s3`` — its bucket/key take templates — to hand
    the bytes to another bucket.
    """
    connection = _dropbox_connection(action["connection_id"])
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    path = _rendered_field(action, "path", event, steps)
    if not path:
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        path = str(data.get("path") or "").strip()
    if not path:
        raise ValueError("dropbox_read_file requires a file path")
    body = _dropbox_download(access_token, path, transport=transport)
    filename = base._safe_filename(path.split("/")[-1])
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    bucket = os.environ["RENDER_ARTIFACTS_BUCKET"]
    key = "dropbox/{}/{}/{}".format(
        str(event.get("id") or "unknown").replace("/", "_"),
        str(action.get("id") or "read").replace("/", "_"),
        filename,
    )
    client = s3_client
    if client is None:
        import boto3

        client = boto3.client("s3")
    client.put_object(
        Bucket=bucket, Key=key, Body=body, ContentType=content_type,
        ServerSideEncryption="AES256",
    )
    return {
        "filename": filename,
        "size": len(body),
        "content_type": content_type,
        "bucket": bucket,
        "key": key,
        "path": path,
    }


# A read that hands over where the bytes live instead of moving them — the
# role Google Drive's webContentLink plays for s3_upload's source_url.
# get_temporary_link is an RPC route (JSON body on the api host) even though
# the link it returns points at the content host.
DROPBOX_TEMP_LINK_URL = "https://api.dropboxapi.com/2/files/get_temporary_link"


def _dropbox_temp_link(access_token, path, *, transport=None):
    raw = _dropbox_rpc(
        DROPBOX_TEMP_LINK_URL, access_token, {"path": path},
        transport=transport, unreachable="dropbox temp-link unreachable",
    )
    try:
        return json.loads(raw.decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        raise RuntimeError("dropbox temp-link returned an unreadable body") from None


def run_dropbox_get_temp_link(action, event, *, transport=None, steps=None):
    """Get a short-lived direct-download link for one file
    (files/get_temporary_link).

    ``path`` renders against the event and falls back to the triggering
    file's path, so a dropbox file.created → temp-link chain needs nothing
    but the connection. The output is ``{link, item}`` — ``item`` is the
    found-file shape (id/name/path/tag/size/modified) — and ``link`` feeds
    straight into s3_upload's ``source_url`` when a chain wants the bytes
    without staging them first. Dropbox links expire after about four hours.
    """
    connection = _dropbox_connection(action["connection_id"])
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    path = _rendered_field(action, "path", event, steps)
    if not path:
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        path = str(data.get("path") or "").strip()
    if not path:
        raise ValueError("dropbox_get_temp_link requires a file path")
    response = _dropbox_temp_link(access_token, path, transport=transport)
    link = str(response.get("link") or "")
    if not link:
        raise RuntimeError("dropbox temp-link returned no link")
    metadata = response.get("metadata") if isinstance(response.get("metadata"), dict) else {}
    return {
        "link": link,
        "item": _found_item(metadata) if metadata.get("id") else None,
    }


def run_dropbox_create_folder(action, event, *, transport=None, steps=None):
    """Create one folder (files/create_folder_v2, Zapier's Create Folder).

    ``path`` renders against the event and must be the full path including
    the folder name (``/Invoices/{month}``). An existing folder is a
    ``path/conflict/folder_conflict`` error, not a result — chain
    dropbox_find with ``create_if_missing`` when find-or-create is wanted.
    Output: ``{folder: path, item: {id, name, path, tag}}``.
    """
    connection = _dropbox_connection(action["connection_id"])
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    path = _rendered_field(action, "path", event, steps)
    if not path:
        raise ValueError("dropbox_create_folder requires a folder path")
    raw = _dropbox_rpc(
        DROPBOX_CREATE_FOLDER_URL, access_token, {"path": path},
        transport=transport, unreachable="dropbox create-folder unreachable",
    )
    try:
        data = json.loads(raw.decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        raise RuntimeError("dropbox create-folder returned an unreadable body") from None
    metadata = data.get("metadata") or {}
    return {
        "folder": path,
        "item": _found_item(metadata) if metadata.get("id") else None,
    }


def _run_dropbox_transfer(action, event, *, url, verb, transport, steps):
    """One files/move_v2 or files/copy_v2 call; the two share every shape.

    Both paths render against the event. ``to_path`` includes the new name —
    a move within the same folder under a different name is a rename. With
    ``autorename`` Dropbox appends a suffix instead of erroring when the
    destination exists. Output: ``{moved|copied: to_path, item}``.
    """
    connection = _dropbox_connection(action["connection_id"])
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    from_path = _rendered_field(action, "from_path", event, steps)
    to_path = _rendered_field(action, "to_path", event, steps)
    if not from_path or not to_path:
        raise ValueError(f"dropbox_{verb} requires from_path and to_path")
    raw = _dropbox_rpc(
        url, access_token,
        {"from_path": from_path, "to_path": to_path,
         "autorename": _boolean(action, "autorename")},
        transport=transport, unreachable=f"dropbox {verb} unreachable",
    )
    try:
        data = json.loads(raw.decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        raise RuntimeError(f"dropbox {verb} returned an unreadable body") from None
    metadata = data.get("metadata") or {}
    return {
        verb: to_path,
        "item": _found_item(metadata) if metadata.get("id") else None,
    }


def run_dropbox_move(action, event, *, transport=None, steps=None):
    """Move (or rename) one file or folder (files/move_v2, Zapier's Move
    File / Rename File). See :func:`_run_dropbox_transfer` for the shared
    semantics; output is ``{moved: to_path, item}``."""
    return _run_dropbox_transfer(action, event, url=DROPBOX_MOVE_URL, verb="moved",
                                 transport=transport, steps=steps)


def run_dropbox_copy(action, event, *, transport=None, steps=None):
    """Copy one file or folder to a new path (files/copy_v2, Zapier's Copy
    File). See :func:`_run_dropbox_transfer` for the shared semantics;
    output is ``{copied: to_path, item}``."""
    return _run_dropbox_transfer(action, event, url=DROPBOX_COPY_URL, verb="copied",
                                 transport=transport, steps=steps)
