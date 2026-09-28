"""Google Drive connector: file discovery and the find-file action over a
Google connection.

``drive_find_file`` is Drive's workflow action: it searches the connection's
Drive for a file by name and hands its id to the next action in the chain.
Downloads ride the S3 upload's ``source_url`` with the connection's bearer
token (see ``engine.actions.s3``). The served resources are the same Drive
``files`` listing (items carry ``mimeType``, so folder pickers filter
client-side) and its folder-only variant; discovery delegates to the shared
provider layer like every other connection.
"""
from ..engine.actions.drive import run_drive_find_file
from ..connections import discovery as provider
from .registry import Action, Discovery, register, register_discovery


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


# --- trigger discovery: file options for the find action's name field ----------

from . import trigger_discovery  # noqa: E402
from .trigger_discovery import (  # noqa: E402
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
