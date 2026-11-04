"""Implementation of the `dapier gdrive` commands: a thin client over
``/api/agent/gdrive/*``. Files move as base64 inside the JSON bodies (the
API has no binary channel); the connection token stays server-side and
nothing here talks to Google directly.
"""

import base64
import mimetypes
import os
import sys
from urllib.parse import urlencode

from .. import api
from .shared import preview_body, print_json

__all__ = [
    "gdrive_accounts", "gdrive_list", "gdrive_meta", "gdrive_download",
    "gdrive_upload", "gdrive_update", "gdrive_copy", "gdrive_move",
    "gdrive_share", "gdrive_trash", "gdrive_restore", "gdrive_mkdir",
]

BASE = "/api/agent/gdrive"


def _account_params(account, extra=None):
    params = dict(extra or {})
    if account:
        params["account"] = account
    return "?" + urlencode(params) if params else ""


def _read_bytes(path):
    """Local content: a path, or stdin for '-'."""
    if path == "-":
        return sys.stdin.buffer.read()
    with open(path, "rb") as handle:
        return handle.read()


def gdrive_accounts(api_url, as_json=False, debug=False):
    data = api.call(api_url, "GET", BASE + "/accounts", debug=debug)
    accounts = data.get("accounts") or []
    if as_json:
        print_json(data)
        return 0
    if not accounts:
        print("No Google connections serve Drive. Connect one first "
              "(dapier connections create).")
        return 0
    print(f"{'CONNECTION':<24} {'EMAIL':<36} TITLE / REFS")
    for item in accounts:
        refs = ", ".join(item.get("refs") or [])
        title = " / ".join(part for part in (item.get("title"), refs) if part)
        print(f"{str(item.get('connection_id') or ''):<24} "
              f"{str(item.get('email') or '(not signed in)'):<36} {title}")
    return 0


def gdrive_list(api_url, query=None, folder=None, account=None, max_results=25,
                page_token=None, as_json=False, debug=False):
    params = {"max": int(max_results)}
    if query:
        params["q"] = query
    if folder:
        params["folder"] = folder
    if account:
        params["account"] = account
    if page_token:
        params["page_token"] = page_token
    data = api.call(api_url, "GET", BASE + "/files?" + urlencode(params), debug=debug)
    files = data.get("files") or []
    if as_json:
        print_json(data)
        return 0
    print(f"{'ID':<34} {'NAME':<40} MIME / SIZE")
    for item in files:
        detail = item.get("mime_type") or ""
        if item.get("size"):
            detail += f" ({item['size']} bytes)"
        print(f"{str(item.get('id') or ''):<34} "
              f"{str(item.get('name') or '')[:40]:<40} {detail}")
    print(f"({data.get('count') or 0} files)")
    if data.get("next_page_token"):
        print(f"next page: --page-token {data['next_page_token']}")
    return 0


def gdrive_meta(api_url, file_id, account=None, as_json=False, debug=False):
    data = api.call(api_url, "GET",
                    BASE + f"/files/{file_id}" + _account_params(account), debug=debug)
    if as_json:
        print_json(data)
        return 0
    print(f"Name:    {data.get('name')}")
    print(f"ID:      {data.get('id')}")
    print(f"Type:    {data.get('mime_type')}")
    if data.get("size") is not None:
        print(f"Size:    {data.get('size')} bytes")
    print(f"URL:     {data.get('url') or ''}")
    print(f"Updated: {data.get('modified_time') or ''}")
    parents = ", ".join(data.get("parents") or [])
    if parents:
        print(f"Parents: {parents}")
    if data.get("trashed"):
        print("Trashed: yes")
    return 0


def gdrive_download(api_url, file_id, account=None, out=None, export_mime=None,
                    debug=False):
    data = api.call(api_url, "GET", BASE + f"/files/{file_id}/content"
                    + _account_params(account, {"export_mime": export_mime} if export_mime else None),
                    debug=debug)
    data_bytes = base64.b64decode(data.get("data_b64") or "")
    target = out or os.path.basename(data.get("name") or "") or f"drive-{file_id}"
    with open(target, "wb") as handle:
        handle.write(data_bytes)
    print(f"saved {len(data_bytes)} bytes to {target} "
          f"({data.get('mime_type') or 'unknown type'})")
    return 0


def gdrive_upload(api_url, path, name=None, account=None, folder=None, mime=None,
                  as_json=False, preview=False, debug=False):
    filename = name or os.path.basename(path if path != "-" else "stdin-upload")
    content = _read_bytes(path)
    body = {
        "name": filename,
        "content_b64": base64.b64encode(content).decode("ascii"),
        "mime_type": mime or mimetypes.guess_type(filename)[0]
        or "application/octet-stream",
    }
    if account:
        body["account"] = account
    if folder:
        body["folder_id"] = folder
    if preview:
        return preview_body(body)
    data = api.call(api_url, "POST", BASE + "/files", body=body, debug=debug)
    if as_json:
        print_json(data)
        return 0
    print(f"ok: uploaded {data.get('name')} ({data.get('id')})")
    if data.get("url"):
        print(f"    {data.get('url')}")
    return 0


def gdrive_update(api_url, file_id, account=None, name=None, file=None, mime=None,
                  as_json=False, preview=False, debug=False):
    body = {}
    if preview and not (name or file or mime):
        raise api.ApiError("update needs a new name and/or --file content to preview")
    if account:
        body["account"] = account
    if name:
        body["name"] = name
    if mime:
        body["mime_type"] = mime
    if file:
        body["content_b64"] = base64.b64encode(_read_bytes(file)).decode("ascii")
    if preview:
        return preview_body(body)
    data = api.call(api_url, "PATCH", BASE + f"/files/{file_id}"
                    + _account_params(account), body=body, debug=debug)
    if as_json:
        print_json(data)
        return 0
    print(f"ok: updated {data.get('name')} ({data.get('id')})")
    return 0


def gdrive_copy(api_url, file_id, account=None, name=None, as_json=False, preview=False,
                debug=False):
    body = {"account": account} if account else {}
    if name:
        body["name"] = name
    if preview:
        return preview_body(body)
    data = api.call(api_url, "POST", BASE + f"/files/{file_id}/copy"
                    + _account_params(account), body=body, debug=debug)
    if as_json:
        print_json(data)
        return 0
    print(f"ok: copied to {data.get('name')} ({data.get('id')})")
    return 0


def gdrive_move(api_url, file_id, account=None, add_folder=None, remove_folder=None,
                as_json=False, preview=False, debug=False):
    body = {}
    if account:
        body["account"] = account
    if add_folder:
        body["add_parent"] = add_folder
    if remove_folder:
        body["remove_parent"] = remove_folder
    if preview:
        return preview_body(body)
    data = api.call(api_url, "POST", BASE + f"/files/{file_id}/move"
                    + _account_params(account), body=body, debug=debug)
    if as_json:
        print_json(data)
        return 0
    print(f"ok: {data.get('name')} now in: {', '.join(data.get('parents') or [])}")
    return 0


def gdrive_share(api_url, file_id, account=None, role="reader", share_type="user",
                 email=None, as_json=False, preview=False, debug=False):
    body = {"role": role, "share_type": share_type}
    if account:
        body["account"] = account
    if email:
        body["email_address"] = email
    if preview:
        return preview_body(body)
    data = api.call(api_url, "POST", BASE + f"/files/{file_id}/share"
                    + _account_params(account), body=body, debug=debug)
    if as_json:
        print_json(data)
        return 0
    grantee = email or share_type
    print(f"ok: shared {data.get('file_id')} as {data.get('role')} with {grantee} "
          f"(permission {data.get('permission_id')})")
    return 0


def gdrive_trash(api_url, file_id, account=None, permanent=False, as_json=False,
                 preview=False, debug=False):
    body = {"permanent": bool(permanent)}
    if account:
        body["account"] = account
    if preview:
        return preview_body(body)
    data = api.call(api_url, "POST", BASE + f"/files/{file_id}/trash"
                    + _account_params(account), body=body, debug=debug)
    if as_json:
        print_json(data)
        return 0
    if data.get("permanent"):
        print(f"ok: {data.get('file_id')} permanently deleted")
    else:
        print(f"ok: {data.get('file_id')} moved to trash (recoverable)")
    return 0


def gdrive_mkdir(api_url, name, account=None, parent=None, as_json=False, preview=False,
                 debug=False):
    body = {"name": name}
    if account:
        body["account"] = account
    if parent:
        body["parent_id"] = parent
    if preview:
        return preview_body(body)
    data = api.call(api_url, "POST", BASE + "/folders", body=body, debug=debug)
    if as_json:
        print_json(data)
        return 0
    print(f"ok: folder {data.get('name')} created ({data.get('id')})")
    return 0


def gdrive_restore(api_url, file_id, account=None, as_json=False, debug=False):
    body = {"account": account} if account else {}
    data = api.call(api_url, "POST", BASE + f"/files/{file_id}/restore"
                    + _account_params(account), body=body, debug=debug)
    if as_json:
        print_json(data)
        return 0
    state = "still in trash" if data.get("trashed") else "restored from trash"
    print(f"ok: {data.get('name')} ({data.get('file_id')}) {state}")
    return 0
