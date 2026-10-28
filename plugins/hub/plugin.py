"""Hub flow integration: canonical Hub session orchestration."""
from src.dapier.connectors.registry import Action, Connector, connector, register
from src.dapier.connectors import registry
from src.dapier.connectors.trigger_discovery import TriggerDiscovery, register_trigger_discovery
from src.dapier.triggers.poll_sources import PollSource, register_source

from plugins.hub.action import run_hub_session
from plugins.hub import completions

def validate_session(action):
    for field in ENTRY.required:
        if not isinstance(action.get(field), str) or not action[field].strip():
            raise ValueError(f"hub_session requires non-empty {field}")
    if "attachments" in action:
        from .attachments import enabled
        enabled(action["attachments"])
    for field in ENTRY.optional - {"attachments"}:
        if field in action and not isinstance(action[field], str):
            raise ValueError(f"hub_session {field} must be a string")
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
    optional=frozenset({"project", "workspace", "cwd", "tier", "notify_to", "notify_from", "attachments"}),
    fields=(
        {"key": "title", "label": "Title", "required": True},
        {"key": "text", "label": "Task instructions", "type": "textarea", "required": True},
        {"key": "acceptance_criteria", "label": "Acceptance criteria", "type": "textarea", "required": True},
        {"key": "workspace", "label": "Hub workspace", "default": "dapier-agents"},
        {"key": "cwd", "label": "Directory within workspace"},
        {"key": "tier", "label": "Tier", "type": "select",
         "options": ["quick", "daily", "deep"], "default": "daily"},
        {"key": "project", "label": "Hub project", "default": "dapier-agents"},
        {"key": "attachments", "label": "Trigger attachments", "type": "select", "options": ["on", "off"], "default": "on"},
        {"key": "notify_to", "label": "Completion recipient", "type": "email"},
        {"key": "notify_from", "label": "Completion sender", "type": "email", "default": "agents@dtcdev.click"},
    ),
))

connector(Connector(name="hub", label="Hub", events=("session.finished",), icon="bot",
                    event_info={"session.finished": ("Session finished", "A session started by Dapier reaches a terminal state, with its available Hub finish report")}))
register_source(PollSource(name="hub.sessions", connector="hub", event="session.finished", label="Hub sessions",
                           validate=completions.validate, fetch=completions.fetch,
                           prepare=completions.prepare, ack=completions.ack))


def _completion_sample(event=None, connection_id=None, limit=None):
    from src.dapier.connectors import trigger_discovery as discovery
    found = discovery.history_sample("hub", "session.finished")
    if found is not None:
        return {"sample": found, "source": "history", "connection_id": connection_id}
    return {"sample": discovery.synthetic_sample("hub", "session.finished", {
        "id": "20000000-0000-4000-8000-000000000002",
        "hub_task_id": "10000000-0000-4000-8000-000000000001",
        "hub_session_id": "20000000-0000-4000-8000-000000000002",
        "hub_link": "https://hub.dtcdev.click/#/intake/10000000-0000-4000-8000-000000000001",
        "state": "stopped", "fleet_state": "exited", "worker_report_status": "done",
        "finish_report_status": "available",
        "finish_report": {"kind": "report", "actor": "hub:service:fleet-finish",
                          "authority": "hub_launch_verified",
                          "source": {"reference": "fleet-finish:30000000-0000-4000-8000-000000000003"},
                          "text": "Example Hub finish report."},
        "source_workflow_id": "email-trigger-agents", "source_event_id": "example-event", "step_id": "delegate",
    }), "source": "synthetic", "connection_id": connection_id}



register_trigger_discovery(TriggerDiscovery(
    connector="hub", label="Hub", kind="sample", resource="", fetch=_completion_sample))
