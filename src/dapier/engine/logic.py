"""In-workflow logic steps: filter, condition, paths, delay, for_each, digest.

Logic steps live in the action chain next to connector actions and share the
same step telemetry (the before/after/error hooks), so run history shows them
like any other step. A failed filter is not an error: the step is marked
``filtered`` and the rest of the chain is skipped quietly.

Step shapes (predicates read the event's ``data`` — or the loop's scope —
plus the ``steps`` outputs of every step before them, so mid-chain filters
can route on what earlier steps found; operators are the trigger-filter set
in ``engine.matching._matches_filter``: equals, not_equals, in, prefix,
suffix, contains, does_not_contain, gt, gte, lt, lte — numeric when both
sides parse as float — plus exists and empty):

    - id: only-invoices
      type: filter
      field: subject          # dotted path; {field}/{operator}/{value} ...
      operator: contains      #   is the shorthand for one rule ...
      value: invoice          # ... or the trigger-filter style:
      # when:
      #   route: {equals: invoice}
      #   subject: {contains: invoice}
      #   steps.find.output.row_id: {exists: true}

    - id: invoice-path
      type: condition
      when: {route: {equals: invoice}}
      then: [...]             # steps for the matching side
      else: [...]             # optional steps for the other side

    - id: route
      type: paths
      paths:
        - label: invoices       # first matching branch wins ...
          when: {subject: {contains: invoice}}
          actions: [...]
        - label: receipts       # ... the flat filter shorthand works here too
          field: subject
          operator: contains
          value: receipt
          actions: [...]
      default: [...]            # optional; runs when no branch matched

    - id: pause
      type: delay
      seconds: 30             # ≤ MAX_DELAY_SECONDS sleeps inside this invocation
      # Longer pauses suspend the run (see RunSuspended) and resume later —
      # any combination of, or instead of seconds:
      # minutes: 5
      # hours: 2
      # days: 1
      # until: "2026-10-01T09:00:00+00:00"   # template-renderable, exclusive
      #                                       # with the duration fields

    - id: each-attachment
      type: for_each
      list: attachments       # dotted path to a list in the event data
      item: item              # template variable bound per iteration
      actions: [...]          # steps run once per item, {item.*} rendered

    - id: collect
      type: digest
      mode: accumulate        # accumulate (default) | flush
      key: nightly-invoices   # required; the digest's name (per-workflow)
      item: "{subject}"       # one rendered item per run, and/or:
      # items:               # a list of rendered items appended in order
      #   - "{trigger.occurred_at}"
      #   - "{steps.render.output.url}"

    - id: send
      type: digest
      mode: flush
      key: nightly-invoices   # the same key the accumulate steps used
      # Flush claims and clears the batch in one atomic operation (two
      # concurrent flushes never double-send; an appender racing a flush
      # lands in the next digest). The claimed items ride the event for the
      # rest of the run as ``digest``: following steps template
      # ``{digest.items}`` (the list) and ``{digest.count}``, and a for_each
      # can iterate ``list: digest.items``. An empty digest flushes to
      # nothing: the step records ``skipped`` in run history with
      # ``{count: 0, items: [], empty: true}`` in its output and the chain
      # runs on (a following send can gate on ``{digest.count}``).

Error handling (generic keys, legal on any step — logic or connector action):

    - id: call-api
      type: http_request
      url: https://example.test
      on_error: halt          # halt (default) | continue | run
      error_actions:          # with on_error: run: what to do on failure
        - id: alert
          type: slack
          channel: "#alerts"
          text: "step failed: {steps.call-api.error}"

``halt`` (the default) is today's behavior: the exception aborts the run.
``continue`` records the step as ``failed`` and moves on to the next step.
``run`` records the failure the same way, executes ``error_actions`` as a
sub-chain (ids prefixed ``<step>.error``), then moves on. Handled failures
land in the ``steps`` context with the error message templatable as
``{steps.<id>.error}`` (and ``status: failed``), so the steps that follow —
and the error branch itself — can react to what broke. A filter inside an
error branch still stops the run quietly, like any filtered branch.

The lighter ``on_fail: continue|halt`` key is the Zapier error policy on its
own: ``continue`` absorbs the step's failure — the step is recorded
``skipped`` (the error message templatable as ``{steps.<id>.error}``, like
the handled failures above) — and the chain runs on to completion; the run
itself still completes successfully. Absent (or ``halt``) is today's
behavior: the error fails the run to the queue's redrive. ``on_fail`` and
``on_error`` are mutually exclusive on one step.

Autoretry (Zapier's autoretry) is the third key, connector actions only: it
retries a transient failure in place, before any error policy applies.

    - id: call-api
      type: http_request
      url: https://example.test
      autoretry:
        attempts: 2            # 1-3; total tries = attempts + 1
        initial_seconds: 1     # optional (default 1, bounds 1-60)
        max_seconds: 30        # optional (default 60, bounds 1-60): where
                               # the doubling backoff caps

On an exception the action retries up to ``attempts`` times with exponential
backoff (``initial_seconds`` doubling per retry, capped at ``max_seconds``,
plus small jitter so concurrent failures do not retry in lockstep). Once the
retries are exhausted the failure falls through to the step's ``on_fail``/
``on_error`` policy exactly as a single attempt would — and past both, to
the workflow-level ``retry`` policy (the worker's queue redrive), which
sees only the still-failing step. A step that needed its retries records
``attempts`` (the tries made) in its output, so run history shows a step
that succeeded on try 3. Retry is opt-in per step — absent by default and
never auto-enabled on status codes. Logic steps (filter, condition, paths,
delay, for_each, digest) cannot carry the key; the save-time validator
(``registry.validate_autoretry_key``) and the engine both reject that.
"""
import json
import random
import re
import time
from datetime import datetime, timezone


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
    from .matching import _matches_filter

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


def run_chain(workflow_id, steps, event, run_action, *,
              before_action=None, after_action=None, on_action_error=None,
              prefix="", scope=None, step_outputs=None):
    """Run a chain of steps in order.

    ``run_action`` dispatches connector actions as ``run_action(step, event,
    workflow_id, steps=...)`` — ``steps`` is the run's accumulated step
    outputs (``{action_id: {"status": ..., "output": ...}}``; a step that
    failed under ``on_error: continue|run`` also carries ``"error"``), so
    templating runners can reference earlier ones; logic steps are handled
    here.
    Sub-chains (condition branches, loop bodies) recurse into run_chain, so
    every nested step gets the same telemetry hooks and ids. ``scope`` is the
    data predicates evaluate against (loop bodies bind ``{item}`` into it);
    it defaults to the event data. Returns why the chain stopped early
    (``"filtered"``) or None when it ran to the end.
    """
    if step_outputs is None:
        step_outputs = {}
    for index, step in enumerate(steps):
        if not isinstance(step, dict) or not str(step.get("type") or "").strip():
            raise ValueError(f"step {index} needs a type")
        try:
            stop = _run_step(
                workflow_id, step, index, event, run_action,
                before_action=before_action, after_action=after_action,
                on_action_error=on_action_error, prefix=prefix, scope=scope,
                step_outputs=step_outputs,
            )
        except RunSuspended as susp:
            # This chain's remainder resumes after whatever paused inside
            # the step; segments accumulate innermost-first while the
            # suspension unwinds.
            remaining = steps[index + 1:]
            if remaining:
                susp.segments.append(
                    {"steps": remaining, "prefix": prefix, "scope": scope})
            raise
        if stop:
            return stop
    return None


def resume_chain(workflow_id, segments, event, run_action, *,
                 before_action=None, after_action=None, on_action_error=None,
                 step_outputs=None):
    """Continue a suspended run over its ``RunSuspended.segments``.

    A segment is one chain's un-run remainder — ``{"steps": [...], "prefix":
    ..., "scope": ...}`` — or a paused loop — ``{"loop": {"action_id",
    "step", "from", "scope", "item_var", "cap"}}`` — ordered as the run
    would have gone: the chain that paused first, then each enclosing
    chain's tail. The accumulated ``step_outputs`` carry over, so resumed
    steps still see the outputs of everything before the pause. Segments
    run in order and the first stop (``filtered``) skips the ones behind
    it, exactly like the original chain walk. A delay inside the remainder
    suspends again (``RunSuspended`` unwinds with the segments that are
    still left), so any number of pauses chain.
    """
    if step_outputs is None:
        step_outputs = {}
    for position, segment in enumerate(segments):
        try:
            loop = segment.get("loop") if isinstance(segment, dict) else None
            if loop:
                _run_for_each(
                    workflow_id, str(loop.get("action_id") or ""), loop.get("step") or {},
                    event, run_action,
                    before_action=before_action, after_action=after_action,
                    on_action_error=on_action_error, scope=loop.get("scope"),
                    step_outputs=step_outputs, start=int(loop.get("from") or 0),
                )
                continue
            stop = run_chain(
                workflow_id, (segment or {}).get("steps") or [], event, run_action,
                before_action=before_action, after_action=after_action,
                on_action_error=on_action_error,
                prefix=str((segment or {}).get("prefix") or ""),
                scope=(segment or {}).get("scope"),
                step_outputs=step_outputs,
            )
            if stop:
                return stop
        except RunSuspended as susp:
            # The replay paused again: the new suspension carries its own
            # unwound remainder (innermost first) plus every segment still
            # waiting behind this one, or the enclosing chains' tails would
            # silently never run.
            susp.segments.extend(segments[position + 1:])
            raise
    return None


def _run_step(workflow_id, step, index, event, run_action, *,
              before_action, after_action, on_action_error, prefix, scope=None,
              step_outputs=None):
    if step_outputs is None:
        step_outputs = {}
    action_id = _step_id(prefix, step, index)
    quota_gate = None
    if before_action:
        try:
            gate = before_action(workflow_id, action_id, event, step.get("type"))
        except QuotaExceeded as exc:
            # A spent task budget refuses the step before the connector
            # call; the re-raise inside the try below hands it to the
            # ordinary error machinery so the step record, the on_fail /
            # on_error policy and the retry tagging all apply exactly as
            # for a connector failure. Anything else the gate raises
            # (LeaseBusy) keeps its own semantics.
            quota_gate = exc
        else:
            if not gate:
                step_outputs[action_id] = {"status": "skipped"}
                return None
    # The autoretry plan is parsed and validated before the step runs: a bad
    # config is an authoring error, loud even when on_fail would absorb the
    # step's failures (mirroring the lazy on_fail/on_error checks).
    autoretry = _autoretry_plan(step, action_id)
    started = time.monotonic()
    try:
        if quota_gate is not None:
            raise quota_gate
        stop, output, status = _execute_step(
            workflow_id, action_id, step, event, run_action,
            before_action=before_action, after_action=after_action,
            on_action_error=on_action_error, scope=scope,
            step_outputs=step_outputs, autoretry=autoretry,
        )
    except RunSuspended as susp:
        # Suspension is not a failure and no error policy applies: the step
        # closes out ``delayed`` (run history shows the pause) and the
        # unwind continues so every enclosing chain appends its remainder.
        # Every frame the unwind passes through records itself, so the
        # resume can close the pause out ``completed`` again; the innermost
        # frame — the delay step itself — owns ``delay_action_id``.
        susp.paused_ids.append(action_id)
        if susp.delay_action_id is None:
            susp.delay_action_id = action_id
        susp.step_outputs = step_outputs
        step_outputs[action_id] = {"status": "delayed", "output": dict(susp.output)}
        if after_action:
            after_action(workflow_id, action_id, event, output=dict(susp.output),
                         duration_ms=_elapsed(started), status="delayed")
        raise
    except Exception as exc:
        # ``on_fail`` is the step's own failure policy (Zapier's error
        # setting); ``on_error`` is the richer handled-failure policy below.
        # The two are mutually exclusive — checked when on_fail is set.
        on_fail = str(step.get("on_fail") or "").strip().lower()
        if on_fail and on_fail not in ON_FAIL_MODES:
            raise ValueError(
                f"step '{action_id}': on_fail must be one of {', '.join(ON_FAIL_MODES)}"
            ) from exc
        if on_fail and str(step.get("on_error") or "").strip():
            raise ValueError(
                f"step '{action_id}': set on_fail or on_error, not both") from exc
        if on_fail == "continue":
            # Absorb the failure: the error hook still fires (the attempt
            # did fail — the worker releases the step record and the error
            # message lands on it), then the step closes out ``skipped``
            # through the after hook and the chain runs on.
            if on_action_error:
                on_action_error(workflow_id, action_id, event, exc,
                                duration_ms=_elapsed(started))
            message = str(exc) or exc.__class__.__name__
            step_outputs[action_id] = {"status": "skipped", "error": message, "output": {}}
            if after_action:
                after_action(workflow_id, action_id, event, output={},
                             duration_ms=_elapsed(started), status="skipped")
            return None
        mode = str(step.get("on_error") or "halt").strip().lower() or "halt"
        if mode not in ERROR_MODES:
            raise ValueError(
                f"step '{action_id}': on_error must be one of {', '.join(ERROR_MODES)}") from exc
        # Tag the failing workflow for the worker's failure
        # notifications (see engine.notify), plus the innermost failing step
        # so a retry policy can tell an action failure (retryable) from a
        # logic-step problem (a config error fails fast). The innermost
        # chain tags first; outer levels keep it.
        exc.dapier_workflow = workflow_id
        exc.dapier_step_id = getattr(exc, "dapier_step_id", None) or action_id
        exc.dapier_step_type = getattr(exc, "dapier_step_type", None) or str(step.get("type") or "")
        if on_action_error:
            on_action_error(workflow_id, action_id, event, exc, duration_ms=_elapsed(started))
        if mode == "halt":
            raise
        return _handle_error(
            workflow_id, step, action_id, exc, event, run_action,
            before_action=before_action, after_action=after_action,
            on_action_error=on_action_error, scope=scope,
            step_outputs=step_outputs, duration_ms=_elapsed(started),
        )
    step_outputs[action_id] = {"status": status, "output": output or {}}
    if after_action:
        after_action(workflow_id, action_id, event, output=output or {},
                     duration_ms=_elapsed(started), status=status)
    return stop


def _handle_error(workflow_id, step, action_id, exc, event, run_action, *,
                  before_action, after_action, on_action_error, scope=None,
                  step_outputs=None, duration_ms=None):
    """A step failed with ``on_error: continue|run``: record the failure and
    move on instead of aborting the run.

    The failed step lands in the ``steps`` context as
    ``{"status": "failed", "error": ..., "output": {}}`` — the error message
    is what ``{steps.<id>.error}`` renders — and ``after_action`` reports the
    ``failed`` status so run history shows the step like any other. With
    ``run``, ``error_actions`` executes as a sub-chain (ids prefixed
    ``<step>.error``) after the failure is recorded, so the branch can
    template the error. Either way the chain proceeds: a filter inside the
    error branch stops the run quietly, like any filtered branch.
    """
    mode = str(step.get("on_error") or "").strip().lower()
    error_actions = step.get("error_actions")
    if mode == "run" and (not isinstance(error_actions, list) or not error_actions):
        raise ValueError(
            f"step '{action_id}': on_error run needs a non-empty error_actions list") from exc
    message = str(exc) or exc.__class__.__name__
    step_outputs[action_id] = {"status": "failed", "error": message, "output": {}}
    if after_action:
        after_action(workflow_id, action_id, event, output={},
                     duration_ms=duration_ms, status="failed")
    if mode != "run":
        return None
    return run_chain(
        workflow_id, error_actions, event, run_action,
        before_action=before_action, after_action=after_action,
        on_action_error=on_action_error, prefix=f"{action_id}.error",
        scope=scope, step_outputs=step_outputs,
    )


def _execute_step(workflow_id, action_id, step, event, run_action, *,
                  before_action, after_action, on_action_error, scope=None,
                  step_outputs=None, autoretry=None):
    """One step: returns (stop reason, output summary, run-history status)."""
    step_type = str(step.get("type"))
    if step_type == "filter":
        # Mid-chain routing sees earlier outputs too: the rules read the
        # trigger data (or the loop's scope) plus ``steps``, like the
        # delay/digest contexts below.
        return _run_filter(
            step, {**_predicate_scope(event, scope), "steps": step_outputs or {}})
    if step_type == "condition":
        return _run_condition(
            workflow_id, action_id, step, event, run_action,
            before_action=before_action, after_action=after_action,
            on_action_error=on_action_error, scope=scope,
            step_outputs=step_outputs,
        )
    if step_type == "delay":
        context = {**_predicate_scope(event, scope), "steps": step_outputs}
        return _run_delay(step, context)
    if step_type == "paths":
        return _run_paths(
            workflow_id, action_id, step, event, run_action,
            before_action=before_action, after_action=after_action,
            on_action_error=on_action_error, scope=scope,
            step_outputs=step_outputs,
        )
    if step_type == "for_each":
        return _run_for_each(
            workflow_id, action_id, step, event, run_action,
            before_action=before_action, after_action=after_action,
            on_action_error=on_action_error, scope=scope,
            step_outputs=step_outputs,
        )
    if step_type == "digest":
        context = {**_predicate_scope(event, scope), "steps": step_outputs}
        return _run_digest(workflow_id, step, event, context)
    return None, _dispatch_action(
        step, event, workflow_id, run_action, step_outputs, autoretry), "completed"


def _dispatch_action(step, event, workflow_id, run_action, step_outputs, autoretry):
    """One connector action through the caller's dispatch.

    Without an autoretry plan this is today's single call. With one
    (Zapier's autoretry) a failure is retried up to ``attempts`` times with
    exponential backoff; once the retries are exhausted the exception
    propagates untouched — the step's ``on_fail``/``on_error`` policy (and
    past it the workflow-level ``retry`` redrive) sees the failure exactly
    as a single attempt would have. A step that needed its retries records
    ``attempts`` (the tries made) in its output, so run history shows a
    step that succeeded on try 3.
    """
    if not autoretry:
        return run_action(step, event, workflow_id, steps=step_outputs) or {}
    attempts = int(autoretry["attempts"])
    for tries in range(1, attempts + 2):
        try:
            output = run_action(step, event, workflow_id, steps=step_outputs) or {}
        except Exception as exc:
            if tries > attempts:
                raise
            time.sleep(_autoretry_backoff(autoretry, tries, error=exc))
        else:
            output = dict(output)
            output["attempts"] = tries
            return output


def _autoretry_plan(step, action_id):
    """The step's validated autoretry config as a plan dict with defaults
    filled in, or None when the step does not opt in.

    The shape and bounds are validated by ``registry.validate_autoretry_key``
    (the save-time validator shares the checks, so both surfaces reject a
    bad config with the same messages); the engine imports it lazily —
    engine.logic stays import-light. A logic step carrying the key is
    rejected here too.
    """
    config = step.get("autoretry")
    if config is None or (isinstance(config, str) and not config.strip()):
        return None
    from ..connectors import registry

    registry.validate_autoretry_key(step, f"step '{action_id}'", ValueError)
    plan = {**AUTORETRY_DEFAULTS, **config}
    return {"attempts": int(plan["attempts"]),
            "initial_seconds": float(plan["initial_seconds"]),
            "max_seconds": float(plan["max_seconds"])}


def _autoretry_backoff(plan, retry_number, error=None):
    """Seconds to wait before retry ``retry_number`` (1-based): the initial
    delay doubling each retry, capped at ``max_seconds``, plus up to a
    quarter of the delay as jitter so many failing steps do not retry in
    lockstep. A failure carrying an HTTP 429/503 status and the server's
    ``Retry-After`` hint (the webhook/http_request ``HttpError``) waits the
    hinted delay for that step's backoff instead of the doubling one — the
    provider's own pacing, still capped at ``max_seconds``. Bounds-checked
    at save time, the inline sleeps stay small."""
    hinted = _retry_after_hint(error, plan)
    delay = hinted if hinted is not None else min(
        float(plan["max_seconds"]),
        float(plan["initial_seconds"]) * (2 ** (retry_number - 1)))
    return delay + random.uniform(0, delay / 4)


def _retry_after_hint(error, plan):
    """The Retry-After seconds a rate-limited (429/503) failure asks for,
    capped at the plan's max sleep; None when there is no usable hint.

    Only the delay-in-seconds form is honored (the webhook/http actions
    parse nothing else); a hint at or below zero is no hint at all.
    """
    if error is None or getattr(error, "status", None) not in (429, 503):
        return None
    try:
        seconds = float(getattr(error, "retry_after", None))
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return min(float(plan["max_seconds"]), seconds)


def _predicate_scope(event, scope):
    """The data a predicate reads: the loop's scope when inside one."""
    if scope is not None:
        return scope
    return event.get("data") or {}


def _run_filter(step, data):
    rules = predicate_rules(step)
    if rules is None:
        raise ValueError(f"filter '{step.get('id', '')}' needs a when mapping or a field")
    if evaluate_rules(rules, data):
        return None, {"filter": "passed"}, "completed"
    return "filtered", {"filter": "stopped", "when": rules}, "filtered"


def _run_condition(workflow_id, action_id, step, event, run_action, *,
                   before_action, after_action, on_action_error, scope=None,
                   step_outputs=None):
    rules = predicate_rules(step)
    if rules is None:
        raise ValueError(f"condition '{step.get('id', '')}' needs a when mapping or a field")
    passed = evaluate_rules(
        rules, {**_predicate_scope(event, scope), "steps": step_outputs or {}})
    branch = "then" if passed else "else"
    steps = step.get(branch) or []
    if not isinstance(steps, list):
        raise ValueError(f"condition '{action_id}' branch '{branch}' must be a list of steps")
    stop = run_chain(
        workflow_id, steps, event, run_action,
        before_action=before_action, after_action=after_action,
        on_action_error=on_action_error, prefix=f"{action_id}.{branch}",
        scope=scope, step_outputs=step_outputs,
    )
    return stop, {"condition": "passed" if passed else "failed",
                  "branch": branch, "steps": len(steps)}, "completed"


def _run_paths(workflow_id, action_id, step, event, run_action, *,
               before_action, after_action, on_action_error, scope=None,
               step_outputs=None):
    """Zapier-Paths branching: run the first branch whose predicate matches.

    Branches are evaluated in order against the same scope filters read, and
    the first match wins — later predicates are not evaluated. With no match
    the optional ``default`` steps run. A filter inside a branch stops the
    chain quietly, exactly like condition branches.
    """
    branches = step.get("paths")
    if not isinstance(branches, list) or not branches:
        raise ValueError(f"paths '{step.get('id', '')}' needs a non-empty list of paths")
    default_steps = step.get("default")
    if default_steps is not None and not isinstance(default_steps, list):
        raise ValueError(f"paths '{action_id}' default must be a list of steps")
    data = {**_predicate_scope(event, scope), "steps": step_outputs or {}}
    for branch in branches:
        if not isinstance(branch, dict):
            raise ValueError(f"paths '{action_id}': every path must be a mapping")
        rules = predicate_rules(branch)
        if rules is None:
            raise ValueError(
                f"paths '{action_id}': path '{branch.get('label', '')}' "
                "needs a when mapping or a field")
        if not evaluate_rules(rules, data):
            continue
        label = str(branch.get("label") or "").strip() or "path"
        steps = branch.get("actions") or []
        if not isinstance(steps, list):
            raise ValueError(
                f"paths '{action_id}' path '{label}' actions must be a list of steps")
        stop = run_chain(
            workflow_id, steps, event, run_action,
            before_action=before_action, after_action=after_action,
            on_action_error=on_action_error, prefix=f"{action_id}.{label}",
            scope=scope, step_outputs=step_outputs,
        )
        return stop, {"matched": label, "ran": len(steps)}, "completed"
    if default_steps:
        stop = run_chain(
            workflow_id, default_steps, event, run_action,
            before_action=before_action, after_action=after_action,
            on_action_error=on_action_error, prefix=f"{action_id}.default",
            scope=scope, step_outputs=step_outputs,
        )
        return stop, {"matched": None, "ran": len(default_steps)}, "completed"
    return None, {"matched": None, "ran": 0}, "completed"


def _run_delay(step, context):
    """Pause the chain. Up to MAX_DELAY_SECONDS the step sleeps inline; a
    longer pause raises RunSuspended so the run parks and resumes later
    (Zapier's Delay For / Delay Until, unbounded by one invocation)."""
    label = step.get("id", "")
    total, until = _delay_request(step, context, label)
    if until is not None:
        total = max(0.0, until - time.time())
    if total <= MAX_DELAY_SECONDS:
        slept = round(total, 3)
        time.sleep(slept)
        return None, {"delay_seconds": round(total, 3), "slept_seconds": slept}, "completed"
    resume_at = time.time() + total
    raise RunSuspended(resume_at, {
        "delay_seconds": round(total, 3), "resume_at": _iso(resume_at),
        "suspended": True,
    })


DURATION_KEYS = ("days", "hours", "minutes", "seconds")

# Each duration field names its unit; the requested wait is the converted
# sum, so ``minutes: 5`` is five minutes, not five seconds.
DURATION_UNITS = {"days": 86400, "hours": 3600, "minutes": 60, "seconds": 1}


def _delay_request(step, context, label):
    """The requested pause as ``(total_seconds, until_epoch)``.

    Durations combine (``days``/``hours``/``minutes``/``seconds``, numbers,
    each template-renderable against the step context); ``until`` is an ISO
    datetime, also template-renderable, and excludes the duration fields.
    """
    until = step.get("until")
    durations = {}
    for key in DURATION_KEYS:
        if step.get(key) is None:
            continue
        durations[key] = _delay_number(step.get(key), key, label, context)
    if until is not None:
        if any(value > 0 for value in durations.values()):
            raise ValueError(f"delay '{label}': set until or a duration, not both")
        moment = parse_moment(render_value(until, context))
        if moment is None:
            raise ValueError(
                f"delay '{label}': until must be an ISO 8601 datetime "
                "(or a template rendering one)")
        if moment - time.time() > MAX_SUSPENDED_SECONDS:
            # Same ceiling as the duration fields: a suspension may not
            # outlive the executions table's 90-day TTL.
            raise ValueError(
                f"delay '{label}': a delay may not exceed {MAX_SUSPENDED_SECONDS // 86400} days")
        return None, moment
    if not durations:
        raise ValueError(
            f"delay '{label}' needs seconds (or minutes, hours, days, or until) "
            "as a positive number")
    total = round(sum(
        durations[key] * DURATION_UNITS[key] for key in durations), 6)
    if total <= 0:
        raise ValueError(
            f"delay '{label}' needs seconds (or minutes, hours, days, or until) "
            "as a positive number")
    if total > MAX_SUSPENDED_SECONDS:
        raise ValueError(
            f"delay '{label}': a delay may not exceed {MAX_SUSPENDED_SECONDS // 86400} days")
    return total, None


def _delay_number(value, key, label, context):
    rendered = render_value(value, context)
    if isinstance(rendered, str):
        try:
            rendered = float(rendered.strip())
        except ValueError:
            raise ValueError(
                f"delay '{label}': {key} must be a number (or a template rendering one)"
            ) from None
    if isinstance(rendered, bool) or not isinstance(rendered, (int, float)) or rendered < 0:
        raise ValueError(
            f"delay '{label}': {key} must be a number (or a template rendering one)")
    return float(rendered)


def _run_for_each(workflow_id, action_id, step, event, run_action, *,
                  before_action, after_action, on_action_error, scope=None,
                  step_outputs=None, start=0):
    label = step.get("id", "")
    body = step.get("actions")
    if not isinstance(body, list) or not body:
        raise ValueError(f"for_each '{label}' needs at least one step in actions")
    path = str(step.get("list") or "").strip().strip("{}").strip()
    if not path:
        raise ValueError(f"for_each '{label}': needs a list field")
    base = {**_predicate_scope(event, scope), "steps": step_outputs or {}}
    items, found = _lookup(base, path)
    if not found:
        raise ValueError(f"for_each '{label}': list field '{path}' not found in the event data")
    if not isinstance(items, list):
        raise ValueError(f"for_each '{label}': field '{path}' is not a list")
    item_var = str(step.get("item") or "item").strip() or "item"
    cap = max(0, min(int(step.get("max_iterations") or MAX_LOOP_ITERATIONS),
                     MAX_LOOP_ITERATIONS))

    iterated = skipped = 0
    for position, item in enumerate(items[:cap]):
        if position < start:
            # A resumed run continues the loop where the suspension paused it.
            continue
        child_scope = {**base, item_var: item, f"{item_var}_index": position}
        try:
            stop = run_chain(
                workflow_id, render_value(body, child_scope), event, run_action,
                before_action=before_action, after_action=after_action,
                on_action_error=on_action_error, prefix=f"{action_id}[{position}]",
                scope=child_scope, step_outputs=step_outputs,
            )
        except RunSuspended as susp:
            # The loop's remaining iterations resume after the paused
            # iteration's own remainder (already appended by the body's
            # run_chain frame).
            susp.segments.append({"loop": {
                "action_id": action_id, "step": step, "from": position + 1,
                "scope": scope, "item_var": item_var, "cap": cap,
            }})
            raise
        iterated += 1
        # A filter inside the body skips just this iteration; the loop goes on.
        if stop == "filtered":
            skipped += 1
    output = {"iterated": iterated, "items": len(items), "skipped": skipped,
              "truncated": len(items) > cap}
    if start:
        output["resumed_from"] = start
    return None, output, "completed"


def _run_digest(workflow_id, step, event, context):
    """Zapier-Digest batching: accumulate items across runs, flush them later.

    ``accumulate`` appends the step's rendered item(s) — ``item`` (one
    template) and/or ``items`` (a list of templates, appended in order,
    ``item`` first) — to the digest under the required ``key``
    (a literal name: the accumulate and flush runs are different events, so
    the key must not depend on either). Output: ``{"key", "digested"}``
    with the new total count.

    ``shared: true`` moves the digest to a partition every workflow can
    reach — the canonical pattern accumulates in the event's workflow and
    flushes from a schedule-triggered one, which are two workflow ids
    sharing one key. The default scopes the digest to this workflow alone.

    ``flush`` claims and clears the batch in one atomic operation
    (``digests.digests_claim``), so concurrent flushes never double-send,
    and exposes the claimed items to the rest of the run: they ride the
    event data as ``digest`` (``{digest.items}``, ``{digest.count}`` in any
    template — and ``list: digest.items`` for a following for_each). An
    empty digest records ``skipped`` (nothing to send; output
    ``{empty: true, count: 0, items: []}``) and the chain runs on. Either
    way ``digest`` lands on the event with the step's result, so a
    following step can gate on ``{digest.count}``.
    """
    from .actions import digests

    label = step.get("id", "")
    mode = str(step.get("mode") or "accumulate").strip().lower() or "accumulate"
    if mode not in DIGEST_MODES:
        raise ValueError(f"digest '{label}': mode must be one of {', '.join(DIGEST_MODES)}")
    key = str(step.get("key") or "").strip()
    if not key:
        raise ValueError(f"digest '{label}' requires a key")
    scope = digests.SHARED_SCOPE if step.get("shared") else workflow_id

    if mode == "accumulate":
        batch = _digest_batch(step, context, label)
        count = digests.digests_append(scope, key, batch)
        return None, {"key": key, "digested": count}, "completed"

    items = digests.digests_claim(scope, key)
    data = event.get("data")
    if not isinstance(data, dict):
        data = {}
        event["data"] = data
    data["digest"] = {"items": items, "count": len(items)}
    output = {"key": key, "items": items, "count": len(items)}
    if not items:
        output["empty"] = True
        return None, output, "skipped"
    return None, output, "completed"


def _digest_batch(step, context, label):
    """The items one accumulate run appends: ``item`` (one rendered string)
    and/or ``items`` (each template rendered), in that order. At least one
    is required — an accumulate run with nothing to append is a config
    error, not a silent no-op."""
    batch = []
    item = step.get("item")
    if item is not None:
        batch.append(render_value(item, context))
    items = step.get("items")
    if items is not None:
        if not isinstance(items, list) or not items:
            raise ValueError(f"digest '{label}': items must be a non-empty list of templates")
        batch.extend(render_value(entry, context) for entry in items)
    if not batch:
        raise ValueError(
            f"digest '{label}': accumulate needs an item or a non-empty items list")
    return batch
