"""Constants, the error types, and the pure helpers:
moment parsing, template lookup and rendering, and the rule
evaluation the condition/paths controls share."""
from datetime import datetime, timezone
import json
import re
import time


# Lambda-friendly bounds: a delay never sleeps longer than a single invocation
# should, and a loop never iterates enough to blow the timeout.
MAX_DELAY_SECONDS = 60
MAX_LOOP_ITERATIONS = 100

# A delay beyond the inline sleep cap suspends the run (RunSuspended); the
# suspended run may not outlive the executions table's 90-day TTL, so it is
# the ceiling for every delay total.
MAX_SUSPENDED_SECONDS = 90 * 86400

# What a step failure does; keep in sync with registry.ON_ERROR_MODES, the
# save-time validator (engine.logic stays import-light, so the literal lives
# here too).
ERROR_MODES = ("halt", "continue", "run")

# The lighter ``on_fail`` policy (Zapier's error setting): ``continue``
# absorbs a step failure and runs on; absent or ``halt`` re-raises. Keep in
# sync with registry.ON_FAIL_MODES.
ON_FAIL_MODES = ("continue", "halt")

# What a digest step does with the batch it names: ``accumulate`` appends
# the rendered item(s) to the workflow's digest, ``flush`` claims and
# clears it (see engine.actions.digests). Keep in sync with the registry's
# digest LogicStep options.
DIGEST_MODES = ("accumulate", "flush")

# Autoretry backoff defaults when the author sets only ``attempts``: the
# first retry waits one second and the doubling caps at a minute (the same
# bounds registry.AUTORETRY_SECONDS_BOUNDS enforces at save time).
AUTORETRY_DEFAULTS = {"initial_seconds": 1, "max_seconds": 60}

_TOKEN = re.compile(r"\{([^{}]+)\}")


class QuotaExceeded(Exception):
    """The account's monthly task quota is spent; a gate refused the step.

    Raised by a ``before_action`` hook (engine.usage.enforce, the worker's
    task-quota gate). ``_run_step`` re-raises it inside the step-execution
    try so the ordinary action-error machinery applies — the step closes
    ``failed`` with the quota message and honors on_fail/on_error exactly
    like a connector the plan disallows. Lives here (not in usage) so
    engine.logic stays dependency-free.
    """


class CompletedStep:
    """A worker gate skipped a completed step and restored its persisted result."""
    def __init__(self, status, output):
        self.status = status
        self.output = output

    def __bool__(self):
        return False


class RunSuspended(Exception):
    """A delay step paused the run past what one invocation may sleep.

    Not an error: the exception unwinds the whole chain while every level
    appends what is left of its own chain as a resume segment (innermost
    first, so a paused branch or loop iteration continues before the outer
    steps after it), and the worker parks the run on the event queue for the
    moment to resume (requeue-until: SQS DelaySeconds capped at 900s, longer
    waits chain re-enqueues — see engine.worker). Carries everything
    resuming needs: the moment to resume, the remaining segments, and the
    step outputs accumulated so far.
    """

    def __init__(self, resume_at, output):
        super().__init__(f"run suspended until {_iso(resume_at)}")
        self.resume_at = float(resume_at)  # epoch seconds
        self.output = output               # the delay step's output summary
        self.segments = []                 # [{steps, prefix, scope} | {loop: {...}}]
        self.step_outputs = {}             # snapshot set when the unwind starts
        self.paused_ids = []               # ids closed out ``delayed`` (innermost first)
        self.workflow_id = None            # filled by the worker's execute
        self.event = None                  # filled by the worker's execute
        self.delay_action_id = None        # the delay step's run-history id


def _iso(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def parse_moment(value):
    """An aware UTC epoch for an ISO 8601 datetime string, else None.

    Accepts date-only ("2026-10-01") and full timestamps; a trailing Z and
    a missing offset read as UTC. The same rule the runs API uses.
    """
    text = str(value or "").strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).timestamp()


def _elapsed(started):
    return int((time.monotonic() - started) * 1000)


def _step_id(prefix, action, index):
    """The run-history id of a step; sub-steps nest under their parent."""
    own = str(action.get("id") or index)
    return f"{prefix}.{own}" if prefix else own


def _lookup(context, path):
    """Resolve a dotted path (with integer list indexes) against nested data.

    Returns (value, found); a missing path yields (None, False) so callers can
    tell "absent" from a stored null.
    """
    current = context
    for part in str(path).split("."):
        if isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None, False
        elif isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None, False
    return current, True


def evaluate_rules(rules, data):
    """Evaluate trigger-filter style rules ``{field: {operator: value}}``.

    The same expression style as trigger filters (matching._matches_filter):
    every rule must pass. A bare value means equality, mirroring the engine.
    """
    from ..matching import _matches_filter

    if not isinstance(rules, dict) or not rules:
        return False
    for field, rule in rules.items():
        value, _found = _lookup(data, field)
        try:
            if not _matches_filter(value, rule):
                return False
        except KeyError:
            raise ValueError(f"unknown filter operator in rule for '{field}'") from None
    return True


def predicate_rules(action):
    """A filter/condition step's predicate as ``{field: rule}``, or None.

    ``when`` is the canonical multi-rule shape; the flat ``field``/
    ``operator``/``value`` triple is the designer shorthand for one rule.
    """
    when = action.get("when")
    if isinstance(when, dict) and when:
        return when
    field = str(action.get("field") or "").strip()
    if field:
        operator = str(action.get("operator") or "equals").strip()
        return {field: {operator: action.get("value")}}
    return None


def render_value(value, context):
    """Replace ``{dotted.path}`` tokens with context values.

    Tokens that do not resolve stay verbatim, so connector runners can still
    template them against the event data (and unknown fields degrade to the
    engine's usual empty-string rendering downstream).
    """
    if isinstance(value, str):
        return _TOKEN.sub(lambda match: _rendered(context, match.group(1).strip()), value)
    if isinstance(value, dict):
        return {key: render_value(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [render_value(item, context) for item in value]
    return value


def _rendered(context, path):
    value, found = _lookup(context, path)
    if not found:
        return "{" + path + "}"
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return "" if value is None else str(value)


