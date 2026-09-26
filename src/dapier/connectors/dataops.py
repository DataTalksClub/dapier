"""DataOps connector: forward events to a DataOps intake."""
from ..engine.actions.dataops import run_dataops
from .registry import Action, register

register(Action(
    type="dataops",
    label="DataOps intake",
    icon="database-zap",
    run=lambda action, event, workflow_id, steps=None: run_dataops(action, event),
    required=frozenset({"auth_secret_id"}),
    optional=frozenset({"url", "url_env", "timeout_seconds", "connection_id", "filename"}),
    fields=(
        {"key": "auth_secret_id", "label": "Auth secret ID", "placeholder": "dapier/dataops", "required": True},
        {"key": "url_env", "label": "URL env var", "placeholder": "DATAOPS_INTAKE_URL"},
        {"key": "url", "label": "URL (overrides env)"},
        {"key": "connection_id", "label": "Dropbox connection ID", "placeholder": "dropbox — for file-event intakes"},
        {"key": "filename", "label": "Filename override"},
        {"key": "timeout_seconds", "label": "Timeout (s)", "type": "number"},
    ),
))
