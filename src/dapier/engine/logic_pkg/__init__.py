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

Module layout (the historical single ``engine/logic.py`` is now a shim
re-exporting everything here, so ``from .logic import run_chain`` keeps
working):

    - core:       constants, the error types, and the pure helpers
                  (moment parsing, template lookup/rendering, rule evaluation)
    - controls:   filter, condition, paths, and the delay pause/resume machinery
    - loops:      for_each and digest
    - execution:  one step's run: error modes, autoretry, dispatch
    - chains:     run_chain/resume_chain, the ordered walk and its
                  suspension segments

The recursion between the chain walk and the sub-chains (condition branches,
paths, loop bodies, error branches) is the one deliberate import cycle:
``chains`` imports the lower modules at its top, and they import
``run_chain`` lazily inside the functions that recurse.
"""
from .core import (AUTORETRY_DEFAULTS, CompletedStep, DIGEST_MODES, ERROR_MODES,
                   MAX_DELAY_SECONDS, MAX_LOOP_ITERATIONS, MAX_SUSPENDED_SECONDS,
                   ON_FAIL_MODES, QuotaExceeded, RunSuspended, _TOKEN, _elapsed,
                   _iso, _lookup, _rendered, _step_id, evaluate_rules,
                   parse_moment, predicate_rules, render_value)
from .controls import (DURATION_KEYS, DURATION_UNITS, _delay_number,
                       _delay_request, _predicate_scope, _run_condition,
                       _run_delay, _run_filter, _run_paths)
from .loops import _digest_batch, _run_digest, _run_for_each
from .execution import (_autoretry_backoff, _autoretry_plan, _dispatch_action,
                        _execute_step, _handle_error, _retry_after_hint,
                        _run_step)
from .chains import resume_chain, run_chain
