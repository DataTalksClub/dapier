"""The connector registry: one place to add an integration.

Adding an action used to touch four hand-mirrored registries (the engine's
run_* dispatch chain, ACTION_SPECS validation in email_triggers, and the
designer's catalog.ts mirror). It is now one ``Action`` entry registered by
the integration's module under ``src/dapier/connectors/``, and three
consumers read the registry instead of private copies:

- the engine dispatches through :func:`run_action`;
- trigger validation (email/schedule/hook/poll) checks specs via
  :func:`validate_action_chain`;
- ``GET /api/catalog`` serves :func:`catalog` as JSON, and the designer
  renders its palette and inspector from that.

Trigger connectors (the palette's trigger chips) are declared here too, so a
new trigger source is one ``Connector`` entry plus its ingress normalizer.
The registry itself never imports the engine: connector modules import their
runners from ``engine.actions`` (which stays connector-agnostic), and the
engine's dispatch imports this package lazily inside the call.
"""

from dataclasses import dataclass


class ActionError(ValueError):
    """An action chain does not match the registered action specs."""


@dataclass(frozen=True)
class Action:
    """One connector action: how to run it, what it requires, how it renders.

    ``run`` has the engine dispatch signature
    ``(action, event, workflow_id, *, steps=None) -> dict``. ``required`` and
    ``optional`` are the YAML keys validation enforces; ``fields`` is the
    designer inspector schema (key/label/type/options/default/group dicts).
    ``icon`` names an entry in the designer's icon map (a lucide name or a
    product logo key).
    """

    type: str
    label: str
    run: object
    required: frozenset = frozenset()
    optional: frozenset = frozenset()
    description: str = ""
    icon: str = "file-text"
    fields: tuple = ()


@dataclass(frozen=True)
class Connector:
    """One trigger connector: the palette chip and its suggested events."""

    name: str
    label: str
    events: tuple = ()
    icon: str = "webhook"


@dataclass(frozen=True)
class LogicStep:
    """Catalog metadata for an in-workflow logic step (engine.logic)."""

    type: str
    label: str
    description: str = ""
    icon: str = "git-branch"
    fields: tuple = ()


ACTIONS: dict = {}
CONNECTORS: dict = {}
LOGIC: dict = {}


def register(action):
    ACTIONS[action.type] = action
    return action


def connector(entry):
    CONNECTORS[entry.name] = entry
    return entry


def logic_step(entry):
    LOGIC[entry.type] = entry
    return entry


def run_action(action, event, workflow_id=None, steps=None):
    """Dispatch one configured action through its registered runner."""
    entry = ACTIONS.get((action or {}).get("type"))
    if entry is None:
        raise ValueError(f"unsupported action: {(action or {}).get('type')!r}")
    return entry.run(action, event, workflow_id, steps=steps)


def action_specs():
    """``{type: (required, optional)}`` for trigger-side validation."""
    return {entry.type: (entry.required, entry.optional) for entry in ACTIONS.values()}


def validate_action_chain(actions):
    """Validate a stored trigger's action chain against the registry.

    The loud half of the contract: unknown types, missing required keys and
    unknown keys fail the save; template strings are checked by
    ``templating.validate_action``. Returns the chain unchanged.
    """
    if not isinstance(actions, list) or not actions:
        raise ActionError("at least one action is required")
    from ..engine.actions import templating

    for action in actions:
        if not isinstance(action, dict):
            raise ActionError("each action must be an object")
        action_type = action.get("type")
        entry = ACTIONS.get(action_type)
        if entry is None:
            raise ActionError(f"unsupported action type: {action_type!r}")
        missing = sorted(key for key in entry.required if not str(action.get(key) or "").strip())
        if missing:
            raise ActionError(f"{action_type} action is missing: {', '.join(missing)}")
        unknown = sorted(set(action) - entry.required - entry.optional - {"type", "id"})
        if unknown:
            raise ActionError(f"{action_type} action has unknown keys: {', '.join(unknown)}")
        try:
            templating.validate_action(action)
        except templating.TemplateError as exc:
            raise ActionError(f"{action_type}: {exc}") from exc
    return actions


def _entry_json(entry):
    return {
        "type": entry.type,
        "label": entry.label,
        "description": entry.description,
        "icon": entry.icon,
        "fields": [dict(field) for field in entry.fields],
    }


def catalog():
    """The JSON-safe catalog behind ``GET /api/catalog``."""
    return {
        "actions": [_entry_json(entry) for entry in ACTIONS.values()]
        + [_entry_json(entry) for entry in LOGIC.values()],
        "connectors": [
            {"name": entry.name, "label": entry.label, "events": list(entry.events), "icon": entry.icon}
            for entry in CONNECTORS.values()
        ],
        "filter_operators": ["equals", "prefix", "suffix", "contains"],
        "logic_operators": ["equals", "in", "prefix", "suffix", "contains"],
        "formatters": sorted(_formatter_names()),
    }


def _formatter_names():
    from ..engine.actions import templating

    return templating.FORMATTERS.keys()
