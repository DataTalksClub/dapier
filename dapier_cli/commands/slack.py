"""Implementation of the `dapier slack` commands."""

import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from .. import api
from .shared import print_json

__all__ = [
    "slack_find_user", "slack_open_dm", "slack_history", "slack_dm_export",
    "slack_replies", "slack_channels", "slack_search", "slack_post",
    "slack_attach_image",
]

DEFAULT_CONNECTION_ID = "slack-alexey"
DEFAULT_POST_CONNECTION_ID = "slack"


def slack_find_user(api_url, query, connection_id=DEFAULT_CONNECTION_ID, limit=50, debug=False):
    path = "/api/agent/slack/users?" + urlencode({
        "q": query,
        "connection_id": connection_id,
        "limit": int(limit),
    })
    data = api.call(api_url, "GET", path, debug=debug)
    items = data.get("items") or []
    if not items:
        print("No matching users.")
        return 0
    print(f"{'ID':<14} {'NAME':<28} {'HANDLE':<20} EMAIL")
    for item in items:
        print(
            f"{str(item.get('id') or ''):<14} "
            f"{str(item.get('name') or '')[:28]:<28} "
            f"{str(item.get('handle') or '')[:20]:<20} "
            f"{item.get('email') or ''}"
        )
    return 0


def slack_open_dm(api_url, user, connection_id=DEFAULT_CONNECTION_ID, debug=False):
    data = api.call(
        api_url, "POST", "/api/agent/slack/dm",
        body={"user": user, "connection_id": connection_id}, debug=debug)
    print_json(data)
    return 0


def slack_history(api_url, channel, connection_id=DEFAULT_CONNECTION_ID, limit=100,
                  cursor=None, oldest=None, latest=None, debug=False):
    params = {
        "channel": channel,
        "connection_id": connection_id,
        "limit": int(limit),
    }
    if cursor:
        params["cursor"] = cursor
    if oldest:
        params["oldest"] = oldest
    if latest:
        params["latest"] = latest
    data = api.call(api_url, "GET", "/api/agent/slack/history?" + urlencode(params), debug=debug)
    print_json(data)
    return 0


def slack_dm_export(api_url, user, connection_id=DEFAULT_CONNECTION_ID, limit=200,
                    cursor=None, debug=False):
    params = {
        "user": user,
        "connection_id": connection_id,
        "limit": int(limit),
    }
    if cursor:
        params["cursor"] = cursor
    data = api.call(api_url, "GET", "/api/agent/slack/dm-export?" + urlencode(params), debug=debug)
    print_json(data)
    return 0


def _get(api_url, path, params, debug):
    params = {k: v for k, v in params.items() if v not in (None, "")}
    print_json(api.call(api_url, "GET", path + "?" + urlencode(params), debug=debug))
    return 0


def slack_replies(api_url, channel, thread_ts, connection_id=DEFAULT_CONNECTION_ID,
                  limit=200, cursor=None, debug=False):
    return _get(api_url, "/api/agent/slack/replies", {
        "channel": channel, "thread_ts": thread_ts, "connection_id": connection_id,
        "limit": int(limit), "cursor": cursor}, debug)


def slack_channels(api_url, connection_id=DEFAULT_CONNECTION_ID, limit=200, cursor=None,
                   debug=False):
    return _get(api_url, "/api/agent/slack/channels", {
        "connection_id": connection_id, "limit": int(limit), "cursor": cursor}, debug)


def slack_search(api_url, query, connection_id=DEFAULT_CONNECTION_ID, count=20, page=1,
                 debug=False):
    return _get(api_url, "/api/agent/slack/search", {
        "q": query, "connection_id": connection_id, "count": int(count),
        "page": int(page)}, debug)


SLACK_MAX_FILE_BYTES = 1024 * 1024 * 1024  # Slack's per-file limit
_UPLOAD_HOST = "files.slack.com"


def _read_text(text_file, text):
    """(text, None) or (None, error message)."""
    if text_file is not None:
        if text_file == "-":
            return sys.stdin.read(), None
        try:
            return Path(text_file).read_text(encoding="utf-8"), None
        except OSError as exc:
            return None, f"cannot read text file {text_file!r}: {exc}"
    return str(text or ""), None


def _read_image(path):
    """(filename, bytes); raises ApiError loudly on a missing/empty/too-big file."""
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        raise api.ApiError(f"cannot read image {path!r}: {exc.strerror or exc}") from None
    if size <= 0:
        raise api.ApiError(f"image {path!r} is empty")
    if size > SLACK_MAX_FILE_BYTES:
        raise api.ApiError(
            f"image {path!r} is too big: {size} bytes > Slack limit {SLACK_MAX_FILE_BYTES} bytes")
    with open(path, "rb") as handle:
        return os.path.basename(path), handle.read()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _upload_bytes(upload_url, content):
    """POST the bytes to Slack's presigned upload URL (the URL is the credential;
    no token is sent and the URL is never printed)."""
    parsed = urlsplit(str(upload_url or ""))
    if parsed.scheme != "https" or parsed.hostname != _UPLOAD_HOST:
        raise api.ApiError("Slack returned an unexpected upload URL host; refusing to upload")
    request = urllib.request.Request(
        upload_url, data=content, method="POST",
        headers={"Content-Type": "application/octet-stream"})
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=120) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    except (OSError, TimeoutError) as exc:
        raise api.ApiError(f"Slack file upload failed: {type(exc).__name__}") from None
    if status == 413:
        raise api.ApiError("Slack file upload rejected: file too big (HTTP 413)")
    if status >= 300:
        raise api.ApiError(f"Slack file upload failed (HTTP {status}); upload URL redacted")


def _upload_image(api_url, path, connection_id, debug):
    """Upload a local image through the presigned flow; returns (file_id, filename)."""
    filename, content = _read_image(path)
    minted = api.call(api_url, "POST", "/api/agent/slack/image-upload-url", body={
        "filename": filename, "length": len(content), "connection_id": connection_id,
    }, debug=debug)
    file_id = minted.get("file_id")
    if not file_id or not minted.get("upload_url"):
        raise api.ApiError("API returned no Slack upload target")
    _upload_bytes(minted["upload_url"], content)
    return file_id, filename


def _report_ts(data):
    print_json(data)
    if data.get("ts_error"):
        print(f"Warning: {data['ts_error']}", file=sys.stderr)
        return 1
    return 0


def slack_post(api_url, channel, text_file=None, text=None,
               connection_id=DEFAULT_POST_CONNECTION_ID, thread_ts=None,
               unfurl_links=True, unfurl_media=True, dry_run=False, image=None,
               debug=False):
    body_text, error = _read_text(text_file, text)
    if error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    if not body_text.strip():
        print("Error: message text is empty", file=sys.stderr)
        return 1
    if image and not dry_run:
        file_id, filename = _upload_image(api_url, image, connection_id, debug)
        payload = {"channel": channel, "text": body_text, "connection_id": connection_id,
                   "file_id": file_id, "title": filename}
        if thread_ts:
            payload["thread_ts"] = thread_ts
        data = api.call(api_url, "POST", "/api/agent/slack/post", body=payload,
                        timeout=60, debug=debug)
        return _report_ts(data)
    if image:
        _read_image(image)  # dry-run still fails loudly on a bad local file
    payload = {
        "channel": channel,
        "text": body_text,
        "connection_id": connection_id,
        "unfurl_links": bool(unfurl_links),
        "unfurl_media": bool(unfurl_media),
        "dry_run": bool(dry_run),
    }
    if thread_ts:
        payload["thread_ts"] = thread_ts
    data = api.call(api_url, "POST", "/api/agent/slack/post", body=payload, debug=debug)
    print_json(data)
    if image and data.get("files_write") is False:
        print("Error: the Slack token lacks the files:write scope; --image would fail. "
              "Add files:write to the Slack app and reinstall it.", file=sys.stderr)
        return 1
    return 0


def slack_attach_image(api_url, channel, ts, image, replace=False, text_file=None, text=None,
                       connection_id=DEFAULT_POST_CONNECTION_ID, debug=False):
    """Attach an image to an existing message: a thread reply, or with
    ``replace`` repost text+image and delete the old (bot-owned) message."""
    body_text = None
    if text_file is not None or text is not None:
        body_text, error = _read_text(text_file, text)
        if error:
            print(f"Error: {error}", file=sys.stderr)
            return 1
    file_id, filename = _upload_image(api_url, image, connection_id, debug)
    payload = {"channel": channel, "ts": ts, "file_id": file_id, "replace": bool(replace),
               "connection_id": connection_id, "title": filename}
    if body_text and body_text.strip():
        payload["text"] = body_text
    data = api.call(api_url, "POST", "/api/agent/slack/attach-image", body=payload,
                    timeout=60, debug=debug)
    return _report_ts(data)
