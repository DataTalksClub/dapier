"""DataOps connector: forward events to a DataOps intake."""
from ..engine.actions.dataops import run_dataops
from .registry import Action, register

register(Action(
    type="dataops",
    label="DataOps intake",
    icon="database-zap",
    run=lambda action, event, workflow_id, steps=None: run_dataops(action, event, steps=steps),
    required=frozenset({"auth_secret_id"}),
    optional=frozenset({"url", "url_env", "timeout_seconds", "connection_id", "filename", "path"}),
    fields=(
        {"key": "auth_secret_id", "label": "Auth secret ID", "placeholder": "dapier/dataops", "required": True},
        {"key": "url_env", "label": "URL env var", "placeholder": "DATAOPS_INTAKE_URL"},
        {"key": "url", "label": "URL (overrides env)", "type": "url"},
        {"key": "connection_id", "label": "Dropbox connection ID", "placeholder": "dropbox — for file-event intakes"},
        {"key": "filename", "label": "Filename override"},
        {"key": "path", "label": "Dropbox file path", "placeholder": "{steps.move.output.item.path}"},
        {"key": "timeout_seconds", "label": "Timeout (s)", "type": "number"},
    ),
))


# --- trigger discovery: the newest recorded intake, else a realistic sample ----

from . import trigger_discovery
from .trigger_discovery import (
    DEFAULT_LIMIT,
    TriggerDiscovery,
    history_or_synthetic_fetch,
    register_trigger_discovery,
)

# DataOps traffic normally flows OUT of dapier (the action forwards into the
# intake), so recorded runs carry the connector only when an operator replays
# one. The synthetic sample mirrors the intake document contract the action
# builds (engine.actions.dataops._intake_body): a processed Dropbox file.
_DATAOPS_SYNTHETIC_DATA = {
    "version": "2026-07-01",
    "messageId": "discover-00000000",
    "recipientRoute": "dropbox-upload",
    "from": "dropbox",
    "subject": "invoice-4137.pdf",
    "receivedAt": "2026-09-27T10:15:00Z",
    "documents": [{
        "kind": "dropbox-file",
        "storageUri": "s3://dapier-dataops-documents/transfer/discover/invoice-4137.pdf",
        "filename": "invoice-4137.pdf",
        "contentType": "application/pdf",
        "sizeBytes": 51200,
        "checksum": "sha256:" + "0" * 64,
    }],
}


register_trigger_discovery(TriggerDiscovery(
    connector="dataops", label="DataOps intake", kind="sample", resource="",
    fetch=history_or_synthetic_fetch("dataops", "document.received", _DATAOPS_SYNTHETIC_DATA)))
