"""Template rendering for workflow actions: fields, step outputs, formatters.

A template is ordinary text with ``{token}`` placeholders. A token is a dotted
path into the execution context, optionally followed by ``|``-separated
formatter calls:

    {subject}                           event data field (as always)
    {trigger.subject | trim | upper}    the triggering event (data + envelope)
    {steps.render.output.s3.key}        an earlier action's captured output
    {steps.render.status}               that step's execution status

The context mirrors what the run history records: every executed step
contributes ``status`` and ``output`` under its action id. Missing paths
render as an empty string, exactly like a missing ``{field}`` always has, and
never fail a run; the same applies to a formatter that cannot be applied.
Unknown formatter names are rejected loudly when a workflow or trigger is
saved (``validate_template``) and are skipped with a warning at run time.
"""
import json
import logging
import re
from datetime import datetime, timedelta


logger = logging.getLogger(__name__)

# Literal braces ({{, }}) win over tokens wherever they start matching first.
TOKEN_RE = re.compile(r"\{\{|\}\}|\{([^{}]+)\}")

_OFFSET_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


class TemplateError(ValueError):
    """A template is malformed or names an unknown formatter (save-time)."""


def build_context(event, steps=None):
    """The template context for one action run.

    The event's data fields sit at the top level (the historical ``{field}``
    form keeps working), the whole event is mirrored under ``trigger`` — data
    fields first so ``{trigger.subject}`` means the message subject, envelope
    scalars (``connector``, ``event``, ``id``, ``occurred_at``, ``source``)
    alongside — and the steps captured so far under ``steps``, shaped like the
    run history: ``{action_id: {"status": ..., "output": ...}}``.
    """
    data = event.get("data") if isinstance(event, dict) else None
    data = data if isinstance(data, dict) else {}
    trigger = {
        **data,
        "id": event.get("id"),
        "connector": event.get("connector"),
        "event": event.get("event"),
        "source": event.get("source"),
        "occurred_at": event.get("occurred_at"),
        "data": data,
    }
    return {**data, "trigger": trigger, "steps": steps or {}}


def render(template, event, steps=None):
    """Expand a template against the event and the steps run before it.

    Never raises on data: a path that does not resolve, a formatter the
    context cannot be applied to, or an unknown formatter name all render as
    an empty string, with a log line.
    """
    context = build_context(event, steps)

    def substitute(match):
        text = match.group(0)
        if text in ("{{", "}}"):
            return text[:1]
        return _expand(match.group(1), context)

    return TOKEN_RE.sub(substitute, str(template or ""))


def validate_template(template):
    """Raise TemplateError when a template is malformed or names an unknown
    formatter. Paths are data-dependent, so only their shape is checked; this
    is the loud half of the contract, run at workflow/trigger save time."""
    for match in TOKEN_RE.finditer(str(template or "")):
        if not match.group(1):
            continue  # literal {{ or }}
        token = match.group(1)
        parts = [part.strip() for part in token.split("|")]
        if not parts[0]:
            raise TemplateError(f"empty path in token {{{token}}}")
        for spec in parts[1:]:
            name, args = _formatter_args(spec)
            entry = FORMATTERS.get(name)
            if entry is None:
                raise TemplateError(
                    f"unknown formatter '{name}' in token {{{token}}} "
                    f"(known formatters: {', '.join(sorted(FORMATTERS))})")
            low, high = entry[1]
            if not low <= len(args) <= high:
                wanted = str(low) if low == high else f"{low}-{high}"
                raise TemplateError(
                    f"formatter '{name}' in token {{{token}}} takes {wanted} "
                    f"argument(s), got {len(args)}")


def validate_action(action):
    """Validate every template string in an action mapping (recursively),
    naming the offending field in the error."""
    def walk(value, path):
        if isinstance(value, str):
            try:
                validate_template(value)
            except TemplateError as exc:
                raise TemplateError(f"{path}: {exc}") from None
        elif isinstance(value, dict):
            for key, item in value.items():
                walk(item, f"{path}.{key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]")

    for key, value in (action or {}).items():
        walk(value, str(key))


def _expand(token, context):
    parts = [part.strip() for part in token.split("|")]
    value = _resolve(context, parts[0])
    if value is None:
        logger.debug("template token {%s} did not resolve; rendering as empty", token)
        return ""
    text = _stringify(value)
    for spec in parts[1:]:
        try:
            text = _apply_formatter(text, spec)
        except _UnknownFormatter as exc:
            logger.warning("template token {%s}: %s; rendering as empty", token, exc)
            return ""
        except Exception as exc:
            logger.warning("template token {%s}: formatter '%s' failed (%s); rendering as empty",
                           token, spec, exc)
            return ""
    return text


def _resolve(context, path):
    """Walk a dotted path; numeric segments index lists. None when missing."""
    value = context
    for part in path.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and re.fullmatch(r"-?\d+", part):
            index = int(part)
            if not -len(value) <= index < len(value):
                return None
            value = value[index]
        else:
            return None
    return value


def _stringify(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str)
    return str(value)


def _formatter_args(spec):
    """Split ``name:arg1:arg2`` into (name, args). Single-argument formatters
    keep their raw remainder, so patterns and format specs may contain ':'."""
    name, _, rest = spec.partition(":")
    name = name.strip()
    entry = FORMATTERS.get(name)
    split = entry[2] if entry else True
    if not rest:
        return name, []
    return name, rest.split(":") if split else [rest]


class _UnknownFormatter(Exception):
    pass


def _apply_formatter(text, spec):
    name, args = _formatter_args(spec)
    entry = FORMATTERS.get(name)
    if entry is None:
        raise _UnknownFormatter(f"unknown formatter '{spec.strip()}'")
    return str(entry[0](text, *args))


def _trim(value):
    return value.strip()


def _slice(value, start, end=None):
    start = int(start) if str(start).strip() else 0
    if end is None or not str(end).strip():
        return value[start:]
    return value[start:int(end)]


def _regex_extract(value, pattern):
    match = re.search(pattern, value)
    if not match:
        return ""
    return match.group(1) if match.groups() else match.group(0)


def _round(value, digits="0"):
    places = int(digits)
    number = round(float(value), places)
    return str(int(number)) if places <= 0 else str(number)


def _format(value, spec):
    try:
        number = int(value)
    except ValueError:
        number = float(value)  # raises ValueError for non-numeric input
    return format(number, spec)


def _parse_datetime(value):
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


def _date_format(value, spec):
    return _parse_datetime(value).strftime(spec)


def _date_offset(value, spec):
    match = re.fullmatch(r"([+-]?\d+)\s*([smhdw])", spec.strip())
    if not match:
        raise ValueError(f"offset must look like '1d' or '-2h', got {spec!r}")
    seconds = int(match.group(1)) * _OFFSET_UNITS[match.group(2)]
    return (_parse_datetime(value) + timedelta(seconds=seconds)).isoformat()


# name -> (callable, (min_args, max_args), split_args_on_colon)
FORMATTERS = {
    "trim": (_trim, (0, 0), True),
    "lower": (str.lower, (0, 0), True),
    "upper": (str.upper, (0, 0), True),
    "slice": (_slice, (1, 2), True),
    "replace": (str.replace, (2, 2), True),
    "regex_extract": (_regex_extract, (1, 1), False),
    "round": (_round, (0, 1), True),
    "format": (_format, (1, 1), False),
    "date_format": (_date_format, (1, 1), False),
    "date_offset": (_date_offset, (1, 1), False),
}
