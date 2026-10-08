"""Human workflow names.

A workflow's ``id`` is its stable internal key (file name, API paths, run
records, hook triggers). People read a *name* instead: the optional ``name:``
override when the YAML carries one, else a name generated from what the flow
does — ``"<trigger phrase> → <main actions>"``, e.g. ``"Webhook agent-redo →
Agent"`` or ``"Dropbox file created in income-invoices → DataOps, delete from
Dropbox"``.

This module is the single source of truth: the console, the designer, and
the CLI all show what the API computes here. The rules are deterministic —
the same definition always yields the same name:

* Trigger phrase: a short connector + event phrase (``Email``, ``Webhook``,
  ``Zoom recording completed``), then the most identifying filter value when
  one is present (hook, email route/sender, channel, folder or path; opaque
  ids are skipped). Several triggers on one connector collapse to the
  connector; several connectors join with "or".
* Actions: every step in run order (branches included), labelled; plumbing
  steps (date formatting, code, filters, conditions, reads and finds) are
  dropped whenever a user-facing action exists; duplicate labels collapse;
  at most two labels show, the rest as "+N more".
"""
import re

MAX_NAME_LENGTH = 80
ARROW = "→"

# Whole-trigger phrases where "<Connector> <event words>" reads badly.
_TRIGGER_PHRASES = {
    "webhook.request.received": "Webhook",
    "email.message.received": "Email",
    "gmail.message.received": "Gmail",
    "renderer.job.completed": "Render finished",
    "schedule.schedule.triggered": "Schedule",
    "custom.received": "Custom event",
    "telegram.callback_query.received": "Telegram button tap",
}

# Connector nouns where the registry label is longer than people say it.
_CONNECTOR_NOUNS = {
    "webhook": "Webhook",
    "email": "Email",
    "google-drive": "Drive",
    "google-sheets": "Sheets",
    "google-calendar": "Calendar",
    "youtube": "YouTube",
    "s3": "S3",
    "rss": "RSS",
    "ai": "AI",
}

# Action labels: verb phrases people recognise. Unknown types fall back to
# the connector registry's label, then the type itself.
_ACTION_LABELS = {
    "agent": "Agent",
    "ai": "AI",
    "dataops": "DataOps",
    "dropbox_upload": "Upload to Dropbox",
    "dropbox_delete": "Delete from Dropbox",
    "dropbox_move": "Move in Dropbox",
    "dropbox_copy": "Copy in Dropbox",
    "dropbox_create_folder": "Create Dropbox folder",
    "s3_upload": "Upload to S3",
    "s3_delete_object": "Delete from S3",
    "drive_upload_file": "Upload to Drive",
    "drive_copy_file": "Copy in Drive",
    "drive_move_file": "Move in Drive",
    "drive_delete_file": "Delete from Drive",
    "drive_share_file": "Share Drive file",
    "slack": "Post to Slack",
    "slack_dm": "Slack DM",
    "slack_upload_file": "Upload to Slack",
    "telegram_send": "Send to Telegram",
    "telegram_send_document": "Send to Telegram",
    "telegram_send_photo": "Send to Telegram",
    "sheets_append_row": "Add row to Sheets",
    "sheets_update_row": "Update Sheets row",
    "sheets_delete_row": "Delete Sheets row",
    "webhook": "Send webhook",
    "http_request": "HTTP request",
    "email_send": "Send email",
    "gmail_send": "Send Gmail",
    "render_html_to_pdf": "Render PDF",
    "mailchimp_upsert_member": "Update Mailchimp member",
    "mailchimp_tag_member": "Tag Mailchimp member",
    "mailchimp_unsubscribe_member": "Unsubscribe in Mailchimp",
    "youtube_upload_video": "Upload to YouTube",
    "youtube_update_video": "Update YouTube video",
    "youtube_add_to_playlist": "Add to YouTube playlist",
    "zoom_create_meeting": "Create Zoom meeting",
    "zoom_delete_recording": "Delete Zoom recording",
    "run_workflow": "Run workflow",
    "storage_set": "Save to storage",
    # Plumbing (named only when nothing user-facing exists).
    "date_time": "Format date",
    "code": "Run code",
    "js": "Run code",
    "filter": "Filter",
    "condition": "Condition",
    "paths": "Paths",
    "delay": "Delay",
    "for_each": "Loop",
    "digest": "Digest",
    "digest_add": "Digest",
    "digest_flush": "Send digest",
}

# Steps that move data around rather than do what the flow is for.
_PLUMBING = {"date_time", "code", "js", "filter", "condition", "paths", "delay",
             "for_each", "digest", "digest_add", "formatter", "storage_get",
             "storage_find"}
_PLUMBING_PATTERN = re.compile(r"_(find|read|lookup|get|head|list|presign)(_|$)")

# Filter fields that identify a trigger, most identifying first, with the
# word that joins the value to the trigger phrase ("" = a plain space).
_FILTER_KEYS = (
    ("hook", ""), ("route", "to"), ("from", "from"), ("sender", "from"),
    ("to", "to"), ("channel", "in"), ("channel_name", "in"), ("chat", "in"),
    ("folder", "in"), ("path", "in"), ("subject", ""), ("label", ""),
)
_VALUE_OPERATORS = ("equals", "prefix", "contains", "suffix")
_MAX_VALUE_LENGTH = 32


def _connector_noun(connector):
    if connector in _CONNECTOR_NOUNS:
        return _CONNECTOR_NOUNS[connector]
    try:
        from ..connectors import registry

        entry = registry.CONNECTORS.get(connector)
        if entry is not None and entry.label:
            return str(entry.label)
    except Exception:  # noqa: BLE001 — naming must never fail a list
        pass
    words = re.sub(r"[-_.]+", " ", connector).strip()
    return words[:1].upper() + words[1:] if words else "Trigger"


def _event_words(event):
    parts = [part for part in re.split(r"[.]", event) if part]
    if parts and parts[-1] == "received":
        parts = parts[:-1]
    new = bool(parts) and parts[-1] == "new"
    if new:
        parts = parts[:-1]
    words = " ".join(part.replace("_", " ") for part in parts)
    return words, new


def trigger_phrase(trigger):
    """``"Dropbox file created"`` for one trigger spec (no filter value)."""
    connector = str(trigger.get("connector") or "").strip()
    event = str(trigger.get("event") or "").strip()
    if not connector:
        return "No trigger"
    key = f"{connector}.{event}"
    if key in _TRIGGER_PHRASES:
        return _TRIGGER_PHRASES[key]
    noun = _connector_noun(connector)
    words, new = _event_words(event)
    if new:
        return f"New {noun} {words}".strip()
    return f"{noun} {words}".strip()


def _opaque(value):
    """True for machine ids (channel ids, spreadsheet ids) — not names."""
    return (len(value) >= 16 and " " not in value and any(ch.isdigit() for ch in value)
            and any(ch.isupper() for ch in value))


def _filter_value(trigger):
    """(joiner, value) for the most identifying filter, or None."""
    filters = trigger.get("filters")
    if not isinstance(filters, dict):
        return None
    for field, joiner in _FILTER_KEYS:
        rule = filters.get(field)
        value = None
        if isinstance(rule, dict):
            for operator in _VALUE_OPERATORS:
                if isinstance(rule.get(operator), (str, int)) and str(rule[operator]).strip():
                    value = str(rule[operator]).strip()
                    break
        elif isinstance(rule, (str, int)) and str(rule).strip():
            value = str(rule).strip()
        if not value:
            continue
        if field in ("folder", "path"):
            segments = [segment for segment in value.split("/") if segment]
            if not segments:
                continue
            value = segments[-1]
        if field == "route" and trigger.get("connector") not in ("email", "gmail"):
            joiner = ""
        if _opaque(value):
            continue
        if len(value) > _MAX_VALUE_LENGTH:
            value = value[:_MAX_VALUE_LENGTH - 1].rstrip() + "…"
        return joiner, value
    return None


def _with_value(phrase, found):
    if not found:
        return phrase
    joiner, value = found
    return f"{phrase} {joiner} {value}" if joiner else f"{phrase} {value}"


def trigger_summary(workflow):
    """The trigger half of the generated name."""
    from .matching import workflow_triggers

    triggers = workflow_triggers(workflow)
    if not triggers:
        return "No trigger"
    if len(triggers) == 1:
        return _with_value(trigger_phrase(triggers[0]), _filter_value(triggers[0]))
    connectors = []
    for trigger in triggers:
        connector = str(trigger.get("connector") or "").strip()
        if connector and connector not in connectors:
            connectors.append(connector)
    values = {_filter_value(trigger) for trigger in triggers}
    if len(connectors) == 1:
        phrases = {trigger_phrase(trigger) for trigger in triggers}
        phrase = phrases.pop() if len(phrases) == 1 else _connector_noun(connectors[0])
        return _with_value(phrase, values.pop() if len(values) == 1 else None)
    nouns = [_connector_noun(connector) for connector in connectors]
    phrase = " or ".join(nouns[:2]) + (f" +{len(nouns) - 2}" if len(nouns) > 2 else "")
    common = {value[1] for value in values if value}
    if len(common) == 1 and None not in values:
        return f"{phrase} {common.pop()}"
    return phrase


def _flatten(steps):
    """Every step in run order, branches included (error handlers are not)."""
    for step in steps or []:
        if not isinstance(step, dict):
            continue
        yield step
        for key in ("then", "else", "actions", "default"):
            if isinstance(step.get(key), list):
                yield from _flatten(step[key])
        if isinstance(step.get("paths"), list):
            for branch in step["paths"]:
                if isinstance(branch, dict):
                    yield from _flatten(branch.get("actions"))


def _is_plumbing(kind):
    return kind in _PLUMBING or bool(_PLUMBING_PATTERN.search(kind))


_REGISTRY_VERB = re.compile(r"^(?P<service>[^:()]+?)\s*(?::\s*(?P<a>.+)|\((?P<b>[^)]+)\))$")


def _verb_first(label):
    """Registry labels read "Dropbox: move file" / "Google Calendar (delete
    event)"; a name reads better verb first: "Move file in Dropbox"."""
    match = _REGISTRY_VERB.match(label.strip())
    if not match:
        return label
    verb = (match.group("a") or match.group("b") or "").strip()
    if not verb:
        return label
    return f"{verb[:1].upper()}{verb[1:]} in {match.group('service').strip()}"


def action_label(kind):
    if kind in _ACTION_LABELS:
        return _ACTION_LABELS[kind]
    try:
        from ..connectors import registry

        entry = registry.ACTIONS.get(kind) or registry.LOGIC.get(kind)
        if entry is not None and entry.label:
            return _verb_first(str(entry.label))
    except Exception:  # noqa: BLE001 — naming must never fail a list
        pass
    words = re.sub(r"[_.]+", " ", kind).strip()
    return words[:1].upper() + words[1:] if words else "Action"


def _actions_of(workflow):
    actions = workflow.get("actions")
    if not isinstance(actions, list) and workflow.get("flow"):
        flows = workflow.get("flows") if isinstance(workflow.get("flows"), dict) else {}
        bound = flows.get(workflow["flow"]) if isinstance(flows, dict) else None
        actions = bound.get("actions") if isinstance(bound, dict) else None
    return actions if isinstance(actions, list) else []


def _join_case(label):
    """A follow-on verb phrase reads mid-sentence ("delete from Dropbox");
    names keep their capitals (DataOps, Agent, Google Calendar)."""
    words = label.split(" ")
    first = words[0]
    verb_phrase = len(words) > 1 and words[1][:1].islower()
    if verb_phrase and first[1:] == first[1:].lower():
        return label[:1].lower() + label[1:]
    return label


def action_summary(workflow, limit=2):
    """The actions half of the generated name ("" when there are none)."""
    kinds = [str(step.get("type") or "").strip() for step in _flatten(_actions_of(workflow))]
    kinds = [kind for kind in kinds if kind]
    meaningful = [kind for kind in kinds if not _is_plumbing(kind)] or kinds
    labels = []
    for kind in meaningful:
        label = action_label(kind)
        if label not in labels:
            labels.append(label)
    if not labels:
        return ""
    shown = [labels[0]] + [_join_case(label) for label in labels[1:limit]]
    text = ", ".join(shown)
    extra = len(labels) - limit
    return f"{text} +{extra} more" if extra > 0 else text


def generated_name(workflow):
    """The auto name: "<trigger phrase> → <main actions>"."""
    if not isinstance(workflow, dict):
        return ""
    trigger = trigger_summary(workflow)
    actions = action_summary(workflow)
    name = f"{trigger} {ARROW} {actions}" if actions else trigger
    if len(name) > MAX_NAME_LENGTH:
        name = name[:MAX_NAME_LENGTH - 1].rstrip() + "…"
    return name


def custom_name(workflow):
    """The ``name:`` override, stripped ("" when unset or not a string)."""
    value = workflow.get("name") if isinstance(workflow, dict) else None
    return value.strip() if isinstance(value, str) else ""


def display_name(workflow):
    """(name, source): the override when set ("custom"), else the generated
    name ("auto")."""
    custom = custom_name(workflow)
    if custom:
        return custom, "custom"
    return generated_name(workflow), "auto"


def name_fields(workflow):
    """The ``name`` / ``name_source`` pair API responses carry."""
    name, source = display_name(workflow)
    return {"name": name, "name_source": source}


def validate_name(value):
    """A ``name:`` value as stored ("" = no override) or raise ValueError."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("name must be a string")
    text = " ".join(value.split())
    if len(text) > MAX_NAME_LENGTH:
        raise ValueError(f"name may be at most {MAX_NAME_LENGTH} characters")
    return text
