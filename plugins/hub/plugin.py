"""Hub flow integration: canonical Hub intake/session orchestration."""
from src.dapier.connectors.registry import Action, register
from src.dapier.connectors import registry

from plugins.hub.action import run_hub_session
from plugins.hub import intake  # noqa: F401 (registration of the existing intake action)

def validate_session(action):
    for field in ENTRY.required:
        if not isinstance(action.get(field), str) or not action[field].strip():
            raise ValueError(f"hub_session requires non-empty {field}")
    common = {"type", "id"} | registry.ERROR_STEP_KEYS | registry.ON_FAIL_STEP_KEYS | registry.AUTORETRY_STEP_KEYS
    unknown = set(action) - ENTRY.required - ENTRY.optional - common
    if unknown:
        raise ValueError(f"hub_session has unknown keys: {', '.join(sorted(unknown))}")


ENTRY = register(Action(
    type="hub_session", label="Hub session", icon="inbox",
    description="Create a Hub intake and request a headless session in its workspace.",
    run=run_hub_session,
    validate=validate_session,
    required=frozenset({"id", "title", "text", "acceptance_criteria"}),
    optional=frozenset({"project", "workspace", "cwd", "tier"}),
    fields=(
        {"key": "title", "label": "Title", "required": True},
        {"key": "text", "label": "Task instructions", "type": "textarea", "required": True},
        {"key": "acceptance_criteria", "label": "Acceptance criteria", "type": "textarea", "required": True},
        {"key": "workspace", "label": "Hub workspace", "default": "dapier-agents"},
        {"key": "cwd", "label": "Directory within workspace"},
        {"key": "tier", "label": "Tier", "type": "select",
         "options": ["quick", "daily", "deep"], "default": "daily"},
        {"key": "project", "label": "Hub project", "default": "dapier-agents"},
    ),
))
