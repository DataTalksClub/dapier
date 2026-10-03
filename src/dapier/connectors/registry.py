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
Discovery resources (``Discovery`` — a provider listing the console and CLI
browse to fill action fields) and provider health checks (``ConnectionTest``)
are declared by the same connector modules. The registry itself never imports
the engine: connector modules import their runners from ``engine.actions``
(which stays connector-agnostic), and the engine's dispatch imports this
package lazily inside the call.
"""

import math
import re
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

    A ``fields`` entry is passed through to clients verbatim and MAY also
    carry:

    - ``"help"``: help text shown beside the field (never enforced);
    - ``"discover"``: ``{"resource": "connector.name", "params": {...}}``
      pointing at a :class:`Discovery` that lists values for the field, where
      ``params`` maps each discovery parameter name to the key of ANOTHER
      field of the same action supplying its value (never enforced);
    - ``"type"``: ``number``, ``email``, or ``url`` — save-time validation
      checks *literal* values against the type
      (:func:`validate_action_chain`); templated values are skipped, since
      what they render to is data, not config.
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


@dataclass(frozen=True)
class Discovery:
    """One connection-scoped discovery: a provider listing clients browse.

    ``run(connection, params) -> list[dict]`` receives the stored connection
    record plus the resolved query params, and returns items that each carry
    at least ``{"id": str, "name": str}`` (extra keys are welcome and render
    as details). ``params`` is a tuple of field dicts in the Action
    ``fields`` schema (key/label/type/required/default) describing the query
    parameters the caller must supply. Registered under
    ``f"{connector}.{name}"``.
    """

    name: str
    connector: str
    label: str
    description: str = ""
    params: tuple = ()
    run: object = None


@dataclass(frozen=True)
class ConnectionTest:
    """One provider health check behind the connection test endpoint.

    ``run(connection) -> dict`` returns ``{"ok": bool, "detail": str}`` plus
    an optional ``identity`` dict; it reports a verdict instead of raising so
    every caller can render the outcome. Registered under ``connector``
    (the connection's provider key).
    """

    connector: str
    run: object = None


ACTIONS: dict = {}
CONNECTORS: dict = {}
LOGIC: dict = {}
DISCOVERIES: dict = {}
CONNECTION_TESTS: dict = {}


# A connection's provider key and the discovery sources it can reach: one
# Google OAuth connection (provider "google") backs the Sheets, Drive,
# Calendar and Gmail resources. "s3"/"aws" name the stored AWS-keys
# credential, which has no connection record but resolves the same bucket
# listing.
PROVIDER_DISCOVERY_SOURCES = {
    "google": ("google-sheets", "google-drive", "google-calendar", "gmail"),
    "youtube": ("youtube",),
}

# Providers whose health check is registered under another key. Now empty:
# the s3→aws alias moved into the aws plugin's manifest, and plugins feed
# this map through the loader.
CONNECTION_TEST_ALIASES = {}


def register(action):
    ACTIONS[action.type] = action
    return action


def connector(entry):
    CONNECTORS[entry.name] = entry
    return entry


def logic_step(entry):
    LOGIC[entry.type] = entry
    return entry


def register_discovery(discovery):
    DISCOVERIES[f"{discovery.connector}.{discovery.name}"] = discovery
    return discovery


def register_connection_test(test):
    CONNECTION_TESTS[test.connector] = test
    return test


def discoveries():
    """Every registered :class:`Discovery`, sorted by ``connector.name`` key."""
    return [DISCOVERIES[key] for key in sorted(DISCOVERIES)]


def connection_tests():
    """``{connector: ConnectionTest}`` — a copy, so callers cannot mutate."""
    return dict(CONNECTION_TESTS)


def discoveries_for_provider(provider):
    """Discoveries reachable through a connection of ``provider``.

    A connection's provider key is coarser than the discovery sources
    (Google OAuth connections serve both Sheets and Drive), so this filters
    :data:`PROVIDER_DISCOVERY_SOURCES` against the registry.
    """
    sources = PROVIDER_DISCOVERY_SOURCES.get(str(provider or "").strip().lower(), ())
    return [entry for entry in discoveries() if entry.connector in sources]


def connection_test_for(provider):
    """The registered health check for ``provider``, or None when it has none."""
    provider = str(provider or "").strip().lower()
    return CONNECTION_TESTS.get(CONNECTION_TEST_ALIASES.get(provider, provider))


def run_action(action, event, workflow_id=None, steps=None):
    """Dispatch one configured action through its registered runner."""
    entry = ACTIONS.get((action or {}).get("type"))
    if entry is None:
        raise ValueError(f"unsupported action: {(action or {}).get('type')!r}")
    return entry.run(action, event, workflow_id, steps=steps)


def action_specs():
    """``{type: (required, optional)}`` for trigger-side validation."""
    return {entry.type: (entry.required, entry.optional) for entry in ACTIONS.values()}


# Generic per-step error-handling keys, legal on any step (connector action
# or logic step alike): ``on_error: halt|continue|run`` decides what a step
# failure does — halt aborts the run (today's behavior, the default),
# continue marks the step failed and moves on, run executes the step's
# ``error_actions`` chain in its place. The engine honors these keys at run
# time; validate_action_chain and the designer's structural validation share
# the shape checks below so a bad value fails the save, not the run.
ON_ERROR_MODES = ("halt", "continue", "run")
ERROR_STEP_KEYS = frozenset({"on_error", "error_actions"})

# The lighter per-step failure policy (Zapier's error setting), legal on any
# step beside ``on_error`` — the two are mutually exclusive on one step:
# ``continue`` absorbs the failure (the step reads ``skipped`` in run
# history) and the chain runs on; absent or ``halt`` fails the run.
ON_FAIL_MODES = ("continue", "halt")
ON_FAIL_STEP_KEYS = frozenset({"on_fail"})

# Per-step autoretry (Zapier's autoretry): legal on connector actions only,
# never on logic steps. ``autoretry: {attempts, initial_seconds,
# max_seconds}`` — ``attempts`` (1..AUTORETRY_ATTEMPTS_MAX) is required, the
# seconds (AUTORETRY_SECONDS_BOUNDS) are optional with engine defaults 1 and
# 60 (engine.logic.AUTORETRY_DEFAULTS — keep in sync). The engine retries
# the action with exponential backoff before any on_fail/on_error policy
# applies; the bounds keep those inline backoff sleeps small inside one
# invocation.
AUTORETRY_STEP_KEYS = frozenset({"autoretry"})
AUTORETRY_FIELDS = frozenset({"attempts", "initial_seconds", "max_seconds"})
AUTORETRY_ATTEMPTS_MAX = 3
AUTORETRY_SECONDS_BOUNDS = (1, 60)


def validate_autoretry_key(step, where, error=ActionError):
    """Validate one step's ``autoretry`` config (connector actions only).

    Sibling of :func:`validate_error_keys`/:func:`validate_on_fail_key`:
    shape-checks the mapping and its bounds with clear messages, and rejects
    the key on logic steps — retrying a filter, a delay or a loop is not a
    thing. The engine's run-time plan parser (engine.logic._autoretry_plan)
    calls this same validator, so a config that slips past a save fails the
    step with the same message.
    """
    config = step.get("autoretry")
    if config is None or (isinstance(config, str) and not config.strip()):
        return step
    if str(step.get("type") or "") in LOGIC:
        raise error(f"{where}: autoretry only applies to connector actions")
    if not isinstance(config, dict):
        raise error(f"{where}: autoretry must be a mapping of "
                    f"{', '.join(sorted(AUTORETRY_FIELDS))}")
    unknown = sorted(set(config) - AUTORETRY_FIELDS)
    if unknown:
        raise error(f"{where}: autoretry has unknown keys: {', '.join(unknown)}")
    attempts = config.get("attempts")
    if attempts is None:
        raise error(f"{where}: autoretry needs attempts (1-{AUTORETRY_ATTEMPTS_MAX})")
    if isinstance(attempts, bool) or not isinstance(attempts, int) \
            or not 1 <= attempts <= AUTORETRY_ATTEMPTS_MAX:
        raise error(f"{where}: autoretry attempts must be a whole number "
                    f"between 1 and {AUTORETRY_ATTEMPTS_MAX}")
    low, high = AUTORETRY_SECONDS_BOUNDS
    seconds = {}
    for key in ("initial_seconds", "max_seconds"):
        if key not in config or config[key] is None:
            continue
        value = config[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not low <= value <= high:
            raise error(f"{where}: autoretry {key} must be a number between "
                        f"{low} and {high}")
        seconds[key] = value
    if "initial_seconds" in seconds and "max_seconds" in seconds \
            and seconds["initial_seconds"] > seconds["max_seconds"]:
        raise error(f"{where}: autoretry initial_seconds must not exceed max_seconds")
    return step


def validate_on_fail_key(step, where, error=ActionError):
    """Validate one step's generic ``on_fail`` key.

    Sibling of :func:`validate_error_keys` for the lighter ``on_fail:
    continue|halt`` policy; a step may carry one policy, never both.
    """
    mode = str(step.get("on_fail") or "").strip()
    if not mode:
        return step
    if str(step.get("on_error") or "").strip():
        raise error(f"{where}: set on_fail or on_error, not both")
    if mode not in ON_FAIL_MODES:
        raise error(f"{where}: on_fail must be one of {', '.join(ON_FAIL_MODES)}")
    return step


# Save-time field types: a ``fields`` entry may declare ``type`` of
# number/integer/boolean/email/url — or ``enum`` with a ``choices`` list (a
# ``select`` field's ``options`` count as choices too) — and
# validate_action_chain rejects *literal* values that are clearly wrong for
# it. Only literals are checked — a value carrying a ``{token}`` template
# renders at run time from data the save has not seen, so it is skipped, and
# an empty value stays the required-key check's job. Deliberately shallow (a
# "plausible shape", not a spec-compliance test) so existing chains keep
# saving: the registry flags typos, not intent. Enum membership compares
# case-insensitively because every runner normalizes case before checking
# (method and value_input_option upper, find/kind/match lower).
FIELD_TYPES = ("number", "integer", "boolean", "email", "url", "enum")
# The designer's widget alias for enum: a select field's options are choices.
ENUM_ALIASES = ("select",)
_TEMPLATE_TOKEN = re.compile(r"\{[^{}]*\}")
_EMAIL_SHAPE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_URL_SHAPE = re.compile(r"^[a-z][a-z0-9+.-]*://\S+$", re.IGNORECASE)
_BOOLEAN_WORDS = ("true", "false", "1", "0", "yes", "no", "on", "off")


def _field_type_violation(field_type, value):
    """What a literal value gets wrong for its declared type, or None."""
    if value is None or isinstance(value, (dict, list)):
        return None
    text = str(value).strip()
    if not text or _TEMPLATE_TOKEN.search(text):
        return None
    if field_type in ("number", "integer"):
        if isinstance(value, bool):
            return f"{value!r} is not a number"
        try:
            number = float(text)
        except ValueError:
            return f"{value!r} is not a number"
        if not math.isfinite(number):
            return f"{value!r} is not a number"
        if field_type == "integer" and not number.is_integer():
            return f"{value!r} is not a whole number"
        return None
    if field_type == "boolean":
        if isinstance(value, bool) or text.lower() in _BOOLEAN_WORDS:
            return None
        return f"{value!r} is not a boolean (true/false)"
    if field_type == "email":
        # run_email_send splits ``to`` on commas, so every part must look
        # like one address.
        addresses = [part.strip() for part in text.split(",") if part.strip()]
        if addresses and all(_EMAIL_SHAPE.fullmatch(part) for part in addresses):
            return None
        return f"{value!r} is not an email address"
    if field_type == "url":
        return None if _URL_SHAPE.fullmatch(text) else f"{value!r} is not a URL"
    return None


def _enum_violation(choices, value):
    """What a literal value gets wrong for its choice list, or None."""
    if value is None or isinstance(value, (dict, list)):
        return None
    text = str(value).strip()
    if not text or _TEMPLATE_TOKEN.search(text):
        return None
    options = [choice for choice in (choices or ()) if str(choice).strip()]
    if text.lower() in {str(choice).strip().lower() for choice in options}:
        return None
    return f"{value!r} is not one of: {', '.join(sorted(str(choice) for choice in options))}"


def _validate_field_types(action, entry):
    """Reject clearly-wrong literals for typed fields (see FIELD_TYPES)."""
    for field in entry.fields:
        key = field.get("key")
        field_type = field.get("type")
        if key not in action or (
            field_type not in FIELD_TYPES and field_type not in ENUM_ALIASES
        ):
            continue
        if field_type == "enum" or field_type == "select":
            choices = field.get("choices")
            if choices is None:
                choices = field.get("options")
            violation = _enum_violation(choices, action[key])
        else:
            violation = _field_type_violation(field_type, action[key])
        if violation:
            raise ActionError(f"{entry.type} action field '{key}' {violation}")


def field_type_problem(field_type, value, choices=None):
    """The type problem with one concrete value, or None.

    Public form of :func:`_field_type_violation`/:func:`_enum_violation` for
    the dry-run, which checks what a sample event *rendered* under the same
    rules save time applies to literals.
    """
    if field_type in ("enum", "select"):
        return _enum_violation(choices, value)
    return _field_type_violation(field_type, value)


def validate_field_types(action, where, error=ActionError):
    """One step's typed-field literals (see FIELD_TYPES), in the caller's
    error dialect — the designer's save path applies the same rules stored
    trigger chains answer to through :func:`validate_action_chain`.
    Registered actions only; logic steps and unknown types have no field
    schema to check against.
    """
    entry = ACTIONS.get((action or {}).get("type"))
    if entry is None:
        return action
    try:
        _validate_field_types(action, entry)
    except ActionError as exc:
        raise error(str(exc)) from None
    return action


def validate_error_keys(step, where, error=ActionError):
    """Validate one step's generic ``on_error``/``error_actions`` keys.

    ``error`` is the caller's exception dialect (ActionError for stored
    trigger chains, WorkflowError for designer saves); ``where`` names the
    step in messages. Recursing into ``error_actions`` steps is the caller's
    job — the designer owns step-chain recursion.
    """
    mode = str(step.get("on_error") or "").strip()
    if mode and mode not in ON_ERROR_MODES:
        raise error(f"{where}: on_error must be one of {', '.join(ON_ERROR_MODES)}")
    if mode == "run" and "error_actions" not in step:
        raise error(f"{where}: error_actions must be a non-empty list of steps")
    if "error_actions" in step:
        actions = step.get("error_actions")
        if not isinstance(actions, list) or not actions:
            raise error(f"{where}: error_actions must be a non-empty list of steps")
        if mode != "run":
            raise error(f"{where}: error_actions only run when on_error is run")
    return step


def validate_action_chain(actions):
    """Validate a stored trigger's action chain against the registry.

    The loud half of the contract: unknown types, missing required keys and
    unknown keys fail the save; template strings are checked by
    ``templating.validate_action``. The generic error-handling keys are
    allowed on every action and shape-checked here, and literal values of
    typed fields (``type: number|integer|boolean|email|url|enum`` — see
    ``FIELD_TYPES``) must be plausible for their type; templated values are
    skipped. Returns the chain unchanged.
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
        unknown = sorted(set(action) - entry.required - entry.optional - {"type", "id"}
                         - ERROR_STEP_KEYS - ON_FAIL_STEP_KEYS - AUTORETRY_STEP_KEYS)
        if unknown:
            raise ActionError(f"{action_type} action has unknown keys: {', '.join(unknown)}")
        validate_error_keys(action, str(action_type))
        validate_on_fail_key(action, str(action_type))
        validate_autoretry_key(action, str(action_type))
        if action.get("error_actions"):
            validate_action_chain(action["error_actions"])
        _validate_field_types(action, entry)
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


# The filter/predicate operators engine.matching._matches_filter implements —
# the single evaluator behind trigger filters and every logic
# filter/condition/paths rule, so both lists carry the same set (``in`` has
# always worked on trigger filters too). The catalog output is what the
# designer's catalog.ts mirror is hand-synced from; an unknown operator
# still fails loudly at run time, which doubles as save-time detection.
FILTER_OPERATORS = ("equals", "not_equals", "in", "prefix", "suffix", "contains",
                    "does_not_contain", "gt", "gte", "lt", "lte", "exists", "empty")
LOGIC_OPERATORS = FILTER_OPERATORS


def catalog():
    """The JSON-safe catalog behind ``GET /api/catalog``."""
    return {
        # Sorted: plugin modules load in their own order, so registration
        # order is no longer a stable surface.
        "actions": [_entry_json(entry)
                    for entry in sorted(ACTIONS.values(), key=lambda e: e.type)]
        + [_entry_json(entry)
           for entry in sorted(LOGIC.values(), key=lambda e: e.type)],
        "connectors": [
            {"name": entry.name, "label": entry.label, "events": list(entry.events), "icon": entry.icon}
            for entry in sorted(CONNECTORS.values(), key=lambda e: e.name)
        ],
        # Discovery manifests: which provider listings exist and the query
        # params each one needs (the same field-dict schema as action fields).
        "discoveries": [
            {
                "connector": entry.connector,
                "name": entry.name,
                "label": entry.label,
                "description": entry.description,
                "params": [dict(param) for param in entry.params],
            }
            for entry in discoveries()
        ],
        # Trigger-connector discovery (connectors.trigger_discovery): which
        # connectors can pull a sample event, and the field-option listings
        # each one offers (POST /api/agent|admin/discover).
        "discovery": _trigger_discovery_catalog(),
        "filter_operators": list(FILTER_OPERATORS),
        "logic_operators": list(LOGIC_OPERATORS),
        "formatters": sorted(_formatter_names()),
    }


def _trigger_discovery_catalog():
    from . import trigger_discovery

    return trigger_discovery.trigger_discovery_catalog()


def _formatter_names():
    from ..engine.actions import templating

    return templating.FORMATTERS.keys()
