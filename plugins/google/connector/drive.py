"""Google Drive connector: the find-file, upload, read, share, copy,
delete (trash), and create-folder actions over a Google connection, plus
the poll sources (new/updated/deleted file) and the trigger chip's sample
pull.

``drive_find_file`` searches the connection's Drive for a file by name and
hands its id to the next action in the chain; ``drive_upload_file`` writes
one back, sourced like the S3 upload (``source_url``, a staged ``source_s3``
object, or inline content — see ``engine.actions.s3``); ``drive_read_file``
downloads one and stages the bytes for the steps that follow (Google-native
docs export first); ``drive_share_file`` creates a permission,
``drive_copy_file`` duplicates a file, ``drive_delete_file`` trashes one
and ``drive_create_folder`` makes a folder to file things into. The served
resources are the same Drive ``files`` listing (items carry ``mimeType``,
so folder pickers filter client-side) and its folder-only variant;
discovery delegates to the shared provider layer like every other
connection.
"""
import urllib.parse
from datetime import datetime, timezone

from plugins.google.runners.drive import (
    run_drive_copy_file,
    run_drive_create_folder,
    run_drive_delete_file,
    run_drive_find_file,
    run_drive_move_file,
    run_drive_read_file,
    run_drive_share_file,
    run_drive_upload_file,
)
from src.dapier.connections import discovery as provider
from src.dapier.triggers.poll_sources import PollSource, register_source
from src.dapier.connectors.registry import Action, Discovery, register, register_discovery


def _run_files(connection, params, *, transport=None):
    return provider.discover(connection, "files", params, transport=transport)


def _run_folders(connection, params, *, transport=None):
    return provider.discover(connection, "folders", params, transport=transport)


register_discovery(Discovery(
    name="files",
    connector="google-drive",
    label="Drive files",
    description="Files in the connection's Drive, newest first",
    params=({"key": "query", "label": "Drive query", "type": "text",
             "help": "Extra Drive filter, e.g. name contains 'report'"},),
    run=_run_files,
))

register_discovery(Discovery(
    name="folders",
    connector="google-drive",
    label="Drive folders",
    description="Folders in the connection's Drive, newest first",
    run=_run_folders,
))

register(Action(
    type="drive_find_file",
    label="Drive: find file",
    icon="file-text",
    description="Find the most recently modified Drive file matching a name (Find File)",
    run=lambda action, event, workflow_id, steps=None: run_drive_find_file(action, event, steps=steps),
    required=frozenset({"connection_id", "name"}),
    optional=frozenset({"match", "folder"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "google", "required": True},
        {"key": "name", "label": "File name", "placeholder": "report.pdf", "required": True,
         "help": "Searched as a substring unless Match is exact",
         "discover": {"resource": "google-drive.files"}},
        {"key": "folder", "label": "Folder ID",
         "help": "Restricts the search to one folder's children",
         "discover": {"resource": "google-drive.folders"}},
        {"key": "match", "label": "Match", "type": "select",
         "options": ["contains", "exact"], "default": "contains"},
    ),
))

register(Action(
    type="drive_upload_file",
    label="Upload file",
    icon="file-text",
    description=("Upload one file into the connection's Google Drive (Drive v3 "
                 "multipart; Zapier's Upload File). Content comes from exactly one "
                 "of source_url (a download URL), source_s3 {bucket, key} (a staged "
                 "object, e.g. dropbox_read_file's output), or inline content. "
                 "Output: {file_id, name, mime_type, size, webViewLink}."),
    run=lambda action, event, workflow_id, steps=None: run_drive_upload_file(
        action, event, steps=steps),
    required=frozenset({"connection_id", "name"}),
    optional=frozenset({"source_url", "source_s3", "content", "folder_id",
                        "content_type"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "google", "required": True},
        {"key": "name", "label": "File name", "placeholder": "report.pdf", "required": True,
         "help": "The target filename in Drive"},
        {"key": "source_url", "label": "Source URL", "type": "url",
         "placeholder": "https://www.googleapis.com/drive/v3/files/{id}?alt=media",
         "help": ("One content source: a download URL — or source_s3 {bucket, key} "
                  "for a staged object, or inline content")},
        {"key": "content", "label": "Content",
         "help": "Inline text content — takes templates, e.g. {trigger.text}"},
        {"key": "folder_id", "label": "Folder ID",
         "help": "Uploads into one folder (defaults to the Drive root)",
         "discover": {"resource": "google-drive.folders"}},
        {"key": "content_type", "label": "Content type",
         "placeholder": "application/octet-stream"},
    ),
))

register(Action(
    type="drive_share_file",
    label="Drive: share file",
    icon="file-text",
    description=("Share one Drive file by creating a permission (Drive v3 "
                 "permissions.create; Zapier's Share File). Role is "
                 "reader/commenter/writer; Share with picks user, group, "
                 "domain, or anyone — a user or group grant needs the "
                 "grantee's email address. Output: {shared, file_id, "
                 "permission_id, role, type}."),
    run=lambda action, event, workflow_id, steps=None: run_drive_share_file(
        action, event, steps=steps),
    required=frozenset({"connection_id", "file_id"}),
    optional=frozenset({"role", "share_type", "email_address"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "google", "required": True},
        {"key": "file_id", "label": "File ID", "required": True,
         "discover": {"resource": "google-drive.files"}},
        {"key": "role", "label": "Role", "type": "select",
         "options": ["reader", "commenter", "writer"], "default": "reader"},
        {"key": "share_type", "label": "Share with", "type": "select",
         "options": ["user", "group", "domain", "anyone"], "default": "user"},
        {"key": "email_address", "label": "Email address", "type": "email",
         "help": "The user or group to grant — required for those share types"},
    ),
))

register(Action(
    type="drive_copy_file",
    label="Drive: copy file",
    icon="file-text",
    description=("Copy one Drive file (Drive v3 files.copy); the optional "
                 "name names the copy, else Drive's \"Copy of …\". Output: "
                 "{file_id, name, mime_type, size, webViewLink} — the "
                 "upload's keys, so the file steps chain."),
    run=lambda action, event, workflow_id, steps=None: run_drive_copy_file(
        action, event, steps=steps),
    required=frozenset({"connection_id", "file_id"}),
    optional=frozenset({"name"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "google", "required": True},
        {"key": "file_id", "label": "File ID", "required": True,
         "discover": {"resource": "google-drive.files"}},
        {"key": "name", "label": "Copy name",
         "help": "Defaults to Drive's \"Copy of <original name>\""},
    ),
))

register(Action(
    type="drive_delete_file",
    label="Drive: delete file",
    icon="file-text",
    description=("Delete one Drive file (Zapier's Delete File). The default "
                 "moves the file to the trash (Drive v3 files.update with "
                 "trashed: true) — recoverable from Drive's trash; "
                 "Delete permanently calls files.delete instead, which "
                 "destroys the file outright. Output: {trashed, permanent, "
                 "file_id, name}."),
    run=lambda action, event, workflow_id, steps=None: run_drive_delete_file(
        action, event, steps=steps),
    required=frozenset({"connection_id", "file_id"}),
    optional=frozenset({"permanent"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "google", "required": True},
        {"key": "file_id", "label": "File ID", "required": True,
         "discover": {"resource": "google-drive.files"},
         "help": "Rendered from the event — drive_find_file's file.id chains here"},
        {"key": "permanent", "label": "Delete permanently", "type": "boolean",
         "default": "false",
         "help": "Off (default): the file moves to Drive's trash, "
                 "recoverable. On: files.delete destroys it — no trash "
                 "stop, no way back"},
    ),
))

register(Action(
    type="drive_move_file",
    label="Drive: move file",
    icon="file-text",
    description=("Move one Drive file between folders (Drive v3 files.update "
                 "with addParents/removeParents; Zapier's Move File). At "
                 "least one of Add to folder / Remove from folder is "
                 "required; adding keeps the file's other parents. Output: "
                 "{moved: true, file_id, name, parents} — the resulting "
                 "parents."),
    run=lambda action, event, workflow_id, steps=None: run_drive_move_file(
        action, event, steps=steps),
    required=frozenset({"connection_id", "file_id"}),
    optional=frozenset({"add_parent", "remove_parent"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "google", "required": True},
        {"key": "file_id", "label": "File ID", "required": True,
         "discover": {"resource": "google-drive.files"},
         "help": "Rendered from the event — drive_find_file's file.id chains here"},
        {"key": "add_parent", "label": "Add to folder",
         "discover": {"resource": "google-drive.folders"},
         "help": "Folder id to file the object into — its other folders stay"},
        {"key": "remove_parent", "label": "Remove from folder",
         "discover": {"resource": "google-drive.folders"},
         "help": "Folder id to take the object out of (a real move names "
                 "both fields)"},
    ),
))

register(Action(
    type="drive_create_folder",
    label="Drive: create folder",
    icon="file-text",
    description=("Create one Drive folder (Drive v3 files.create with the "
                 "folder mimeType; Zapier's Create Folder). The optional "
                 "parent_folder_id places it, else it lands at the Drive "
                 "root. Output: {folder_id, name, url} — file uploads into "
                 "it via drive_upload_file's folder_id."),
    run=lambda action, event, workflow_id, steps=None: run_drive_create_folder(
        action, event, steps=steps),
    required=frozenset({"connection_id", "name"}),
    optional=frozenset({"parent_folder_id"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "google", "required": True},
        {"key": "name", "label": "Folder name", "placeholder": "Invoices 2026",
         "required": True, "help": "Rendered from the event like every field"},
        {"key": "parent_folder_id", "label": "Parent folder ID",
         "help": "Creates inside one folder (defaults to the Drive root)",
         "discover": {"resource": "google-drive.folders"}},
    ),
))


register(Action(
    type="drive_read_file",
    label="Drive: read file",
    icon="file-text",
    description=("Download one Drive file and stage it for the steps that "
                 "follow (Zapier's Read File). file_id renders from the event — "
                 "chain drive_find_file's output.file.id or a file.created "
                 "trigger. Google-native docs export first (export_as; "
                 "document/presentation → PDF, spreadsheet → CSV, drawing → "
                 "PNG by default). Output: {filename, size, content_type, "
                 "bucket, key} — pair with Amazon S3's source_s3 or Slack's "
                 "upload file to move the bytes on."),
    run=lambda action, event, workflow_id, steps=None: run_drive_read_file(
        action, event, steps=steps),
    required=frozenset({"connection_id", "file_id"}),
    optional=frozenset({"export_as"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "google", "required": True},
        {"key": "file_id", "label": "File ID", "required": True,
         "discover": {"resource": "google-drive.files"},
         "help": "Rendered from the event — drive_find_file's file.id chains here"},
        {"key": "export_as", "label": "Export as",
         "placeholder": "application/pdf — for Google-native files",
         "help": ("Target mime when the file is a Google Doc/Sheet/Slide/"
                  "Drawing (defaults: document & presentation → PDF, "
                  "spreadsheet → CSV, drawing → PNG)")},
    ),
))


# --- trigger discovery: file options for the find action's name field ----------

from src.dapier.connectors import trigger_discovery  # noqa: E402
from src.dapier.connectors.trigger_discovery import (  # noqa: E402
    DEFAULT_LIMIT,
    TriggerDiscovery,
    register_trigger_discovery,
)


def _fetch_file_options(event=None, connection_id=None, limit=DEFAULT_LIMIT):
    """Drive file options via the registry listing (first connected Google
    connection when no id is named)."""
    return trigger_discovery.options_from_registry(
        "google-drive.files", connection_id, limit, provider="google",
        option_of=lambda item: {"value": item.get("id"),
                                "label": item.get("name") or item.get("id")})


register_trigger_discovery(TriggerDiscovery(
    connector="google-drive", label="Google Drive", kind="options",
    resource="google-drive.files",
    fetch=_fetch_file_options))


def _fetch_folder_options(event=None, connection_id=None, limit=DEFAULT_LIMIT):
    """Drive folder options via the registry listing (first connected Google
    connection when no id is named) — the folder-only listing, so the
    trigger config's folder pickers offer folders only."""
    return trigger_discovery.options_from_registry(
        "google-drive.folders", connection_id, limit, provider="google",
        option_of=lambda item: {"value": item.get("id"),
                                "label": item.get("name") or item.get("id")})


register_trigger_discovery(TriggerDiscovery(
    connector="google-drive", label="Google Drive", kind="options",
    resource="google-drive.folders",
    fetch=_fetch_folder_options))


# --- poll source: "New File in Folder" on the poll-trigger schedule ---------
#
# The files.list endpoint is JSON, so the generic poll trigger can already
# read it — what it cannot do is the "new file" semantics: the listing has
# no per-file cursor and the pickers order by modifiedTime, so an edit
# re-lists its file at the top. The ``google-drive.files`` source owns both
# halves of the fix — a createdTime watermark seeded on the first fire, and
# per-file ids the seen store dedupes — and publishes
# ``google-drive``/``file.created`` events so workflows match the palette
# chip while staying scoped through the poll-name filter.

DRIVE_POLL_PAGE_SIZE = 100
DRIVE_POLL_PAGES = 3  # ~300 files per fire, the pickers' listing order

# Older than any Drive createdTime: the discovery sample's "show the
# folder's newest current file" fetch runs against this watermark.
DRIVE_EPOCH_CURSOR = "0000-01-01T00:00:00.000Z"


def _drive_now():
    """Drive-format UTC now: files.list renders createdTime in exactly this
    shape, so the seed watermark compares against it as plain text."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _drive_poll_validate(body):
    """Save-time fetch spec: ``folder_id`` (required — Zapier's trigger is
    folder-scoped), the ``connection_id`` of the Google connection to poll
    as (required — the fetch refreshes its OAuth token), plus the fetch
    defaults every stored drive poll carries."""
    from src.dapier.triggers.email_triggers import TriggerError

    body = body if isinstance(body, dict) else {}
    folder_id = str(body.get("folder_id") or "").strip()
    if not folder_id:
        raise TriggerError("folder_id is required: name the folder to watch")
    if not str(body.get("connection_id") or "").strip():
        raise TriggerError(
            "connection_id is required: pick the Google connection to poll as")
    return {
        "folder_id": folder_id,
        "drive_id": str(body.get("drive_id") or "").strip(),
        "cursor_mode": "next_cursor",
        "id_path": "id",
        "url": "",
    }


def _list_folder_files(token, folder_id, *, drive_id=None, transport=None):
    """The folder's files (up to ~300: three pages of 100), newest created
    first, with the fields the events and pickers share."""
    page_params = {
        "q": f"'{folder_id}' in parents and trashed=false and mimeType != 'application/vnd.google-apps.folder'",
        "orderBy": "createdTime desc",
        "pageSize": DRIVE_POLL_PAGE_SIZE,
        "fields": "nextPageToken,files(id,name,mimeType,createdTime,modifiedTime,size,webViewLink)",
        "supportsAllDrives": "true",
        "includeItemsFromAllDrives": "true",
    }
    if drive_id:
        page_params.update({"corpora": "drive", "driveId": drive_id})
    files = []
    for _page in range(DRIVE_POLL_PAGES):
        url = (provider.GOOGLE_DRIVE_FILES_URL + "?"
               + urllib.parse.urlencode(page_params))
        data = provider._request("GET", url, token, None, transport=transport)
        page = [entry for entry in data.get("files") or []
                if isinstance(entry, dict) and entry.get("id")
                and entry.get("mimeType") != "application/vnd.google-apps.folder"]
        files.extend(page)
        if not page or len(files) >= DRIVE_POLL_PAGE_SIZE * DRIVE_POLL_PAGES \
                or not data.get("nextPageToken"):
            return files
        page_params = {**page_params, "pageToken": data["nextPageToken"]}
    return files


def _drive_poll_fetch(item, cursor=None, *, transport=None):
    """One poll page as ``(items, next_cursor)`` for ``triggers.poll_sources``.

    Lists the folder through Drive's files.list (the provider's shared
    request path), with the bearer token from ``poll_triggers._bearer_token``
    so the connection's OAuth token is refreshed exactly like the classic
    fetch. Items are the API's own file objects (id, name, mimeType,
    createdTime, modifiedTime, size, webViewLink) — JSON-safe as returned.

    The cursor is an ISO ``createdTime`` watermark (Drive renders uniform
    UTC timestamps, so text comparison orders them). With no stored cursor —
    the first fire after enabling — the fetch only seeds the watermark at
    now and emits nothing: the folder's existing files are history, not
    news. With a cursor, only files created strictly after it fire, oldest
    first, and the parked cursor is the newest fired createdTime (the
    incoming one when nothing qualifies; ``fire`` parks it only once the
    page drains). Keying on createdTime means an edit to an old file never
    fires a bogus "file.created"; the seen store then dedupes the re-listed
    id. Raises ``RuntimeError`` on a failed fetch, like every poll source.
    """
    from src.dapier.triggers import poll_triggers

    if not str(item.get("connection_id") or "").strip():
        raise RuntimeError("poll source 'google-drive.files' needs connection_id: "
                           "the Google connection to poll as")
    folder_id = str(item.get("folder_id") or "").strip()
    if not folder_id:
        raise RuntimeError("poll source 'google-drive.files' needs a stored folder_id")
    token = poll_triggers._bearer_token(item["connection_id"])
    try:
        files = _list_folder_files(token, folder_id, drive_id=item.get("drive_id"), transport=transport)
    except provider.DiscoveryError as exc:
        raise RuntimeError(f"drive poll failed: {exc}") from None
    if cursor is None:
        # First fire: seed the watermark at now (the folder's existing files
        # predate it) without emitting anything.
        return [], _drive_now()
    watermark = str(cursor)
    fresh = sorted(
        (file for file in files if str(file.get("createdTime") or "") > watermark),
        key=lambda file: (str(file.get("createdTime") or ""), str(file.get("id") or "")))
    return fresh, str(fresh[-1]["createdTime"]) if fresh else watermark


def _drive_poll_view(item):
    """The provider params ``public_view`` shows beside ``source``."""
    return {"folder_id": item.get("folder_id"), "drive_id": item.get("drive_id")}


register_source(PollSource(
    name="google-drive.files", connector="google-drive", event="file.created",
    label="Google Drive", validate=_drive_poll_validate,
    fetch=_drive_poll_fetch, view=_drive_poll_view))


# --- poll sources: "Updated File" / "Deleted File" via the changes feed -------
#
# Zapier's Updated-File and Deleted-File triggers have no files.list answer —
# createdTime keys the new-file trigger, but an edit only bumps modifiedTime
# (unorderable against a cursor without snapshotting everything), and a
# delete leaves no trace at all. Drive's change feed (``changes.list``) is
# the missing primitive: an opaque page-token cursor (the same
# ``next_cursor`` machinery the seen-set already dedupes), one change per
# edit/remove with its timestamp and — for edits — the file metadata.
#
# Two sources share the feed, one per event: ``google-drive.updates``
# publishes ``file.updated`` for edits inside ``folder_id`` (optional —
# Drive-wide when absent), ``google-drive.deletions`` publishes
# ``file.deleted`` for removals, which are always Drive-wide (a deletion
# change carries a fileId only, no metadata to match a folder against).
# Each source keeps its own poll trigger, cursor and seen-set, so the two
# walk the feed independently.

GOOGLE_DRIVE_CHANGES_URL = "https://www.googleapis.com/drive/v3/changes"
GOOGLE_DRIVE_START_PAGE_TOKEN_URL = GOOGLE_DRIVE_CHANGES_URL + "/startPageToken"

DRIVE_CHANGES_FIELDS = ("nextPageToken,newStartPageToken,"
                        "changes(fileId,removed,time,file(id,name,mimeType,parents,"
                        "createdTime,modifiedTime,size,webViewLink))")


def _drive_changes_validate(body):
    """Save-time fetch spec shared by the changes sources: ``connection_id``
    (required — the fetch refreshes its OAuth token) and the optional
    ``folder_id`` the updates source filters on, plus the fetch defaults
    every stored drive poll carries."""
    from src.dapier.triggers.email_triggers import TriggerError

    body = body if isinstance(body, dict) else {}
    if not str(body.get("connection_id") or "").strip():
        raise TriggerError(
            "connection_id is required: pick the Google connection to poll as")
    return {
        "folder_id": str(body.get("folder_id") or "").strip(),
        "cursor_mode": "next_cursor",
        "id_path": "id",
        "url": "",
    }


def _change_item(change):
    """One change feed entry flattened to the event data shape: identity,
    the moment the change happened, and — for edits — the file metadata."""
    file = change.get("file") if isinstance(change.get("file"), dict) else {}
    return {
        "id": str(change.get("fileId") or ""),
        "file_id": str(change.get("fileId") or ""),
        "name": file.get("name"),
        "mime_type": file.get("mimeType"),
        "parents": file.get("parents") or [],
        "change_time": str(change.get("time") or ""),
        "removed": bool(change.get("removed")),
    }


def _drive_changes_fetch(item, cursor, *, removed, name, transport=None):
    """One changes page as ``(items, next_cursor)`` for ``triggers.poll_sources``.

    With no stored cursor — the first fire after enabling — the fetch only
    seeds the cursor at the feed's ``startPageToken`` and emits nothing:
    the Drive's existing state is history, not news. With a cursor, the page
    after it is filtered to the source's half of the feed (removals for
    ``deletions``; edits, folder-scoped when ``folder_id`` is stored, for
    ``updates``). The parked cursor is the page's continuation token —
    ``nextPageToken`` while the feed has more pages, ``newStartPageToken``
    once it drains — which ``fire`` parks only once the page was processed
    down to nothing; the seen-set dedupes file ids across refetched pages
    (a file edited twice on one page fires once). Raises ``RuntimeError``
    on a failed fetch, like every poll source.
    """
    from src.dapier.triggers import poll_triggers

    if not str(item.get("connection_id") or "").strip():
        raise RuntimeError(f"poll source '{name}' needs connection_id: "
                           "the Google connection to poll as")
    token = poll_triggers._bearer_token(item["connection_id"])
    try:
        if cursor is None:
            data = provider._request("GET", GOOGLE_DRIVE_START_PAGE_TOKEN_URL,
                                     token, None, transport=transport)
            return [], str(data.get("startPageToken") or "")
        params = {
            "pageToken": str(cursor),
            "pageSize": DRIVE_POLL_PAGE_SIZE,
            "includeRemoved": "true",
            "restrictToMyDrive": "false",
            "supportsAllDrives": "true",
            "fields": DRIVE_CHANGES_FIELDS,
        }
        data = provider._request(
            "GET", GOOGLE_DRIVE_CHANGES_URL + "?" + urllib.parse.urlencode(params),
            token, None, transport=transport)
    except provider.DiscoveryError as exc:
        raise RuntimeError(f"drive poll failed: {exc}") from None
    changes = [_change_item(change) for change in data.get("changes") or []
               if isinstance(change, dict) and change.get("fileId")]
    if removed:
        fresh = [change for change in changes if change["removed"]]
    else:
        folder_id = str(item.get("folder_id") or "").strip()
        fresh = [change for change in changes
                 if not change["removed"] and change["name"] is not None
                 and (not folder_id or folder_id in change["parents"])]
    next_token = str(data.get("nextPageToken")
                     or data.get("newStartPageToken") or "")
    if not next_token:
        raise RuntimeError("drive poll failed: the changes page carried no "
                           "continuation token")
    return fresh, next_token


def _drive_updates_fetch(item, cursor=None, *, transport=None):
    return _drive_changes_fetch(item, cursor, removed=False,
                                name="google-drive.updates", transport=transport)


def _drive_deletions_fetch(item, cursor=None, *, transport=None):
    return _drive_changes_fetch(item, cursor, removed=True,
                                name="google-drive.deletions", transport=transport)


def _drive_changes_view(item):
    """The provider params ``public_view`` shows beside ``source``."""
    return {"folder_id": item.get("folder_id") or None}


register_source(PollSource(
    name="google-drive.updates", connector="google-drive", event="file.updated",
    label="Google Drive updates", validate=_drive_changes_validate,
    fetch=_drive_updates_fetch, view=_drive_changes_view))

register_source(PollSource(
    name="google-drive.deletions", connector="google-drive", event="file.deleted",
    label="Google Drive deletions", validate=_drive_changes_validate,
    fetch=_drive_deletions_fetch, view=_drive_changes_view))


def _stored_drive_poll(name):
    """The stored poll trigger named by ``event`` when it watches a Google
    Drive source (files/updates/deletions), or None. A missing selector,
    unconfigured poll triggers, an unknown name and a non-drive source (the
    generic poll connector owns those) fold together: the caller only
    distinguishes live-vs-fallback, so any storage hiccup folds too —
    sampling never raises for want of infrastructure (see
    docs/connector-coverage-audit.md)."""
    from src.dapier.triggers import poll_triggers

    if not name:
        return None
    try:
        item = poll_triggers.get_item(name)
    except Exception:
        return None
    if not item or not str(item.get("source") or "").startswith("google-drive."):
        return None
    return item


# The event each drive poll source publishes — the live sample's ask maps
# the stored poll onto it, and the synthetic fallback picks its payload by it.
_DRIVE_SOURCE_EVENTS = {
    "google-drive.files": "file.created",
    "google-drive.updates": "file.updated",
    "google-drive.deletions": "file.deleted",
}


_DRIVE_SYNTHETIC_FILE = {
    "id": "1a2B3c4D5e6F7g8H9i0J",
    "name": "invoices-2026-09.pdf",
    "mimeType": "application/pdf",
    "createdTime": "2026-09-28T09:14:03.000Z",
    "modifiedTime": "2026-09-28T09:14:03.000Z",
    "size": "81244",
    "webViewLink": "https://drive.google.com/file/d/1a2B3c4D5e6F7g8H9i0J/view",
}

# The changes sources' documented samples: the flattened _change_item shape.
_DRIVE_SYNTHETIC_CHANGES = {
    "file.updated": {
        "id": "1a2B3c4D5e6F7g8H9i0J",
        "file_id": "1a2B3c4D5e6F7g8H9i0J",
        "name": "invoices-2026-09.pdf",
        "mime_type": "application/pdf",
        "parents": ["1F0Ld3rF0Ld3r"],
        "change_time": "2026-09-28T09:31:44.000Z",
        "removed": False,
    },
    "file.deleted": {
        "id": "1a2B3c4D5e6F7g8H9i0J",
        "file_id": "1a2B3c4D5e6F7g8H9i0J",
        "change_time": "2026-09-28T09:40:00.000Z",
        "removed": True,
    },
}


def _fetch_drive_sample(event=None, connection_id=None, limit=DEFAULT_LIMIT):
    """The Drive chip's sample pull: the newest item a stored drive poll
    watches right now (``source: "live"``), else the newest recorded
    google-drive run carrying the asked event (``"history"``), else a
    documented example (``"synthetic"``).

    ``event`` names the stored poll trigger; any ``google-drive.*`` source
    qualifies. The live pull runs the poll's own fetch once — the files
    source against the epoch watermark, the changes sources against the
    trigger's parked cursor (read, never advanced) — and wraps the newest
    item in the envelope a real fire would publish. A live fetch that
    cannot run — no connection, an unreachable Drive, a trigger that has
    not fired yet — falls through to the recorded/documented sample instead
    of failing: a sample pull shows the payload shape, it never raises. The
    fallbacks key on the poll's event (a ``file.deleted`` ask is never
    answered with a ``file.created`` run or example); a bare chip ask
    without a poll stays on the classic new-file sample.
    """
    from src.dapier.triggers import poll_triggers

    name = str(event or "").strip().lower()
    item = _stored_drive_poll(name)
    wanted_event = _DRIVE_SOURCE_EVENTS.get(str((item or {}).get("source") or ""))
    if wanted_event is None and "." in name:
        wanted_event = name  # a dotted ask names an event, not a poll
    if item is not None:
        source = str(item.get("source") or "")
        try:
            if source == "google-drive.files":
                items, _next_cursor = _drive_poll_fetch(item, DRIVE_EPOCH_CURSOR)
            else:
                try:
                    seed = poll_triggers.get_cursor(item.get("poll_id"))
                except Exception:
                    seed = None
                items, _next_cursor = _drive_changes_fetch(
                    item, seed, removed=source == "google-drive.deletions",
                    name=source)
            envelope = (poll_triggers.event_for(item, items[-1])
                        if items else None)
        except Exception:
            envelope = None
        if envelope is not None:
            return {
                "sample": trigger_discovery.as_sample(envelope),
                "source": "live",
                "connection_id": item.get("connection_id") or None,
            }
    found = trigger_discovery.history_sample("google-drive", event=wanted_event)
    if found is not None:
        return {"sample": found, "source": "history", "connection_id": connection_id}
    if wanted_event in _DRIVE_SYNTHETIC_CHANGES:
        synthetic_event, synthetic_data = wanted_event, _DRIVE_SYNTHETIC_CHANGES[wanted_event]
    else:
        synthetic_event, synthetic_data = "file.created", dict(_DRIVE_SYNTHETIC_FILE)
    return {
        "sample": trigger_discovery.synthetic_sample(
            "google-drive", synthetic_event, dict(synthetic_data)),
        "source": "synthetic",
        "connection_id": connection_id,
    }


register_trigger_discovery(TriggerDiscovery(
    connector="google-drive", label="Google Drive", kind="sample", resource="",
    fetch=_fetch_drive_sample))
