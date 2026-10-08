"""Discovery and health-test for a connected provider account.

The Zapier gap this closes: after connecting an app, Zapier lets you browse
it — pick a spreadsheet from a dropdown, see a sample record, confirm the
account is who you think it is. Until now a Dapier workflow hardcoded ids
like ``spreadsheet_id`` into action fields with nothing to discover them
from. This module is the domain half of that: a per-provider catalog of
discoverable resources (``spreadsheets``, ``channels``, ``folder``, ...),
a fetcher per resource that lists live items using the connection's own
token, and :func:`test_connection`, which verifies the stored or refreshed
token against the provider's identity endpoint.

Both API surfaces (``/api/agent/*`` for the CLI, ``/api/admin/*`` for the
console) are thin wrappers over this module. Provider calls go through the
same injectable ``transport`` seam as ``oauth_providers``, so tests fake
the network without sockets or moto.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import credentials, records, tokens
from .providers import oauth_providers, slack_tokens, telegram_api

LIMIT_DEFAULT = 25
LIMIT_CAP = 100
# Cap on how many pages one listing may follow (same bound as
# connectors.dropbox._run_entries); providers decide the page size.
_MAX_PAGES = 5

GOOGLE_DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"
SHEETS_API_URL = "https://sheets.googleapis.com/v4/spreadsheets"
CALENDAR_API_URL = "https://www.googleapis.com/calendar/v3"
YOUTUBE_API_URL = "https://www.googleapis.com/youtube/v3"
GMAIL_API_URL = "https://gmail.googleapis.com/gmail/v1"
SPREADSHEET_MIME = "application/vnd.google-apps.spreadsheet"
FOLDER_MIME = "application/vnd.google-apps.folder"
SLACK_API_URL = "https://slack.com/api"
DROPBOX_LIST_URL = "https://api.dropboxapi.com/2/files/list_folder"
DROPBOX_SEARCH_V2_URL = "https://api.dropboxapi.com/2/files/search_v2"
ZOOM_API_URL = "https://api.zoom.us/v2"

PROVIDER_LABELS = {
    "dropbox": "Dropbox",
    "google": "Google",
    "youtube": "YouTube",
    "zoom": "Zoom",
    "slack": "Slack",
    "telegram": "Telegram",
    "mailchimp": "Mailchimp",
    "aws": "AWS",
    "s3": "S3",
}


class DiscoveryError(ValueError):
    """A discovery or test request cannot be served.

    ``status`` is the HTTP status the API wrappers should respond with:
    400 for a malformed request, 404 for an unknown connection or resource,
    502 when the provider rejects or fails the call.
    """

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Param:
    """One query parameter a resource accepts."""

    name: str
    required: bool = False
    description: str = ""


@dataclass(frozen=True)
class Resource:
    """One discoverable resource: what it is and how to list it.

    ``fetch`` has the signature ``(connection, token, params, limit, *,
    transport=None) -> list[dict]``; every item it returns carries at least
    ``id`` and ``name`` so callers can render uniform pickers.
    """

    name: str
    label: str
    description: str
    fetch: object
    params: tuple = ()


def _default_transport(method, url, *, headers, body, timeout=15):
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, response.read()


def _error_detail(status, raw):
    """Pull the provider's message out of an error body, redacted of secrets."""
    try:
        data = json.loads((raw or b"").decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        return ""
    if not isinstance(data, dict):
        return ""
    error = data.get("error")
    if isinstance(error, dict) and error.get("message"):
        return oauth_providers.redact(str(error["message"]))[:200]
    if data.get("error") or data.get("description"):
        return oauth_providers.redact(str(data.get("error") or data.get("description")))[:200]
    return ""


def _request(method, url, token, payload, *, transport=None):
    """One provider call; returns parsed JSON or raises DiscoveryError(502)."""
    headers = {"authorization": f"Bearer {token}"}
    body = None
    if payload is not None:
        headers["content-type"] = "application/json"
        body = json.dumps(payload).encode()
    try:
        status, raw = (transport or _default_transport)(
            method, url, headers=headers, body=body, timeout=15,
        )
    except Exception as exc:
        raise DiscoveryError(
            f"provider request unreachable: {type(exc).__name__}", status=502) from None
    detail = ""
    if status >= 300:
        detail = _error_detail(status, raw)
        raise DiscoveryError(f"provider returned HTTP {status}{': ' + detail if detail else ''}",
                             status=502)
    try:
        data = json.loads(raw.decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        raise DiscoveryError(f"provider returned HTTP {status} with an unreadable body",
                             status=502) from None
    return data


def clamp_limit(value):
    """The page size as a clamped int (bad input falls back to the default)."""
    try:
        limit = int(str(value or "").strip() or LIMIT_DEFAULT)
    except (TypeError, ValueError):
        return LIMIT_DEFAULT
    return max(1, min(limit, LIMIT_CAP))


def access_token(connection, *, transport=None):
    """The connection's bearer token: refreshed OAuth or stored static token."""
    provider = connection.get("provider")
    if provider in records.TOKEN_PROVIDERS:
        try:
            secret = credentials.get_credential(connection["credential_id"])
        except KeyError:
            raise DiscoveryError(f"{provider} connection has no stored token") from None
        token = secret.get("token") or secret.get("access_token")
        if not token:
            raise DiscoveryError(f"{provider} connection has no stored token")
        return token
    try:
        token, _info = tokens.get_access_token(connection, transport=transport)
    except (tokens.TokenError, records.BindingError) as exc:
        raise DiscoveryError(f"connection has no usable token: {exc}", status=502) from None
    return token


# --- Google (Drive and Sheets) ---


def _drive_files_fetcher(mime_type=None):
    def fetch(connection, token, params, limit, *, transport=None):
        """Drive files.list pages, following nextPageToken up to _MAX_PAGES."""
        query = "trashed=false"
        if mime_type:
            query += f" and mimeType='{mime_type}'"
        extra = str(params.get("query") or "").strip()
        if extra:
            query += f" and ({extra})"
        page_params = {
            "pageSize": limit,
            "orderBy": "modifiedTime desc",
            "fields": "files(id,name,mimeType,modifiedTime)",
            "supportsAllDrives": "true",
            "q": query,
        }
        items = []
        for _page in range(_MAX_PAGES):
            url = GOOGLE_DRIVE_FILES_URL + "?" + urllib.parse.urlencode(page_params)
            data = _request("GET", url, token, None, transport=transport)
            found = [
                {
                    "id": item.get("id"),
                    "name": item.get("name"),
                    "mimeType": item.get("mimeType"),
                    "modified": item.get("modifiedTime"),
                }
                for item in data.get("files") or []
                if isinstance(item, dict) and item.get("id")
            ]
            items.extend(found)
            if not found or len(items) >= limit or not data.get("nextPageToken"):
                break
            page_params = {**page_params, "pageToken": data["nextPageToken"]}
        return items[:limit]
    return fetch


def _google_worksheets(connection, token, params, limit, *, transport=None):
    spreadsheet_id = urllib.parse.quote(params["spreadsheet_id"], safe="")
    url = f"{SHEETS_API_URL}/{spreadsheet_id}?fields=sheets.properties"
    data = _request("GET", url, token, None, transport=transport)
    items = []
    for sheet in data.get("sheets") or []:
        props = sheet.get("properties") or {} if isinstance(sheet, dict) else {}
        grid = props.get("gridProperties") or {}
        if props.get("title") is None:
            continue
        items.append({
            "id": str(props.get("sheetId")),
            "name": props.get("title"),
            "rowCount": grid.get("rowCount"),
            "columnCount": grid.get("columnCount"),
        })
    return items


def _google_columns(connection, token, params, limit, *, transport=None):
    spreadsheet_id = urllib.parse.quote(params["spreadsheet_id"], safe="")
    worksheet = params.get("worksheet") or "Sheet1"
    # Quote the whole A1 range once, late: quoting the worksheet name first
    # would double-encode spaces, and leaving "!" safe breaks Sheets' parser.
    range_a1 = urllib.parse.quote(f"{worksheet}!A1:ZZ1", safe="")
    url = f"{SHEETS_API_URL}/{spreadsheet_id}/values/{range_a1}"
    data = _request("GET", url, token, None, transport=transport)
    rows = data.get("values") or []
    headers = rows[0] if rows and isinstance(rows[0], list) else []
    return [{"id": f"col{index + 1}", "name": str(header)}
            for index, header in enumerate(headers)]


def _google_rows(connection, token, params, limit, *, transport=None):
    """The worksheet's first rows, via the same values API as columns.

    The range asks for exactly ``limit`` rows; each item keeps its
    spreadsheet row number (the header is row 1, so it feeds
    sheets_update_row directly) with trailing empty cells trimmed.
    """
    spreadsheet_id = urllib.parse.quote(params["spreadsheet_id"], safe="")
    worksheet = params.get("worksheet") or "Sheet1"
    range_a1 = urllib.parse.quote(f"{worksheet}!A1:ZZ{limit}", safe="")
    url = f"{SHEETS_API_URL}/{spreadsheet_id}/values/{range_a1}"
    data = _request("GET", url, token, None, transport=transport)
    items = []
    for index, row in enumerate(data.get("values") or [], start=1):
        if not isinstance(row, list):
            continue
        values = list(row)
        while values and str(values[-1]) == "":
            values.pop()
        items.append({"id": str(index), "row": index, "values": values})
        if len(items) >= limit:
            break
    return items


def _google_calendars(connection, token, params, limit, *, transport=None):
    """The connection's calendar list, following nextPageToken like Drive."""
    items = []
    page_params = {"maxResults": min(limit, 250)}
    for _page in range(_MAX_PAGES):
        url = CALENDAR_API_URL + "/users/me/calendarList?" + urllib.parse.urlencode(page_params)
        data = _request("GET", url, token, None, transport=transport)
        found = [
            {
                "id": entry.get("id"),
                "name": entry.get("summary"),
                "timeZone": entry.get("timeZone"),
                "primary": bool(entry.get("primary")),
                "accessRole": entry.get("accessRole"),
            }
            for entry in data.get("items") or []
            if isinstance(entry, dict) and entry.get("id")
        ]
        items.extend(found)
        if not found or len(items) >= limit or not data.get("nextPageToken"):
            break
        page_params = {**page_params, "pageToken": data["nextPageToken"]}
    return items[:limit]


def _google_events(connection, token, params, limit, *, transport=None):
    """Upcoming events in one calendar, expanded per occurrence.

    singleEvents + orderBy=startTime lists the window's occurrences in
    start order; the window opens a day back so events that just started
    still show, and an optional ``query`` narrows by text match.
    """
    calendar_id = urllib.parse.quote(params["calendar_id"], safe="")
    page_params = {
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": min(limit, 250),
        "timeMin": (datetime.now(timezone.utc) - timedelta(days=1))
        .strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if params.get("query"):
        page_params["q"] = params["query"]
    items = []
    for _page in range(_MAX_PAGES):
        url = (f"{CALENDAR_API_URL}/calendars/{calendar_id}/events?"
               + urllib.parse.urlencode(page_params))
        data = _request("GET", url, token, None, transport=transport)
        found = []
        for entry in data.get("items") or []:
            if not isinstance(entry, dict) or not entry.get("id"):
                continue
            start = entry.get("start") or {}
            end = entry.get("end") or {}
            found.append({
                "id": entry.get("id"),
                "name": entry.get("summary"),
                "start": start.get("dateTime") or start.get("date"),
                "end": end.get("dateTime") or end.get("date"),
                "location": entry.get("location"),
                "status": entry.get("status"),
                "htmlLink": entry.get("htmlLink"),
            })
        items.extend(found)
        if not found or len(items) >= limit or not data.get("nextPageToken"):
            break
        page_params = {**page_params, "pageToken": data["nextPageToken"]}
    return items[:limit]


def _gmail_labels(connection, token, params, limit, *, transport=None):
    """The mailbox's labels (Gmail's mailboxes), system and user — the
    ``label:<name>`` terms a Gmail watch's query addresses."""
    data = _request("GET", f"{GMAIL_API_URL}/users/me/labels", token, None,
                    transport=transport)
    return [
        {
            "id": label.get("id"),
            "name": label.get("name") or label.get("id"),
            "type": label.get("type"),
            "messages_total": label.get("messagesTotal"),
        }
        for label in data.get("labels") or []
        if isinstance(label, dict) and label.get("id")
    ]


# --- YouTube ---


def _youtube_channel(connection, token, params, limit, *, transport=None):
    url = f"{YOUTUBE_API_URL}/channels?part=snippet&mine=true"
    data = _request("GET", url, token, None, transport=transport)
    items = []
    for channel in data.get("items") or []:
        snippet = channel.get("snippet") or {} if isinstance(channel, dict) else {}
        if not channel.get("id"):
            continue
        items.append({"id": channel["id"], "name": snippet.get("title") or channel["id"]})
    return items


def _youtube_playlists(connection, token, params, limit, *, transport=None):
    url = f"{YOUTUBE_API_URL}/playlists?" + urllib.parse.urlencode(
        {"part": "snippet", "mine": "true", "maxResults": limit})
    data = _request("GET", url, token, None, transport=transport)
    return [
        {"id": item["id"], "name": (item.get("snippet") or {}).get("title") or item["id"]}
        for item in data.get("items") or []
        if isinstance(item, dict) and item.get("id")
    ]


def _youtube_playlist_items(connection, token, params, limit, *, transport=None):
    url = f"{YOUTUBE_API_URL}/playlistItems?" + urllib.parse.urlencode(
        {"part": "snippet,contentDetails", "playlistId": params["playlist_id"],
         "maxResults": limit})
    data = _request("GET", url, token, None, transport=transport)
    items = []
    for item in data.get("items") or []:
        if not isinstance(item, dict):
            continue
        snippet = item.get("snippet") or {}
        details = item.get("contentDetails") or {}
        video_id = details.get("videoId") or (snippet.get("resourceId") or {}).get("videoId")
        if not video_id:
            continue
        items.append({
            "id": video_id,
            "name": snippet.get("title") or video_id,
            "published": snippet.get("publishedAt"),
        })
    return items


def _youtube_videos(connection, token, params, limit, *, transport=None):
    """The connected channel's recent uploads, newest first.

    The uploads playlist id comes from the channel's contentDetails
    (relatedPlaylists.uploads), so the listing needs no search quota and
    also covers videos not placed in any public playlist.
    """
    url = f"{YOUTUBE_API_URL}/channels?part=contentDetails&mine=true"
    data = _request("GET", url, token, None, transport=transport)
    uploads = ""
    for channel in data.get("items") or []:
        if isinstance(channel, dict) and channel.get("id"):
            related = (channel.get("contentDetails") or {}).get("relatedPlaylists") or {}
            uploads = related.get("uploads") or ""
            break
    if not uploads:
        return []
    return _youtube_playlist_items(connection, token, {"playlist_id": uploads},
                                   limit, transport=transport)


# --- Dropbox ---


def _dropbox_folder(connection, token, params, limit, *, transport=None):
    """One level of a folder; an empty ``path`` falls back to the connection's
    ``root_path`` (``""`` or ``"/"`` both stand for the Dropbox root), the same
    convention connectors.dropbox uses when joining paths."""
    path = str(params.get("path") or "").strip() or connection.get("root_path") or ""
    data = _request("POST", DROPBOX_LIST_URL, token,
                    {"path": path, "recursive": False, "limit": limit},
                    transport=transport)
    return [
        {
            "id": entry.get("id"),
            "name": entry.get("name"),
            "type": entry.get(".tag"),
            "path": entry.get("path_display"),
        }
        for entry in data.get("entries") or []
        if isinstance(entry, dict) and entry.get("id")
    ]


def _dropbox_search(connection, token, params, limit, *, transport=None):
    """files/search_v2 matches for one name query.

    search_v2 nests the actual metadata one level deep
    (``match.metadata.metadata``); matches without that payload fall back to
    the outer metadata so both response shapes render. Items carry the
    ``path`` the actions need, like the folder listing.
    """
    data = _request("POST", DROPBOX_SEARCH_V2_URL, token,
                    {"query": str(params.get("query") or ""),
                     "options": {"max_results": limit},
                     "include_highlights": False},
                    transport=transport)
    items = []
    for match in data.get("matches") or []:
        if not isinstance(match, dict):
            continue
        metadata = match.get("metadata") or {}
        entry = metadata.get("metadata")
        if not isinstance(entry, dict):
            entry = metadata
        if not entry.get("id"):
            continue
        items.append({
            "id": entry.get("id"),
            "name": entry.get("name"),
            "type": entry.get(".tag"),
            "path": entry.get("path_display"),
        })
    return items


# --- Slack ---


def _slack_call(method, token, payload, *, transport=None):
    data = _request("POST", f"{SLACK_API_URL}/{method}", token, payload, transport=transport)
    if not data.get("ok"):
        error = data.get("error") or "unknown_error"
        raise DiscoveryError(f"Slack rejected {method}: {oauth_providers.redact(str(error))}",
                             status=502)
    return data


def _slack_channels(connection, token, params, limit, *, transport=None):
    """conversations.list pages, following next_cursor up to _MAX_PAGES."""
    payload = {"limit": min(limit, 200), "exclude_archived": True}
    items = []
    for _page in range(_MAX_PAGES):
        data = _slack_call("conversations.list", token, payload, transport=transport)
        found = [
            {
                "id": channel.get("id"),
                "name": channel.get("name"),
                "type": "private" if channel.get("is_private") else "channel",
            }
            for channel in data.get("channels") or []
            if isinstance(channel, dict) and channel.get("id")
        ]
        items.extend(found)
        if not found or len(items) >= limit:
            break
        cursor = (data.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
        payload = {**payload, "cursor": cursor}
    return items[:limit]


def _slack_users(connection, token, params, limit, *, transport=None):
    """users.list pages, following next_cursor up to _MAX_PAGES."""
    payload = {"limit": min(limit, 200)}
    items = []
    for _page in range(_MAX_PAGES):
        data = _slack_call("users.list", token, payload, transport=transport)
        found = []
        for member in data.get("members") or []:
            if not isinstance(member, dict) or not member.get("id"):
                continue
            profile = member.get("profile") or {}
            entry = {
                "id": member["id"],
                "name": profile.get("real_name") or member.get("name"),
                "handle": member.get("name"),
            }
            if profile.get("email"):
                # slack_find_user's email field picks from this listing, so
                # the address must travel with the item when Slack reveals it.
                entry["email"] = profile["email"]
            found.append(entry)
        items.extend(found)
        if not found or len(items) >= limit:
            break
        cursor = (data.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
        payload = {**payload, "cursor": cursor}
    return items[:limit]


def _slack_messages(connection, token, params, limit, *, transport=None):
    data = _slack_call("conversations.history", token,
                       {"channel": params["channel"], "limit": limit},
                       transport=transport)
    items = []
    for message in data.get("messages") or []:
        if not isinstance(message, dict) or not message.get("ts"):
            continue
        text = str(message.get("text") or "")
        items.append({
            "id": message["ts"],
            "name": text[:100],
            "user": message.get("user"),
            "ts": message.get("ts"),
        })
    return items


# --- Telegram ---


def _telegram_updates(connection, token, params, limit, *, transport=None):
    result = telegram_api.call(token, "getUpdates",
                               {"limit": min(limit, 100)}, transport=transport)
    items = []
    for update in result or []:
        if not isinstance(update, dict):
            continue
        message = (update.get("message") or update.get("edited_message")
                   or update.get("channel_post") or update.get("edited_channel_post") or {})
        chat = message.get("chat") or {}
        chat_name = (chat.get("title") or chat.get("username")
                     or chat.get("first_name") or chat.get("id"))
        items.append({
            "id": str(update.get("update_id")),
            "name": f"chat {chat_name}" if chat_name else f"update {update.get('update_id')}",
            "chat_id": chat.get("id"),
            "chat_type": chat.get("type"),
            "text": str(message.get("text") or "")[:100],
        })
    return items


def _telegram_chats(connection, token, params, limit, *, transport=None):
    """Chats seen in the bot's pending updates, deduplicated and sorted.

    Telegram serves ``getUpdates`` only while no webhook is set — the same
    webhook Dapier's triggers use — so live bots get Telegram's conflict
    message back instead of a listing.
    """
    try:
        result = telegram_api.call(token, "getUpdates",
                                   {"limit": min(limit, 100)}, transport=transport)
    except telegram_api.TelegramApiError as exc:
        raise DiscoveryError(str(exc), status=502) from None
    chats = {}
    for update in result or []:
        if not isinstance(update, dict):
            continue
        for kind in ("message", "edited_message", "channel_post", "edited_channel_post"):
            chat = ((update.get(kind) or {}).get("chat") or {})
            chat_id = chat.get("id")
            if chat_id is None:
                continue
            chats[str(chat_id)] = {
                "id": str(chat_id),
                "name": str(chat.get("title") or chat.get("username")
                            or chat.get("first_name") or chat_id),
                "type": str(chat.get("type") or ""),
            }
    return [chats[key] for key in sorted(chats)]


def _telegram_chat(connection, token, params, limit, *, transport=None):
    chat = telegram_api.call(token, "getChat",
                             {"chat_id": params["chat_id"]}, transport=transport) or {}
    name = chat.get("title") or chat.get("username") or chat.get("first_name") or chat.get("id")
    return [{"id": str(chat.get("id")), "name": name, "type": chat.get("type")}]


# --- Zoom ---

# The /users/me/meetings listing windows the meeting fetcher serves:
# ``upcoming`` (scheduled meetings, the pickable resource) and
# ``previous_meetings`` (the ones already held). Zoom's other list types
# are refused rather than passed through.
ZOOM_MEETING_LIST_TYPES = ("upcoming", "previous_meetings")


def _zoom_meetings_fetcher(default_type="upcoming"):
    """One Zoom meeting listing with a fixed default ``type`` window.

    ``type`` may still be overridden through the listing's params (declared
    as an optional Param on the resource); the default is baked per
    resource, so ``past_meetings`` answers with ``previous_meetings`` even
    when the picker sends no params — the same factory shape the Drive
    file listings use.
    """
    def fetch(connection, token, params, limit, *, transport=None):
        listing_type = str((params or {}).get("type") or "").strip() or default_type
        if listing_type not in ZOOM_MEETING_LIST_TYPES:
            raise DiscoveryError(
                "zoom meetings type must be one of: "
                + ", ".join(ZOOM_MEETING_LIST_TYPES), status=400)
        url = f"{ZOOM_API_URL}/users/me/meetings?" + urllib.parse.urlencode(
            {"type": listing_type, "per_page": limit})
        data = _request("GET", url, token, None, transport=transport)
        return [
            {
                "id": str(meeting.get("id")),
                "name": meeting.get("topic"),
                "start_time": meeting.get("start_time"),
                "join_url": meeting.get("join_url"),
            }
            for meeting in data.get("meetings") or []
            if isinstance(meeting, dict) and meeting.get("id")
        ]
    return fetch


_zoom_meetings = _zoom_meetings_fetcher("upcoming")
_zoom_past_meetings = _zoom_meetings_fetcher("previous_meetings")


def _zoom_webinars(connection, token, params, limit, *, transport=None):
    """The upcoming webinars a Zoom connection can pick from (Zapier's
    webinar pickers): the ``/users/me/webinars`` list, same item shape as
    the meetings listing."""
    url = f"{ZOOM_API_URL}/users/me/webinars?" + urllib.parse.urlencode(
        {"type": "upcoming", "per_page": limit})
    data = _request("GET", url, token, None, transport=transport)
    return [
        {
            "id": str(webinar.get("id")),
            "name": webinar.get("topic"),
            "start_time": webinar.get("start_time"),
            "join_url": webinar.get("join_url"),
        }
        for webinar in data.get("webinars") or []
        if isinstance(webinar, dict) and webinar.get("id")
    ]


def _zoom_recordings(connection, token, params, limit, *, transport=None):
    since = (datetime.now(timezone.utc) - timedelta(days=30)).date().isoformat()
    url = f"{ZOOM_API_URL}/users/me/recordings?" + urllib.parse.urlencode(
        {"from": since, "per_page": limit})
    data = _request("GET", url, token, None, transport=transport)
    return [
        {
            "id": str(meeting.get("id")),
            "name": meeting.get("topic"),
            "start_time": meeting.get("start_time"),
            "recording_count": len(meeting.get("recording_files") or []),
        }
        for meeting in data.get("meetings") or []
        if isinstance(meeting, dict) and meeting.get("id")
    ]


CATALOG = {
    "google": [
        Resource("spreadsheets", "Spreadsheets",
                 "Spreadsheets the connection can reach, newest first",
                 _drive_files_fetcher(SPREADSHEET_MIME)),
        Resource("files", "Drive files",
                 "Files in the connection's Drive, newest first",
                 _drive_files_fetcher(),
                 (Param("query", False, "Extra Drive filter, e.g. name contains 'report'"),)),
        Resource("folders", "Drive folders",
                 "Folders in the connection's Drive, newest first",
                 _drive_files_fetcher(FOLDER_MIME)),
        Resource("worksheets", "Worksheets", "Tabs in one spreadsheet", _google_worksheets,
                 (Param("spreadsheet_id", True, "Spreadsheet ID from the spreadsheets list"),)),
        Resource("columns", "Columns", "Header row of one worksheet", _google_columns,
                 (Param("spreadsheet_id", True, "Spreadsheet ID from the spreadsheets list"),
                  Param("worksheet", False, "Worksheet name (default Sheet1)"))),
        Resource("rows", "Rows", "First rows of one worksheet, trailing empty cells trimmed",
                 _google_rows,
                 (Param("spreadsheet_id", True, "Spreadsheet ID from the spreadsheets list"),
                  Param("worksheet", False, "Worksheet name (default Sheet1)"))),
        Resource("calendars", "Calendars",
                 "Calendars the connection can see, including the primary",
                 _google_calendars),
        Resource("events", "Calendar events",
                 "Upcoming events in one calendar, start order",
                 _google_events,
                 (Param("calendar_id", True, "Calendar ID from the calendars list"),
                  Param("query", False, "Text to match against event fields"))),
        Resource("labels", "Gmail labels",
                 "Labels in the connection's Gmail, system and user",
                 _gmail_labels),
    ],
    "youtube": [
        Resource("channel", "Channel", "The connected YouTube channel", _youtube_channel),
        Resource("playlists", "Playlists", "Playlists owned by the channel", _youtube_playlists),
        Resource("playlist_items", "Playlist videos", "Videos in one playlist, newest first",
                 _youtube_playlist_items,
                 (Param("playlist_id", True, "Playlist ID from the playlists list"),)),
        Resource("videos", "Channel videos", "The channel's recent uploads, newest first",
                 _youtube_videos),
    ],
    "dropbox": [
        Resource("folder", "Folder entries",
                 "One level of a Dropbox folder; path defaults to the connection's root",
                 _dropbox_folder,
                 (Param("path", False, "Folder path (default: the connection's root_path)"),)),
        Resource("search", "Search matches",
                 "Files and folders matching a name query",
                 _dropbox_search,
                 (Param("query", True, "Name text to search for"),)),
    ],
    "slack": [
        Resource("channels", "Channels", "Active channels in the workspace", _slack_channels),
        Resource("users", "Users", "People in the workspace", _slack_users),
        Resource("messages", "Messages", "Recent messages in one channel", _slack_messages,
                 (Param("channel", True, "Channel ID from the channels list"),)),
    ],
    "telegram": [
        Resource("chats", "Chats",
                 "Chats the bot has received messages from recently",
                 _telegram_chats),
        Resource("updates", "Recent updates",
                 "Latest bot updates with their chats (empty while a webhook is active)",
                 _telegram_updates),
        Resource("chat", "Chat", "One chat's profile", _telegram_chat,
                 (Param("chat_id", True, "Chat id (@name or -100… id)"),)),
    ],
    "zoom": [
        Resource("meetings", "Meetings", "Upcoming meetings on the account", _zoom_meetings),
        Resource("past_meetings", "Past meetings",
                 "Meetings already held on the account, newest first",
                 _zoom_past_meetings,
                 (Param("type", False,
                        "Meeting window — previous_meetings (default) or upcoming"),)),
        Resource("recordings", "Recordings", "Recordings from the last 30 days",
                 _zoom_recordings),
        Resource("webinars", "Webinars", "Upcoming webinars on the account",
                 _zoom_webinars),
    ],
}


def resources_for(provider):
    """The discoverable resources for one provider (empty when none)."""
    return CATALOG.get(provider, [])


def catalog_view(connection):
    """The JSON-safe catalog for one connection (``GET …/discover``)."""
    provider = connection.get("provider")
    return {
        "connection": connection.get("connection_id"),
        "provider": provider,
        "resources": [
            {
                "name": resource.name,
                "label": resource.label,
                "description": resource.description,
                "params": [
                    {"name": param.name, "required": param.required,
                     "description": param.description}
                    for param in resource.params
                ],
            }
            for resource in resources_for(provider)
        ],
    }


def discover(connection, resource, params, *, limit=LIMIT_DEFAULT, transport=None):
    """List one resource's live items for a connection.

    ``params`` may carry extra query-string noise (``limit`` is the page
    size, handled here); anything the resource does not declare is a 400.
    Raises :class:`DiscoveryError` (404) for an unknown resource.
    """
    provider = connection.get("provider")
    wanted = str(resource or "")
    entry = next((item for item in resources_for(provider) if item.name == wanted), None)
    if entry is None and "." in wanted:
        # Field markers carry the full ``connector.name`` registry key while
        # the catalog is per-provider, so the connector prefix is redundant.
        wanted = wanted.rpartition(".")[2]
        entry = next((item for item in resources_for(provider) if item.name == wanted),
                     None)
    if entry is None:
        known = ", ".join(item.name for item in resources_for(provider)) or "none"
        raise DiscoveryError(
            f"Unknown resource '{resource}' for {provider}; known: {known}", status=404)
    known_params = {param.name for param in entry.params}
    clean = {}
    requested = limit
    for name, value in (params or {}).items():
        if name == "limit":
            requested = value
            continue
        if name not in known_params:
            raise DiscoveryError(
                f"Unknown parameter '{name}' for {provider}/{entry.name}; "
                f"known: {', '.join(sorted(known_params)) or 'none'}")
        if str(value or "").strip():
            clean[name] = str(value).strip()
    for param in entry.params:
        if param.required and not clean.get(param.name):
            raise DiscoveryError(f"{entry.name} needs {param.name}: {param.description}")
    token = access_token(connection, transport=transport)
    items = entry.fetch(connection, token, clean, clamp_limit(requested),
                        transport=transport) or []
    return [dict(item) for item in items][:clamp_limit(requested)]


def test_connection(connection, *, transport=None):
    """Verify the connection's token against its provider's identity endpoint.

    Never raises: the response is ``{ok, provider, detail, identity}`` with
    ``ok: False`` carrying the reason, so the API wrappers can answer 200
    and the CLI can exit nonzero on a failed check.
    """
    provider = connection.get("provider")
    label = PROVIDER_LABELS.get(provider, provider)
    try:
        from .importing import TOKEN_PROVIDER_VERIFIERS, verify_token_provider
        if provider in TOKEN_PROVIDER_VERIFIERS:
            account_id, title = verify_token_provider(provider, access_token(connection), transport=transport)
            return {"ok": True, "provider": provider, "detail": f"{title} verified",
                    "identity": {"id": account_id, "name": title}}
        if provider == "slack":
            token = access_token(connection)
            account_id, title = slack_tokens.verify_account(token, transport=transport)
            # Who the token acts as (app or person) rides along so the API
            # can backfill connections verified before it was recorded.
            acts_as = slack_tokens.describe_token(token, transport=transport)
            label = slack_tokens.identity_label(acts_as)
            suffix = f" — {label}" if label else ""
            return {"ok": True, "provider": provider,
                    "detail": f"Slack token verified as {title}{suffix}",
                    "identity": {"id": account_id, "name": title},
                    "account_identity": acts_as}
        if provider == "telegram":
            bot_id, title = telegram_api.get_me(access_token(connection), transport=transport)
            return {"ok": True, "provider": provider,
                    "detail": f"Telegram bot verified as {title}",
                    "identity": {"id": str(bot_id), "name": title}}
        token, info = tokens.get_access_token(connection, transport=transport)
        account_id, title = oauth_providers.verify_account(provider, token, transport=transport)
        refreshed = bool((info or {}).get("refreshed"))
        detail = (f"Refreshed the {label} access token" if refreshed
                  else f"{label} connection verified")
        return {"ok": True, "provider": provider, "detail": detail,
                "identity": {"id": account_id, "name": title}}
    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        return {"ok": False, "provider": provider, "detail": f"{label}: {message}"}
