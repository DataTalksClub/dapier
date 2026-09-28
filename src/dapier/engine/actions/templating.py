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
import operator
import re
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

from .. import logic


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
            if name == "number_format":
                # Two grammars: plain decimals (one argument) vs the
                # currency form currency:CODE[:decimals] (two or three).
                if args and args[0].strip() == "currency":
                    low, high = 2, 3
                else:
                    low, high = 0, 1
            elif name == "switch":
                # Pair-wise a:b mappings, plus an optional odd trailing
                # default: every count from two arguments up is a grammar.
                low, high = 2, max(2, len(args))
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
        if key == "code":
            # Code-step source is code, not a template: it is never rendered,
            # so `{...}` (object literals) and `||` (JS or) are plain syntax
            # and template validation would only produce false failures.
            continue
        walk(value, str(key))


def _expand(token, context):
    parts = [part.strip() for part in token.split("|")]
    value = _resolve(context, parts[0])
    if value is None and len(parts) == 1:
        logger.debug("template token {%s} did not resolve; rendering as empty", token)
        return ""
    # A missing path with formatters still runs the chain against the empty
    # string: ``default`` can rescue it, and every other formatter renders
    # empty exactly as the short-circuit above did.
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


def _default(value, fallback):
    """The fallback when the interpolated value is empty (or whitespace)."""
    return value if value.strip() else fallback


# Symbol table for the common currencies; anything else falls back to a
# suffixed code ("1,234.56 JPY") rather than guessing a glyph.
_CURRENCY_SYMBOLS = {"EUR": "€", "USD": "$", "GBP": "£"}


def _number_format(value, *args):
    """Thousands separators with a fixed number of decimal places, or a
    currency rendering: ``number_format[:decimals]`` keeps the plain form,
    ``number_format:currency:CODE[:decimals]`` prefixes the symbol
    (``number_format:currency:EUR`` -> ``€1,234.56``)."""
    if args and str(args[0]).strip() == "currency":
        if len(args) < 2 or not str(args[1]).strip():
            raise ValueError("currency mode needs a code: number_format:currency:EUR")
        decimals = int(args[2]) if len(args) > 2 and str(args[2]).strip() else 2
        code = str(args[1]).strip().upper()
        amount = f"{float(value):,.{decimals}f}"
        symbol = _CURRENCY_SYMBOLS.get(code)
        return f"{symbol}{amount}" if symbol else f"{amount} {code}"
    if len(args) > 1:
        raise ValueError("plain number_format takes one decimals argument "
                         "(currency mode is number_format:currency:CODE)")
    decimals = int(args[0]) if args else 0
    return f"{float(value):,.{decimals}f}"


def _parse_datetime(value):
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        # Email events carry an RFC 2822 Date header ("Fri, 26 Sep 2026 ...").
        return parsedate_to_datetime(text)


def _date_format(value, spec):
    """strftime rendering, optionally in an IANA timezone: the argument is
    the raw format spec (colons stay, e.g. ``%H:%M``), and a trailing
    ``@<zone>`` converts before formatting —
    ``date_format:%Y-%m-%d %H:%M@Europe/Berlin``. A value without its own
    offset reads as UTC (the same rule logic.parse_moment applies); an
    unknown zone raises, which renders empty per the never-raise convention."""
    fmt, marker, zone = str(spec).partition("@")
    moment = _parse_datetime(value)
    if marker:
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        moment = moment.astimezone(ZoneInfo(zone.strip()))
    return moment.strftime(fmt)


def _time_until(value):
    """Relative time from now until the parsed moment: ``in 3h`` for the
    future, ``5m ago`` for the past, ``just now`` inside a minute; the
    largest unit wins (w, d, h, m — the offset units, rounded to nearest).
    Parses like the runs API's moment rule (logic.parse_moment): ISO 8601
    (date-only, trailing Z, missing offset read as UTC) or bare epoch
    seconds; anything else renders empty per the never-raise convention."""
    epoch = logic.parse_moment(value)
    if epoch is None and str(value or "").strip().isdigit():
        # Epoch seconds ("1780010400"); ISO wins for date-shaped digit runs
        # ("20260924" is the compact date, not an August-1970 timestamp).
        epoch = datetime.fromtimestamp(
            int(str(value).strip()), tz=timezone.utc).timestamp()
    if epoch is None:
        raise ValueError(f"not a datetime, e.g. 2026-10-01T09:00:00Z, got {value!r}")
    seconds = epoch - time.time()
    if abs(seconds) < 60:
        return "just now"
    count, unit = 0, "m"
    for unit_seconds, name in ((_OFFSET_UNITS["w"], "w"), (_OFFSET_UNITS["d"], "d"),
                               (_OFFSET_UNITS["h"], "h"), (_OFFSET_UNITS["m"], "m")):
        if abs(seconds) >= unit_seconds:
            count, unit = int(abs(seconds) / unit_seconds + 0.5), name
            break
    return f"{count}{unit} ago" if seconds < 0 else f"in {count}{unit}"


def _date_offset(value, spec):
    match = re.fullmatch(r"([+-]?\d+)\s*([smhdw])", spec.strip())
    if not match:
        raise ValueError(f"offset must look like '1d' or '-2h', got {spec!r}")
    seconds = int(match.group(1)) * _OFFSET_UNITS[match.group(2)]
    return (_parse_datetime(value) + timedelta(seconds=seconds)).isoformat()


def _split(value, sep, index="0"):
    parts = value.split(sep)
    try:
        position = int(index)
    except ValueError:
        raise ValueError(f"split index must be an integer, got {index!r}") from None
    return parts[position] if -len(parts) <= position < len(parts) else ""


def _join(value, sep):
    """Join a list the context JSON-stringified; a non-list passes through."""
    try:
        items = json.loads(value)
    except ValueError:
        return value
    if not isinstance(items, list):
        return value
    return str(sep).join(_stringify(item) for item in items)


def _title(value):
    return " ".join(word.capitalize() for word in value.split())


def _urlencode(value):
    from urllib.parse import quote_plus

    return quote_plus(value)


def _length(value):
    try:
        parsed = json.loads(value)
    except ValueError:
        return str(len(value))
    return str(len(parsed)) if isinstance(parsed, (str, list, dict)) else str(len(value))


def _truncate(value, limit, suffix="…"):
    cap = int(limit)
    if len(value) <= cap:
        return value
    return value[:max(0, cap - len(suffix))] + suffix


def _slugify(value):
    return re.sub(r"[\s_-]+", "-", re.sub(r"[^a-z0-9\s_-]", "", value.lower())).strip("-")


_EXTRACT_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# URLs stop at whitespace and the quote/bracket characters that wrap them in
# markup, so an href="..." or <https://…> wrapper does not ride along.
_EXTRACT_URL_RE = re.compile(r"https?://[^\s<>\"']+")
_EXTRACT_NUMBER_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")


def _switch(value, *args):
    """Value mapping: ``switch:a:b:c:d`` maps a→b and c→d; an odd trailing
    argument is the default when nothing matches; no match and no default
    renders empty. Any argument count of two or more is a legal grammar —
    validate_template special-cases the bounds."""
    for source, target in zip(args[::2], args[1::2]):
        if value == source:
            return target
    return args[-1] if len(args) % 2 else ""


def _extract_email(value):
    match = _EXTRACT_EMAIL_RE.search(value)
    return match.group(0) if match else ""


def _extract_url(value):
    match = _EXTRACT_URL_RE.search(value)
    if not match:
        return ""
    # Prose punctuation after the URL is a sentence's, not the link's.
    return match.group(0).rstrip(".,;:!?")


def _extract_number(value):
    match = _EXTRACT_NUMBER_RE.search(value)
    return match.group(0) if match else ""


def _pluck(value, path):
    """Read one dotted path out of a JSON string (numeric segments index
    lists, like template paths): scalars render as strings and containers
    as compact JSON; a parse failure or a miss renders empty."""
    try:
        parsed = json.loads(value)
    except ValueError:
        return ""
    found = _resolve(parsed, str(path).strip())
    if found is None:
        return ""
    if isinstance(found, (dict, list)):
        return json.dumps(found, separators=(",", ":"))
    return str(found)


def _math(operate):
    def apply(value, operand):
        result = operate(float(value), float(operand))
        if isinstance(result, float) and result.is_integer():
            return str(int(result))
        return str(result)
    return apply


# name -> (callable, (min_args, max_args), split_args_on_colon)
FORMATTERS = {
    "trim": (_trim, (0, 0), True),
    "lower": (str.lower, (0, 0), True),
    "upper": (str.upper, (0, 0), True),
    "title": (_title, (0, 0), True),
    "slice": (_slice, (1, 2), True),
    "split": (_split, (1, 2), True),
    "join": (_join, (1, 1), False),
    "replace": (str.replace, (2, 2), True),
    "regex_extract": (_regex_extract, (1, 1), False),
    "round": (_round, (0, 1), True),
    "format": (_format, (1, 1), False),
    "default": (_default, (1, 1), False),
    "number_format": (_number_format, (0, 3), True),
    "date_format": (_date_format, (1, 1), False),
    "date_offset": (_date_offset, (1, 1), False),
    "time_until": (_time_until, (0, 0), True),
    "urlencode": (_urlencode, (0, 0), True),
    "length": (_length, (0, 0), True),
    "truncate": (_truncate, (1, 2), True),
    "slugify": (_slugify, (0, 0), True),
    "add": (_math(operator.add), (1, 1), True),
    "subtract": (_math(operator.sub), (1, 1), True),
    "multiply": (_math(operator.mul), (1, 1), True),
    "divide": (_math(operator.truediv), (1, 1), True),
    "switch": (_switch, (2, 2**31), True),
    "extract_email": (_extract_email, (0, 0), True),
    "extract_url": (_extract_url, (0, 0), True),
    "extract_number": (_extract_number, (0, 0), True),
    "pluck": (_pluck, (1, 1), True),
}
