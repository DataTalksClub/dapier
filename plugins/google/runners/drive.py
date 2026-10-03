"""Drive actions through a Google connection: find a file, upload a file,
read one back, share it, copy it, trash it, create a folder.

``drive_find_file`` searches the connection's Drive ``files`` listing (the
same resource discovery serves) and returns the most recently modified
match; a miss is a ``found: False`` output, not an error.

``drive_upload_file`` writes one file back into the Drive (Drive v3
multipart upload), sourcing the bytes exactly like ``s3_upload`` does —
``source_url``, a staged ``source_s3`` object, or inline ``content`` — so
the find/download/stage/upload steps compose both ways.

``drive_read_file`` is the reverse: it downloads one file and stages the
bytes in the render-artifacts bucket for the steps that follow, exporting
Google-native documents through ``files.export`` first.
"""
import json
import mimetypes
import os
import urllib.parse

from src.dapier.connections import tokens
from src.dapier.engine.actions import base
from src.dapier.engine.actions.templating import render

DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"
DRIVE_UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
FIND_MATCH_MODES = ("contains", "exact")
# The matches listed under ``files`` are bounded so a wide ``contains`` hit
# cannot flood downstream payloads; ``count`` stays the full hit count.
FIND_FILES_CAP = 25
SEARCH_TIMEOUT = 15
DOWNLOAD_TIMEOUT = 30
UPLOAD_TIMEOUT = 60

# Multipart delimiter: the metadata and media parts share it (sent in the
# request's content-type header). Long and prefixed so uploaded bytes never
# contain it by accident.
UPLOAD_BOUNDARY = "dapier-drive-multipart-7f3d9c2"

UPLOAD_FIELDS = "id,name,mimeType,size,webViewLink"


def _drive_query(name, mode, folder=""):
    """The Drive ``q`` filter: not trashed, name matched per mode, and when
    a folder id is given, only that folder's children."""
    escaped = name.replace("'", "\\'")
    operator = "=" if mode == "exact" else "contains"
    query = f"trashed=false and name {operator} '{escaped}'"
    folder = str(folder or "").strip()
    if folder:
        query += f" and '{folder.replace(chr(39), chr(92) + chr(39))}' in parents"
    return query


def _search_files(access_token, query, *, transport=None):
    """One Drive ``files.list`` call, parsed; HTTP errors raise readable."""
    transport = transport or base._default_transport
    url = DRIVE_FILES_URL + "?" + urllib.parse.urlencode({
        "pageSize": "10",
        "orderBy": "modifiedTime desc",
        "fields": "files(id,name,mimeType,modifiedTime)",
        "supportsAllDrives": "true",
        "q": query,
    })
    headers = {"authorization": f"Bearer {access_token}"}
    try:
        status, response = transport("GET", url, headers=headers, body=None,
                                     timeout=SEARCH_TIMEOUT)
    except Exception as exc:
        raise RuntimeError(f"google drive unreachable: {type(exc).__name__}")
    if status >= 300:
        detail = ""
        try:
            error = json.loads(response.decode() or "{}").get("error")
            if isinstance(error, dict) and error.get("message"):
                detail = f" ({str(error['message'])[:200]})"
        except (ValueError, UnicodeDecodeError):
            pass
        raise RuntimeError(f"google drive search returned HTTP {status}{detail}")
    try:
        result = json.loads(response.decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        return {}
    return result if isinstance(result, dict) else {}


def _find_projection(file):
    """One search hit as the step output shows it (the single-match shape)."""
    file = file if isinstance(file, dict) else {}
    return {
        "id": file.get("id"),
        "name": file.get("name"),
        "mimeType": file.get("mimeType"),
        "modified": file.get("modifiedTime"),
    }


def run_drive_find_file(action, event, *, transport=None, steps=None):
    """Find the most recently modified Drive file whose name matches.

    ``match`` selects ``contains`` (default) or ``exact`` against the file
    name; an optional ``folder`` id (the folders discovery serves it)
    restricts the search to that folder's children. The output carries the
    first hit under ``file``, how many candidates the search returned under
    ``count``, every hit under ``files`` (bounded at :data:`FIND_FILES_CAP`,
    each in the single-match shape), and Drive's ``nextPageToken`` as
    ``next_page_token`` when the listing has another page.
    """
    connection = base._connected_connection(action["connection_id"])
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    name = render(str(action.get("name") or ""), event, steps).strip()
    if not name:
        raise ValueError("drive_find_file requires a name")
    mode = str(action.get("match") or "contains").strip().lower()
    if mode not in FIND_MATCH_MODES:
        raise ValueError(f"drive_find_file match must be one of: {', '.join(FIND_MATCH_MODES)}")
    folder = render(str(action.get("folder") or ""), event, steps).strip()
    result = _search_files(access_token, _drive_query(name, mode, folder), transport=transport)
    files = result.get("files") if isinstance(result.get("files"), list) else []
    matches = [_find_projection(file) for file in files]
    next_page_token = str(result.get("nextPageToken") or "") or None
    if not matches:
        return {"found": False, "file": None, "count": 0,
                "files": [], "next_page_token": None}
    return {
        "found": True,
        "file": matches[0],
        "count": len(matches),
        "files": matches[:FIND_FILES_CAP],
        "next_page_token": next_page_token,
    }


# --- drive_upload_file: one file into the Drive, sourced like s3_upload -------


def _upload_content(action, event, steps, *, transport=None):
    """The uploaded bytes: an HTTP download, a staged S3 object, or inline
    text — exactly one, mirroring s3_upload's source composition.

    ``source_s3`` bucket/key take templates, so an earlier step's staged
    file (dropbox_read_file's output, say) uploads without a literal
    bucket/key in the workflow; inline ``content`` takes templates too.
    """
    transport = transport or base._default_transport
    url = render(str(action.get("source_url") or ""), event, steps).strip()
    inline = render(str(action.get("content") or ""), event, steps)
    source = action.get("source_s3") if isinstance(action.get("source_s3"), dict) else {}
    staged = {
        "bucket": render(str(source.get("bucket") or ""), event, steps).strip(),
        "key": render(str(source.get("key") or ""), event, steps).strip(),
    }
    given = [name for name, value in (
        ("source_url", url),
        ("source_s3", staged["bucket"] and staged["key"]),
        ("content", inline),
    ) if value]
    if len(given) > 1:
        raise ValueError("drive_upload_file takes one content source "
                         f"({', '.join(given)} given): source_url, source_s3, or content")
    if url:
        try:
            status, body = transport("GET", url, headers={}, body=None,
                                     timeout=DOWNLOAD_TIMEOUT)
        except Exception as exc:
            raise RuntimeError(f"file download unreachable: {type(exc).__name__}") from None
        if status >= 300:
            raise RuntimeError(f"file download returned HTTP {status}")
        return body
    if staged["bucket"] and staged["key"]:
        return base._s3_body(staged)
    if inline:
        return inline.encode()
    raise ValueError("drive_upload_file needs source_url, source_s3 "
                     "with bucket and key, or content")


def _multipart_body(metadata, content, content_type, boundary):
    """One multipart/related body: the metadata JSON part, then the media."""
    head = (
        f"--{boundary}\r\n"
        "Content-Type: application/json; charset=UTF-8\r\n\r\n"
        f"{json.dumps(metadata)}\r\n"
        f"--{boundary}\r\n"
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode()
    return head + content + f"\r\n--{boundary}--\r\n".encode()


def _response_detail(response):
    """The provider's error message out of a JSON error body (truncated)."""
    try:
        error = json.loads(response.decode() or "{}").get("error")
        if isinstance(error, dict) and error.get("message"):
            return f" ({str(error['message'])[:200]})"
    except (ValueError, UnicodeDecodeError, AttributeError):
        pass
    return ""


def run_drive_upload_file(action, event, *, transport=None, steps=None):
    """Upload one file into the connection's Drive (Drive v3 multipart).

    The bytes come from exactly one content source — ``source_url`` (an
    HTTP download), ``source_s3`` ``{bucket, key}`` (a staged object), or
    inline ``content``. ``name`` is the target filename; the optional
    ``folder_id`` (the folders discovery serves it) places the file, and
    ``content_type`` types the media part (application/octet-stream by
    default). The output carries the created file's facts under stable
    keys: ``file_id``, ``name``, ``mime_type``, ``size``, ``webViewLink``.
    """
    connection = base._connected_connection(action["connection_id"])
    transport = transport or base._default_transport
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    raw_name = render(str(action.get("name") or ""), event, steps).strip()
    if not raw_name:
        raise ValueError("drive_upload_file requires a name")
    content_type = render(str(action.get("content_type") or ""), event, steps).strip() \
        or "application/octet-stream"
    content = _upload_content(action, event, steps, transport=transport)
    metadata = {"name": base._safe_filename(raw_name)}
    folder = render(str(action.get("folder_id") or ""), event, steps).strip()
    if folder:
        metadata["parents"] = [folder]
    url = DRIVE_UPLOAD_URL + "?" + urllib.parse.urlencode({
        "uploadType": "multipart",
        "supportsAllDrives": "true",
        "fields": UPLOAD_FIELDS,
    })
    headers = {
        "authorization": f"Bearer {access_token}",
        "content-type": f"multipart/related; boundary={UPLOAD_BOUNDARY}",
    }
    body = _multipart_body(metadata, content, content_type, UPLOAD_BOUNDARY)
    try:
        status, response = transport("POST", url, headers=headers, body=body,
                                     timeout=UPLOAD_TIMEOUT)
    except Exception as exc:
        raise RuntimeError(f"google drive unreachable: {type(exc).__name__}") from None
    if status >= 300:
        raise RuntimeError(
            f"google drive upload returned HTTP {status}{_response_detail(response)}")
    try:
        result = json.loads(response.decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        result = {}
    file = result if isinstance(result, dict) else {}
    return {
        "file_id": file.get("id"),
        "name": file.get("name"),
        "mime_type": file.get("mimeType"),
        "size": file.get("size"),
        "webViewLink": file.get("webViewLink"),
    }


# --- drive_share_file / drive_copy_file: permission and copy staples ----------


SHARE_ROLES = ("reader", "commenter", "writer")
SHARE_TYPES = ("user", "group", "domain", "anyone")


def _file_request(access_token, file_id, suffix, payload, *, fields, method="POST",
                  transport=None, what="request", extra_params=None):
    """One JSON request under a file's path (``…/files/{id}<suffix>``), parsed;
    HTTP errors raise readable, like every drive action. ``fields`` is the
    endpoint's own field selection — Drive validates it per resource.
    ``extra_params`` adds endpoint query parameters (files.update's
    ``addParents``/``removeParents``); a ``payload`` of ``None`` sends no
    request body (Drive's ``files.delete`` takes none)."""
    transport = transport or base._default_transport
    params = {"supportsAllDrives": "true", "fields": fields}
    params.update(extra_params or {})
    url = (DRIVE_FILES_URL + "/" + urllib.parse.quote(file_id, safe="")
           + suffix + "?" + urllib.parse.urlencode(params))
    headers = {"authorization": f"Bearer {access_token}",
               "content-type": "application/json"}
    body = None if payload is None else json.dumps(payload).encode()
    try:
        status, response = transport(method, url, headers=headers,
                                     body=body,
                                     timeout=SEARCH_TIMEOUT)
    except Exception as exc:
        raise RuntimeError(f"google drive unreachable: {type(exc).__name__}") from None
    if status >= 300:
        raise RuntimeError(
            f"google drive {what} returned HTTP {status}{_response_detail(response)}")
    try:
        result = json.loads(response.decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        return {}
    return result if isinstance(result, dict) else {}


def _file_post(access_token, file_id, suffix, payload, *, fields, transport=None,
               what="request"):
    """The POST form of :func:`_file_request` (share, copy)."""
    return _file_request(access_token, file_id, suffix, payload, fields=fields,
                         method="POST", transport=transport, what=what)


def run_drive_share_file(action, event, *, transport=None, steps=None):
    """Share one Drive file by creating a permission (Drive v3
    permissions.create; Zapier's Share File). ``role`` is
    reader/commenter/writer and ``share_type`` picks user/group/domain/
    anyone — a user or group grant needs the grantee's ``email_address``.
    The output carries the created permission and how the file is now
    shared: ``shared``, ``file_id``, ``permission_id``, ``role``, ``type``.
    """
    connection = base._connected_connection(action["connection_id"])
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    file_id = render(str(action.get("file_id") or ""), event, steps).strip()
    if not file_id:
        raise ValueError("drive_share_file requires a file_id")
    role = render(str(action.get("role") or "reader"), event, steps).strip().lower()
    if role not in SHARE_ROLES:
        raise ValueError(f"drive_share_file role must be one of: {', '.join(SHARE_ROLES)}")
    share_type = render(str(action.get("share_type") or "user"), event, steps).strip().lower()
    if share_type not in SHARE_TYPES:
        raise ValueError(f"drive_share_file share_type must be one of: {', '.join(SHARE_TYPES)}")
    email_address = render(str(action.get("email_address") or ""), event, steps).strip()
    if share_type in ("user", "group") and not email_address:
        raise ValueError(f"drive_share_file sharing with a {share_type} "
                         "needs an email_address")
    payload = {"role": role, "type": share_type}
    if email_address:
        payload["emailAddress"] = email_address
    result = _file_post(access_token, file_id, "/permissions", payload,
                        fields="id,role,type,emailAddress",
                        transport=transport, what="share")
    return {
        "shared": True,
        "file_id": file_id,
        "permission_id": result.get("id"),
        "role": result.get("role") or role,
        "type": result.get("type") or share_type,
    }


def run_drive_copy_file(action, event, *, transport=None, steps=None):
    """Copy one Drive file (Drive v3 files.copy); the optional ``name``
    names the copy, else Drive's "Copy of …". The output carries the created
    file under the upload's keys — ``file_id``, ``name``, ``mime_type``,
    ``size``, ``webViewLink`` — so find/copy/upload steps chain alike.
    """
    connection = base._connected_connection(action["connection_id"])
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    file_id = render(str(action.get("file_id") or ""), event, steps).strip()
    if not file_id:
        raise ValueError("drive_copy_file requires a file_id")
    payload = {}
    name = render(str(action.get("name") or ""), event, steps).strip()
    if name:
        payload["name"] = base._safe_filename(name)
    result = _file_post(access_token, file_id, "/copy", payload,
                        fields=UPLOAD_FIELDS, transport=transport, what="copy")
    return {
        "file_id": result.get("id"),
        "name": result.get("name"),
        "mime_type": result.get("mimeType"),
        "size": result.get("size"),
        "webViewLink": result.get("webViewLink"),
    }


# --- drive_delete_file / drive_move_file / drive_create_folder: staples -------


def _flag(value):
    """Designer boolean fields arrive as "true"/"false" strings."""
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return bool(value)


def run_drive_delete_file(action, event, *, transport=None, steps=None):
    """Delete one Drive file; Zapier's Delete File, which trashes rather
    than destroys.

    Drive v3 has two distinct endpoints and the ``permanent`` flag picks
    between them: the default (``permanent`` false) PATCHes ``files.update``
    with ``trashed: true`` — the file moves to the Drive trash and stays
    recoverable — while ``permanent: true`` calls ``files.delete``, which
    permanently deletes the file without ever trashing it (Drive's trash
    does not do a soft-delete first; the trash move is ``files.update``'s
    job). ``file_id`` renders from the event — chain drive_find_file's
    ``file.id`` or a ``file.deleted`` trigger. The output carries
    ``trashed``, ``permanent``, ``file_id`` and the file's ``name`` (None on
    a permanent delete — the file is gone, Drive answers 204 with no body).
    """
    connection = base._connected_connection(action["connection_id"])
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    file_id = render(str(action.get("file_id") or ""), event, steps).strip()
    if not file_id:
        raise ValueError("drive_delete_file requires a file_id")
    permanent = _flag(action.get("permanent"))
    if permanent:
        _file_request(access_token, file_id, "", None,
                      fields="id,name,trashed", method="DELETE",
                      transport=transport, what="permanent delete")
        return {
            "trashed": False,
            "permanent": True,
            "file_id": file_id,
            "name": None,
        }
    result = _file_request(access_token, file_id, "", {"trashed": True},
                           fields="id,name,trashed", method="PATCH",
                           transport=transport, what="delete")
    return {
        "trashed": True,
        "permanent": False,
        "file_id": file_id,
        "name": result.get("name"),
    }


def run_drive_move_file(action, event, *, transport=None, steps=None):
    """Move one Drive file between folders (Drive v3 ``files.update`` with
    ``addParents``/``removeParents``; Zapier's Move File).

    ``add_parent`` and ``remove_parent`` are folder ids (the folders
    discovery serves both) — at least one is required, since a files.update
    that changes neither parents nor properties is a no-op PATCH. Removing
    nothing but adding keeps the file in its current folders too (a file can
    have several parents); to move it out of its current folder, name that
    folder in ``remove_parent``. The output carries ``moved``, ``file_id``,
    the file's ``name`` and its resulting ``parents``.
    """
    connection = base._connected_connection(action["connection_id"])
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    file_id = render(str(action.get("file_id") or ""), event, steps).strip()
    if not file_id:
        raise ValueError("drive_move_file requires a file_id")
    add_parent = render(str(action.get("add_parent") or ""), event, steps).strip()
    remove_parent = render(str(action.get("remove_parent") or ""), event, steps).strip()
    if not add_parent and not remove_parent:
        raise ValueError("drive_move_file needs add_parent or remove_parent")
    extra_params = {}
    if add_parent:
        extra_params["addParents"] = add_parent
    if remove_parent:
        extra_params["removeParents"] = remove_parent
    result = _file_request(access_token, file_id, "", {}, fields="id,name,parents",
                           method="PATCH", extra_params=extra_params,
                           transport=transport, what="move")
    return {
        "moved": True,
        "file_id": file_id,
        "name": result.get("name"),
        "parents": result.get("parents") or [],
    }


FOLDER_MIME = "application/vnd.google-apps.folder"


def run_drive_create_folder(action, event, *, transport=None, steps=None):
    """Create one Drive folder (Drive v3 ``files.create`` with the folder
    mimeType; Zapier's Create Folder). ``name`` renders from the event and
    the optional ``parent_folder_id`` (the folders discovery serves it)
    places the folder, else it lands at the Drive root. Output:
    ``{folder_id, name, url}`` so follow-up steps can file uploads into it.
    """
    connection = base._connected_connection(action["connection_id"])
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    name = render(str(action.get("name") or ""), event, steps).strip()
    if not name:
        raise ValueError("drive_create_folder requires a name")
    parent = render(str(action.get("parent_folder_id") or ""), event, steps).strip()
    payload = {"name": name, "mimeType": FOLDER_MIME}
    if parent:
        payload["parents"] = [parent]
    transport = transport or base._default_transport
    url = DRIVE_FILES_URL + "?" + urllib.parse.urlencode({
        "supportsAllDrives": "true",
        "fields": "id,name,webViewLink",
    })
    headers = {"authorization": f"Bearer {access_token}",
               "content-type": "application/json"}
    try:
        status, response = transport("POST", url, headers=headers,
                                     body=json.dumps(payload).encode(),
                                     timeout=SEARCH_TIMEOUT)
    except Exception as exc:
        raise RuntimeError(f"google drive unreachable: {type(exc).__name__}") from None
    if status >= 300:
        raise RuntimeError(
            f"google drive create returned HTTP {status}{_response_detail(response)}")
    try:
        result = json.loads(response.decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        result = {}
    result = result if isinstance(result, dict) else {}
    return {
        "folder_id": result.get("id"),
        "name": result.get("name") or name,
        "url": result.get("webViewLink"),
    }


# --- drive_read_file: one file's bytes, staged for the steps that follow ------

# Google-native files have no bytes behind alt=media — files.export converts
# them first. These are the automatic targets; anything else native must name
# ``export_as`` explicitly.
_NATIVE_EXPORT_DEFAULTS = {
    "application/vnd.google-apps.document": "application/pdf",
    "application/vnd.google-apps.presentation": "application/pdf",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.drawing": "image/png",
}


def _file_get(access_token, file_id, suffix, *, params=None, transport=None,
              what="request"):
    """One GET under a file's path (``…/files/{id}<suffix>``); returns the
    raw response body. HTTP errors raise readable, like every drive action —
    the body carries Drive's message and surfaces in the RuntimeError."""
    transport = transport or base._default_transport
    url = (DRIVE_FILES_URL + "/" + urllib.parse.quote(file_id, safe="")
           + suffix)
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"authorization": f"Bearer {access_token}"}
    try:
        status, response = transport("GET", url, headers=headers, body=None,
                                     timeout=DOWNLOAD_TIMEOUT)
    except Exception as exc:
        raise RuntimeError(f"google drive unreachable: {type(exc).__name__}") from None
    if status >= 300:
        raise RuntimeError(
            f"google drive {what} returned HTTP {status}{_response_detail(response)}")
    return response


def run_drive_read_file(action, event, *, transport=None, steps=None, s3_client=None):
    """Download one file and stage it for the steps that follow (Zapier's
    Read File).

    ``file_id`` renders against the event — chain drive_find_file and pass
    ``{steps.<id>.output.file.id}``, or leave it to fall back to the
    triggering file's id for a file.created → read chain. The bytes land in
    the render-artifacts bucket under ``drive/<event id>/<step id>/<name>``
    and the output names the staged ref: ``{filename, size, content_type,
    bucket, key}`` plus the file's identity. Pair with Amazon S3's
    ``source_s3`` or Slack's ``slack_upload_file`` to move the bytes on.

    Google-native files (Docs, Sheets, Slides, Drawings) have no bytes to
    download, so they go through ``files.export`` first: ``export_as`` names
    the target mime, defaulting to PDF for documents and presentations, CSV
    for spreadsheets, PNG for drawings.
    """
    connection = base._connected_connection(action["connection_id"])
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    file_id = render(str(action.get("file_id") or ""), event, steps).strip()
    if not file_id:
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        file_id = str(data.get("file_id") or data.get("id") or "").strip()
    if not file_id:
        raise ValueError("drive_read_file requires a file_id")
    metadata_raw = _file_get(access_token, file_id, "",
                             params={"supportsAllDrives": "true",
                                     "fields": "id,name,mimeType"},
                             transport=transport, what="metadata")
    try:
        metadata = json.loads(metadata_raw.decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        raise RuntimeError("google drive metadata returned an unreadable body") from None
    metadata = metadata if isinstance(metadata, dict) else {}
    name = str(metadata.get("name") or file_id)
    mime = str(metadata.get("mimeType") or "")
    if mime.startswith("application/vnd.google-apps."):
        export_as = render(str(action.get("export_as") or ""), event, steps).strip()
        if not export_as:
            export_as = _NATIVE_EXPORT_DEFAULTS.get(mime, "")
        if not export_as:
            raise ValueError(
                f"drive_read_file: {name} is a Google-native file ({mime}) with "
                "no bytes to download — set export_as to a target mime like "
                "application/pdf")
        body = _file_get(access_token, file_id, "/export",
                         params={"mimeType": export_as, "supportsAllDrives": "true"},
                         transport=transport, what="export")
        content_type = export_as
    else:
        body = _file_get(access_token, file_id, "",
                         params={"alt": "media", "supportsAllDrives": "true"},
                         transport=transport, what="download")
        content_type = mime or mimetypes.guess_type(name)[0] \
            or "application/octet-stream"
    filename = base._safe_filename(name)
    bucket = os.environ["RENDER_ARTIFACTS_BUCKET"]
    key = "drive/{}/{}/{}".format(
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
        "file_id": file_id,
        "name": name,
        "mime_type": mime or None,
    }
