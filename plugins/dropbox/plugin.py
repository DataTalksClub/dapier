"""Dropbox connector: upload, read, move, copy, and delete files through a
connection, plus folder/file discovery, the account health check, and the
"new file in folder" poll source behind the trigger chip's sample pull."""
import json

from plugins.dropbox.runners.dropbox import (
    _dropbox_rpc,
    run_dropbox_copy,
    run_dropbox_create_folder,
    run_dropbox_delete,
    run_dropbox_find,
    run_dropbox_get_temp_link,
    run_dropbox_move,
    run_dropbox_read_file,
    run_dropbox_upload,
)
from src.dapier.connections import discovery as provider
from src.dapier.connections import tokens
from src.dapier.connectors.registry import (
    Action,
    Connector,
    ConnectionTest,
    Discovery,
    connector,
    register,
    register_connection_test,
    register_discovery,
)
from src.dapier.triggers.poll_sources import PollSource, register_source

connector(Connector(name="dropbox", label="Dropbox",
                    events=("file.created", "file.updated", "file.deleted"),
                    icon="dropbox"))

DROPBOX_LIST_FOLDER_URL = "https://api.dropboxapi.com/2/files/list_folder"
DROPBOX_LIST_CONTINUE_URL = "https://api.dropboxapi.com/2/files/list_folder/continue"
_PAGE_LIMIT = 300
_MAX_PAGES = 5

register(Action(
    type="dropbox_upload",
    label="Dropbox upload",
    icon="dropbox",
    run=lambda action, event, workflow_id, steps=None, transport=None:
        run_dropbox_upload(action, event, transport=transport, steps=steps),
    required=frozenset({"connection_id", "folder"}),
    optional=frozenset({"source", "filename", "attachment_selection", "exclude_content_types", "overwrite", "autorename", "strict_conflict", "skip_existing"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "dropbox", "required": True},
        {"key": "source", "label": "Source", "type": "select", "options": ["attachment", "output"], "default": "attachment"},
        {"key": "folder", "label": "Folder", "placeholder": "/Invoices", "required": True,
         "discover": {"resource": "dropbox.folders"}},
        {"key": "filename", "label": "Filename override"},
        {"key": "attachment_selection", "label": "Attachment selection", "type": "select", "options": ["all", "single", "first"], "default": "all"},
        {"key": "exclude_content_types", "label": "Exclude content types", "placeholder": "text/html, text/plain"},
        {"key": "overwrite", "label": "Overwrite existing file", "type": "boolean", "default": "false"},
        {"key": "strict_conflict", "label": "Reject identical file conflicts", "type": "boolean", "default": "false"},
        {"key": "autorename", "label": "Autorename on conflict", "type": "boolean", "default": "true"},
        {"key": "skip_existing", "label": "Skip when the path already exists", "type": "boolean", "default": "false"},
    ),
))

register(Action(
    type="dropbox_delete",
    label="Dropbox delete",
    icon="dropbox",
    run=lambda action, event, workflow_id, steps=None, transport=None:
        run_dropbox_delete(action, event, transport=transport),
    required=frozenset({"connection_id"}),
    optional=frozenset({"path"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "dropbox", "required": True},
        {"key": "path", "label": "Path", "placeholder": "defaults to the event's file path",
         "discover": {"resource": "dropbox.files"}},
    ),
))


register(Action(
    type="dropbox_find",
    label="Dropbox: find file or folder",
    icon="dropbox",
    run=lambda action, event, workflow_id, steps=None, transport=None:
        run_dropbox_find(action, event, transport=transport, steps=steps),
    required=frozenset({"connection_id", "query"}),
    optional=frozenset({"kind", "path", "create_if_missing"}),
    description=("Searches the connection's Dropbox for one file or folder by name "
                 "(files/search, filename matches only, active entries). Output: "
                 "{found: true, item: {id, name, path, tag, size, modified}}; a miss is "
                 "{found: false, item: null}. With create_if_missing a folder find "
                 "creates the missing folder and reports created: true."),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "dropbox", "required": True},
        {"key": "query", "label": "Name to find", "placeholder": "{filename}", "required": True,
         "discover": {"resource": "dropbox.search", "params": {"query": "query"}}},
        {"key": "kind", "label": "Kind", "type": "select",
         "options": ["any", "file", "folder"], "default": "any"},
        {"key": "path", "label": "Folder to search", "placeholder": "defaults to the connection's root",
         "discover": {"resource": "dropbox.folders"}},
        {"key": "create_if_missing", "label": "Create folder if missing", "type": "boolean"},
    ),
))


register(Action(
    type="dropbox_read_file",
    label="Dropbox: read file",
    icon="dropbox",
    run=lambda action, event, workflow_id, steps=None, transport=None:
        run_dropbox_read_file(action, event, transport=transport, steps=steps),
    required=frozenset({"connection_id"}),
    optional=frozenset({"path"}),
    description=("Downloads one file from the connection's Dropbox (files/download) and "
                 "stages the bytes for the steps that follow. Output: {filename, size, "
                 "content_type, bucket, key, path}; the path defaults to the event's file "
                 "path. Pair with Amazon S3's source_s3 (bucket/key take templates) to "
                 "move the bytes into a bucket."),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "dropbox", "required": True},
        {"key": "path", "label": "Path", "placeholder": "defaults to the event's file path",
         "discover": {"resource": "dropbox.files"}},
    ),
))


register(Action(
    type="dropbox_get_temp_link",
    label="Dropbox: get temporary link",
    icon="dropbox",
    run=lambda action, event, workflow_id, steps=None, transport=None:
        run_dropbox_get_temp_link(action, event, transport=transport, steps=steps),
    required=frozenset({"connection_id"}),
    optional=frozenset({"path"}),
    description=("Mints a short-lived direct download link for one file "
                 "(files/get_temporary_link, valid for a few hours). Output: "
                 "{link, item: {id, name, path, tag, size, modified}}; chain it "
                 "before Amazon S3's upload step and pass "
                 "{steps.<id>.output.link} as that step's source_url to pull "
        "Dropbox bytes into the pipeline. The path defaults to the "
        "event's file path."),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "dropbox", "required": True},
        {"key": "path", "label": "Path", "placeholder": "defaults to the event's file path",
         "discover": {"resource": "dropbox.files"}},
    ),
))


register(Action(
    type="dropbox_create_folder",
    label="Dropbox: create folder",
    icon="dropbox",
    run=lambda action, event, workflow_id, steps=None, transport=None:
        run_dropbox_create_folder(action, event, transport=transport, steps=steps),
    required=frozenset({"connection_id", "path"}),
    description=("Creates one folder (files/create_folder_v2). The path is the "
                 "full destination including the new folder's name and renders "
                 "from the event; an existing folder is an error — chain "
                 "dropbox_find with create_if_missing when find-or-create is "
                 "wanted. Output: {folder, item: {id, name, path, tag}}."),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "dropbox", "required": True},
        {"key": "path", "label": "Folder path", "placeholder": "/Invoices/{month}", "required": True},
    ),
))


register(Action(
    type="dropbox_move",
    label="Dropbox: move file",
    icon="dropbox",
    run=lambda action, event, workflow_id, steps=None, transport=None:
        run_dropbox_move(action, event, transport=transport, steps=steps),
    required=frozenset({"connection_id", "from_path", "to_path"}),
    optional=frozenset({"autorename"}),
    description=("Moves one file or folder (files/move_v2, Zapier's Move File — "
                 "also its Rename File: a move within the same folder under a "
                 "new name renames). Both paths render from the event; "
                 "autorename appends a suffix instead of erroring when the "
                 "destination exists. Output: {moved, item}."),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "dropbox", "required": True},
        {"key": "from_path", "label": "From path", "placeholder": "{path} from a dropbox trigger",
         "required": True, "discover": {"resource": "dropbox.files"}},
        {"key": "to_path", "label": "To path", "placeholder": "/Archive/{filename}", "required": True},
        {"key": "autorename", "label": "Autorename on conflict", "type": "boolean"},
    ),
))


register(Action(
    type="dropbox_copy",
    label="Dropbox: copy file",
    icon="dropbox",
    run=lambda action, event, workflow_id, steps=None, transport=None:
        run_dropbox_copy(action, event, transport=transport, steps=steps),
    required=frozenset({"connection_id", "from_path", "to_path"}),
    optional=frozenset({"autorename"}),
    description=("Copies one file or folder to a new path (files/copy_v2, "
                 "Zapier's Copy File). Both paths render from the event; "
                 "autorename appends a suffix instead of erroring when the "
                 "destination exists. Output: {copied, item}."),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "dropbox", "required": True},
        {"key": "from_path", "label": "From path", "required": True,
         "discover": {"resource": "dropbox.files"}},
        {"key": "to_path", "label": "To path", "placeholder": "/Archive/{filename}", "required": True},
        {"key": "autorename", "label": "Autorename on conflict", "type": "boolean"},
    ),
))


def _run_folders(connection, params, *, transport=None):
    """Folders under the connection's Dropbox root (or ``path``)."""
    return _run_entries(connection, params, tag="folder", transport=transport)


def _run_files(connection, params, *, transport=None):
    """Files under the connection's Dropbox root (or ``path``)."""
    return _run_entries(connection, params, tag="file", transport=transport)


def _run_entries(connection, params, *, tag, transport=None):
    """One listing of one entry kind, following the cursor at most _MAX_PAGES.

    Entries without a usable name fall back to their path instead of failing
    the whole listing; items carry the ``path`` the actions actually need
    (upload target folder, delete target file), not just the Dropbox id.
    """
    token, _info = tokens.get_access_token(connection, transport=transport)
    path = params.get("path")
    if path is None or str(path).strip() == "":
        path = connection.get("root_path") or ""
    payload = {"path": str(path), "recursive": False,
               "include_deleted": False, "limit": _PAGE_LIMIT}
    items = []
    for page in range(_MAX_PAGES):
        url = DROPBOX_LIST_FOLDER_URL if page == 0 else DROPBOX_LIST_CONTINUE_URL
        raw = _dropbox_rpc(
            url, token, payload,
            transport=transport,
            unreachable="dropbox listing unreachable",
        )
        try:
            data = json.loads(raw.decode() or "{}")
        except (ValueError, UnicodeDecodeError):
            break
        items.extend(_entry_item(entry, tag) for entry in data.get("entries") or []
                     if isinstance(entry, dict) and entry.get("id")
                     and entry.get(".tag") == tag)
        if not data.get("has_more") or not data.get("cursor"):
            break
        payload = {"cursor": data["cursor"]}
    return items


def _entry_item(entry, tag):
    name = str(entry.get("name") or entry.get("path_display") or entry["id"])
    item = {
        "id": str(entry["id"]),
        "name": name,
        "path": str(entry.get("path_display") or entry.get("path_lower") or name),
    }
    if tag == "file":
        item["size"] = entry.get("size")
        item["modified"] = str(entry.get("server_modified") or "")
    return item


register_discovery(Discovery(
    name="folders",
    connector="dropbox",
    label="Folders",
    description="Folders under the connection's root",
    params=({"key": "path", "label": "Folder path", "type": "text",
             "help": "Defaults to the connection's configured root"},),
    run=_run_folders,
))

register_discovery(Discovery(
    name="files",
    connector="dropbox",
    label="Files",
    description="Files under the connection's root, with their paths",
    params=({"key": "path", "label": "Folder path", "type": "text",
             "help": "Defaults to the connection's configured root"},),
    run=_run_files,
))


def _run_search(connection, params, *, transport=None):
    """files/search_v2 matches for one query (shared provider layer)."""
    return provider.discover(connection, "search", params, transport=transport)


register_discovery(Discovery(
    name="search",
    connector="dropbox",
    label="Search matches",
    description="Files and folders matching a name query",
    params=({"key": "query", "label": "Search query", "type": "text",
             "required": True},),
    run=_run_search,
))


def _run_test(connection, *, transport=None):
    """users/get_current_account, via the shared token lifecycle."""
    try:
        _token, info = tokens.get_access_token(connection, transport=transport)
    except Exception as exc:
        return {"ok": False, "detail": str(exc) or type(exc).__name__}
    return {
        "ok": True,
        "detail": f"Dropbox account verified for {info.get('account_title') or info.get('provider_account_id')}",
        "identity": {
            "account_id": info.get("provider_account_id"),
            "title": info.get("account_title"),
        },
    }


register_connection_test(ConnectionTest(connector="dropbox", run=_run_test))


# --- poll source: "New File in Folder" on the poll-trigger schedule ---------
#
# file.created fires from Dropbox's webhooks (triggers.intake.dropbox_
# resolver) — but those need a registered Dropbox app whose webhook endpoint
# is verified. The ``dropbox.files`` source is the no-app path: a stored
# poll trigger lists one folder through the module's own listing
# (``_run_entries``: files/list_folder + continue) on the schedule machinery
# (triggers.poll_sources) and publishes the same ``dropbox``/
# ``file.created`` events, so a workflow matches the same chip either way.
# The first fire seeds the cursor without emitting: enabling a trigger must
# not fire the folder's whole history.


def _dropbox_poll_validate(body):
    """Save-time fetch spec: the folder ``path`` to watch (required —
    Zapier's New File in Folder is folder-scoped, and the value is the same
    path the actions and folder pickers speak), the ``connection_id`` of the
    Dropbox connection to poll as (required — the fetch refreshes its OAuth
    token), plus the fetch defaults every stored dropbox poll carries."""
    from src.dapier.triggers.email_triggers import TriggerError

    body = body if isinstance(body, dict) else {}
    path = str(body.get("path") or "").strip()
    if not path:
        raise TriggerError("path is required: name the folder to watch")
    if not str(body.get("connection_id") or "").strip():
        raise TriggerError(
            "connection_id is required: pick the Dropbox connection to poll as")
    return {
        "path": path,
        "cursor_mode": "next_cursor",
        "id_path": "id",
        "url": "",
    }


def _dropbox_poll_files(item, *, transport=None):
    """The folder's file entries through the module's own listing, on the
    stored connection's (refreshed) OAuth token — the exact call the folder
    pickers serve, so the poll sees what the operator browsed."""
    from src.dapier.engine.actions import base

    connection = base._connected_connection(item["connection_id"])
    return _run_entries(connection, {"path": str(item.get("path") or "").strip()},
                        tag="file", transport=transport)


def _dropbox_poll_fetch(item, cursor=None, *, transport=None):
    """One poll page as ``(items, next_cursor)`` for ``triggers.poll_sources``.

    Lists the stored folder through ``_run_entries`` with the connection
    record behind the stored ``connection_id``, so the OAuth token is
    refreshed exactly like the actions' calls. Items are the listing's file
    entries (id, name, path, size, modified — ``modified`` is Dropbox's
    ``server_modified``).

    The cursor is the newest fired ``server_modified`` (ISO Zulu, so text
    comparison orders it; the file id breaks ties). With no stored cursor —
    the first fire after enabling — the fetch only seeds the watermark at
    the folder's newest file and emits nothing: the folder's existing files
    are history, not news. With a cursor, only files modified strictly after
    it fire, oldest first, and the parked cursor is the newest fired
    ``modified`` (the incoming one when nothing qualifies; ``fire`` parks it
    only once the page drains). Keying on ``server_modified`` means an edit
    re-lists its file at the watermark's edge — the seen store then dedupes
    the re-listed id. Raises ``RuntimeError`` on a failed fetch, like every
    poll source.
    """
    if not str(item.get("connection_id") or "").strip():
        raise RuntimeError("poll source 'dropbox.files' needs connection_id: "
                           "the Dropbox connection to poll as")
    if not str(item.get("path") or "").strip():
        raise RuntimeError("poll source 'dropbox.files' needs a stored path")
    try:
        files = _dropbox_poll_files(item, transport=transport)
    except (ValueError, tokens.TokenError) as exc:
        raise RuntimeError(f"dropbox poll failed: {exc}") from None
    if cursor is None:
        # First fire: seed the watermark at the folder's newest file (None
        # on an empty folder) without emitting anything.
        return [], max((str(file.get("modified") or "") for file in files),
                       default="0000-01-01T00:00:00Z")
    watermark = str(cursor)
    fresh = sorted(
        (file for file in files if str(file.get("modified") or "") > watermark),
        key=lambda file: (str(file.get("modified") or ""), str(file.get("id") or "")))
    return fresh, (str(fresh[-1]["modified"]) if fresh else watermark)


def _dropbox_poll_view(item):
    """The provider params ``public_view`` shows beside ``source``."""
    return {"path": item.get("path")}


register_source(PollSource(
    name="dropbox.files", connector="dropbox", event="file.created",
    label="Dropbox", validate=_dropbox_poll_validate,
    fetch=_dropbox_poll_fetch, view=_dropbox_poll_view))


def _stored_dropbox_poll(name):
    """The stored poll trigger named by ``event`` when it watches Dropbox,
    or None. Poll ids cannot contain dots and event names do, so a per-event
    ask never matches a poll; a missing selector, unconfigured poll
    triggers, an unknown name and a non-dropbox source (the generic poll
    connector owns those) fold together: the caller only distinguishes
    live-vs-fallback, so any storage hiccup folds too — sampling never
    raises for want of infrastructure (see docs/connector-coverage-audit.md)."""
    from src.dapier.triggers import poll_triggers

    name = str(name or "").strip().lower()
    if not name or "." in name:
        return None
    try:
        item = poll_triggers.get_item(name)
    except Exception:
        return None
    if not item or str(item.get("source") or "") != "dropbox.files":
        return None
    return item


# Older than any Dropbox server_modified: the chip's live sample pulls
# against this watermark, so the folder's newest file answers before the
# trigger is even past its first (seeding) fire.
_DROPBOX_EPOCH_CURSOR = "0000-01-01T00:00:00Z"


# --- trigger discovery: a per-event file sample — recorded history when its
# envelope carries the asked event, else a realistic example

from src.dapier.connectors import trigger_discovery
from src.dapier.connectors.trigger_discovery import (
    DEFAULT_LIMIT,
    TriggerDiscovery,
    options_from_registry,
    per_event_sample_fetch,
    register_trigger_discovery,
)

# The file-event shapes the dropbox resolver publishes (see
# triggers.intake.dropbox_resolver.process_entry): a new or changed file
# carries the full file state, a deletion only the path that disappeared.
_DROPBOX_SYNTHETIC_DATA = {
    "account_id": "dbid:discover-example",
    "path": "/Invoices/invoice-4137.pdf",
    "path_lower": "/invoices/invoice-4137.pdf",
    "file_id": "id:discover-example",
    "rev": "discover1",
    "content_hash": None,
    "size": 51200,
}

# file.updated: same shape, a changed rev and the size that change implies.
_DROPBOX_UPDATED_DATA = {
    "account_id": "dbid:discover-example",
    "path": "/Invoices/invoice-4137.pdf",
    "path_lower": "/invoices/invoice-4137.pdf",
    "file_id": "id:discover-example",
    "rev": "discover2",
    "content_hash": None,
    "size": 76800,
}

# file.deleted: only the identity of the path that disappeared.
_DROPBOX_DELETED_DATA = {
    "account_id": "dbid:discover-example",
    "path": "/Invoices/invoice-4137.pdf",
    "path_lower": "/invoices/invoice-4137.pdf",
}


_PER_EVENT_DROPBOX_SAMPLE = per_event_sample_fetch("dropbox", "file.created", {
    "file.created": _DROPBOX_SYNTHETIC_DATA,
    "file.updated": _DROPBOX_UPDATED_DATA,
    "file.deleted": _DROPBOX_DELETED_DATA,
})


def _fetch_dropbox_sample(event=None, connection_id=None, limit=DEFAULT_LIMIT):
    """The Dropbox chip's sample pull. An ``event`` naming a stored
    dropbox.files poll pulls live through the poll's own fetch against the
    epoch watermark — no stored cursor is read or advanced — and wraps the
    folder's newest file in the envelope a real fire would publish. Anything
    else — a per-event ask, no stored poll yet, or a live fetch that cannot
    run (no connection, an unreachable Dropbox) — falls through to the
    per-event chain: the newest recorded dropbox run carrying the asked
    event, else the documented webhook example. A sample pull shows the
    payload shape, it never raises."""
    from src.dapier.triggers import poll_triggers

    item = _stored_dropbox_poll(event)
    if item is not None:
        try:
            files, _next_cursor = _dropbox_poll_fetch(item, _DROPBOX_EPOCH_CURSOR)
            envelope = (poll_triggers.event_for(item, files[-1])
                        if files else None)
        except Exception:
            envelope = None
        if envelope is not None:
            return {
                "sample": trigger_discovery.as_sample(envelope),
                "source": "live",
                "connection_id": item.get("connection_id") or None,
            }
    return _PER_EVENT_DROPBOX_SAMPLE(event=event, connection_id=connection_id,
                                     limit=limit)


register_trigger_discovery(TriggerDiscovery(
    connector="dropbox", label="Dropbox", kind="sample", resource="",
    fetch=_fetch_dropbox_sample))


def _fetch_folder_options(event=None, connection_id=None, limit=DEFAULT_LIMIT):
    """Folder options for the upload action's folder field (via the registry).

    The value is the folder's path — the field stores paths — labeled with
    the folder name.
    """
    return options_from_registry(
        "dropbox.folders", connection_id, limit, provider="dropbox",
        option_of=lambda item: {
            "value": item.get("path") or item.get("name") or item.get("id"),
            "label": item.get("name") or item.get("path") or item.get("id"),
        })


register_trigger_discovery(TriggerDiscovery(
    connector="dropbox", label="Dropbox", kind="options", resource="dropbox.folders",
    fetch=_fetch_folder_options))


def _path_option(item):
    """``{value: path, label: name}`` — the path fields store paths."""
    return {"value": item.get("path") or item.get("name") or item.get("id"),
            "label": item.get("name") or item.get("path") or item.get("id")}


def _fetch_file_options(event=None, connection_id=None, limit=DEFAULT_LIMIT):
    """File options for the read/upload actions' file fields (via the
    registry). The optional folder path rides in ``event``; without one the
    listing defaults to the connection's root, like the folders listing."""
    path = str(event or "").strip()
    return trigger_discovery.options_from_registry(
        "dropbox.files", connection_id, limit, provider="dropbox",
        params={"path": path} if path else {},
        option_of=_path_option)


register_trigger_discovery(TriggerDiscovery(
    connector="dropbox", label="Dropbox", kind="options", resource="dropbox.files",
    fetch=_fetch_file_options))


def _fetch_search_options(event=None, connection_id=None, limit=DEFAULT_LIMIT):
    """Search-match options for one query (via the registry); the query
    rides in ``event`` (trigger_discovery.listing_params)."""
    return trigger_discovery.options_from_registry(
        "dropbox.search", connection_id, limit, provider="dropbox",
        params=trigger_discovery.listing_params(event, ("query",)),
        option_of=_path_option)


register_trigger_discovery(TriggerDiscovery(
    connector="dropbox", label="Dropbox", kind="options", resource="dropbox.search",
    fetch=_fetch_search_options))
