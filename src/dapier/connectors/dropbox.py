"""Dropbox connector: upload and delete files through a connection, plus
folder/file discovery and the account health check."""
import json

from ..connections import discovery as provider
from ..connections import tokens
from ..engine.actions.dropbox import (
    _dropbox_rpc,
    run_dropbox_delete,
    run_dropbox_find,
    run_dropbox_upload,
)
from .registry import (
    Action,
    ConnectionTest,
    Discovery,
    register,
    register_connection_test,
    register_discovery,
)

DROPBOX_LIST_FOLDER_URL = "https://api.dropboxapi.com/2/files/list_folder"
DROPBOX_LIST_CONTINUE_URL = "https://api.dropboxapi.com/2/files/list_folder/continue"
_PAGE_LIMIT = 300
_MAX_PAGES = 5

register(Action(
    type="dropbox_upload",
    label="Dropbox upload",
    icon="dropbox",
    run=lambda action, event, workflow_id, steps=None: run_dropbox_upload(action, event),
    required=frozenset({"connection_id", "folder"}),
    optional=frozenset({"source", "filename"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "dropbox", "required": True},
        {"key": "source", "label": "Source", "type": "select", "options": ["attachment", "output"], "default": "attachment"},
        {"key": "folder", "label": "Folder", "placeholder": "/Invoices", "required": True,
         "discover": {"resource": "dropbox.folders"}},
        {"key": "filename", "label": "Filename override"},
    ),
))

register(Action(
    type="dropbox_delete",
    label="Dropbox delete",
    icon="dropbox",
    run=lambda action, event, workflow_id, steps=None: run_dropbox_delete(action, event),
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
    run=lambda action, event, workflow_id, steps=None: run_dropbox_find(action, event, steps=steps),
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


# --- trigger discovery: a per-event file sample — recorded history when its
# envelope carries the asked event, else a realistic example

from . import trigger_discovery
from .trigger_discovery import (
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


register_trigger_discovery(TriggerDiscovery(
    connector="dropbox", label="Dropbox", kind="sample", resource="",
    fetch=per_event_sample_fetch("dropbox", "file.created", {
        "file.created": _DROPBOX_SYNTHETIC_DATA,
        "file.updated": _DROPBOX_UPDATED_DATA,
        "file.deleted": _DROPBOX_DELETED_DATA,
    })))


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
