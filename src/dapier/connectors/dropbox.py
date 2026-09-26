"""Dropbox connector: upload and delete files through a connection."""
from ..engine.actions.dropbox import run_dropbox_delete, run_dropbox_upload
from .registry import Action, register

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
        {"key": "folder", "label": "Folder", "placeholder": "/Invoices", "required": True},
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
        {"key": "path", "label": "Path", "placeholder": "defaults to the event's file path"},
    ),
))
