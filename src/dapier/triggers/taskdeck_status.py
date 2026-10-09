"""The cross-project console's status contract, projected from the schedules.

Relay defines the contract (``docs/contract.md`` in the taskdeck repo) and
polls one read-only endpoint per project. This module is Dapier's answer to
it: the same payload shape, so a console that can read one service reads
them all, extended with what only Dapier knows — how healthy each schedule
is, and how many of them need a human.

Everything here is a projection of what ``schedule_triggers`` already
computes. Its ``public_view`` answers what fires next, what fired last, and
whether that is on time; this module renames those into contract rows and
refuses to guess at the rest. A schedule is never programmed from here, and
no new AWS call is made: a console that can only observe cannot cause an
outage.

What Dapier cannot answer is reported as unknown, not as fine. The worker
Lambda has no heartbeat to age and the event queue is never read for depth,
so both come back null where Relay sends numbers — see ``_worker`` and
``_queue``.
"""

import hmac
import os
from datetime import datetime, timezone

from dapier_cli import __version__

from . import schedule_triggers

CONTRACT_VERSION = 1
PROJECT = "dapier"
TOKEN_ENV = "TASKDECK_STATUS_TOKEN"
# ``schedule_triggers.health`` reports this state for "needs a human".
ATTENTION = "attention"

# No CORS headers are set here, and none are needed. The API's stage-level
# CorsConfiguration (template.yaml) answers the preflight and stamps the
# allow-origin header on every response, and an HTTP API ignores whatever CORS
# headers an integration returns — so headers set in code would be dropped by
# the deployed function while misleading whoever read them. The browser-hosted
# console reads this route through that configuration, which is why
# ``authorization`` sits on the stage's allow-headers list.


# --- the contract payload -------------------------------------------------------

def _items(table_ref):
    """Every schedule trigger, or [] when there is nothing to read.

    A deployment with no schedule table configured has no schedules to
    report, which is an answer rather than an error: the console shows an
    empty schedule list instead of a broken panel. Anything else — a
    table that exists and fails — still raises, because a silently empty
    status is indistinguishable from a healthy quiet one.
    """
    try:
        return schedule_triggers.load_items(table_ref=table_ref)
    except schedule_triggers.TriggerError:
        return []


def _last_success(view):
    """The most recent fire that ran a workflow, or None.

    Manual fires do not count. Run now does run the workflows, but it says
    nothing about the rule — the same reason ``public_view``'s
    ``last_fired_at`` skips them — and a schedule whose EventBridge rule
    stopped firing must not read as current because an operator fired it by
    hand. The history is newest first, so the first match is the most recent.
    """
    for fire in view.get("fires") or []:
        if fire.get("outcome") == schedule_triggers.RAN and not fire.get("manual"):
            return fire.get("at")
    return None


def _max_age(view):
    """How long a schedule may go without firing before it is overdue.

    Its own interval, plus the grace EventBridge gets before Dapier calls a
    fire missed. The grace matters: without it a rule two minutes late reads
    as overdue on every poll, and a consumer that knows nothing about cron
    semantics cannot tell "late" from "broken". Taking the same ten minutes
    ``schedule_triggers`` applies before it calls a schedule late keeps the
    console's red flag on the same trigger as Dapier's own ``attention``
    verdict, so the two never disagree about the same schedule. A schedule
    with no repeating interval (a once-a-year cron) stays null: there is no
    gap to age against.

    One reading the consumer must not take naively: the interval is the
    *typical* gap between fires (the median of the next few), so a weekday
    schedule reads as daily and its Friday-to-Monday gap ages past ``max_age_s``
    over every weekend. Dapier's own verdict does not — ``health`` walks the
    actual expression forward, weekends included — so ``health.state`` is the
    authoritative overdue signal and ``max_age_s`` is the rough number beside
    it, not a second verdict to compare against.
    """
    interval = view.get("interval_seconds")
    if not interval:
        return None
    return int(interval) + int(schedule_triggers.LATE_GRACE.total_seconds())


def _worker():
    """The worker's liveness, which Dapier cannot answer.

    The worker is a Lambda the event queue wakes up. There is no process
    checking in, so there is no heartbeat to age and no running work whose
    silence would mean wedged. ``healthy`` is null rather than true: a
    consumer must be able to tell "nothing is wrong" from "nobody is
    reporting", and Dapier does not report. ``mode`` names the shape of the
    thing doing the work so the empty fields read as a design decision
    rather than a hole in the data.
    """
    return {"mode": "lambda", "last_seen": None, "healthy": None, "stalled_runs": None}


def _queue():
    """The queue fires ride, and nothing about its depth.

    The name comes from this deployment's own environment, so a console can
    say which queue it is looking at. ``pending`` is the contract's own key
    for the depth (``docs/contract.md`` in the Relay repo reads
    ``queue.pending``), and ``depth`` stays beside it as the alias this
    projection's brief named — a console written against either spelling
    reads the same answer. Both are null, and so is the oldest pending age:
    reading them means an SQS call per poll on a read-only endpoint, and
    there is no consumer-lag story here worth one — the worker is woken by
    the queue itself, so a backing-up queue shows up as late fires in the
    schedule rows instead, which is the symptom an operator can act on.
    """
    url = os.environ.get("EVENT_QUEUE_URL", "").strip()
    name = url.rstrip("/").rsplit("/", 1)[-1] if url else None
    return {"name": name, "pending": None, "depth": None, "oldest_pending_age_s": None}


def _schedule_row(view):
    """One schedule as a contract row: Relay's fields, plus Dapier's health.

    ``health`` is carried through whole rather than flattened into a
    boolean, because "the last fire failed" and "nothing listens to it" are
    different problems with different fixes, and a console that shows only
    the boolean sends an operator to the wrong place.
    """
    health = view.get("health") or {}
    next_runs = view.get("next_runs") or []
    return {
        "name": view.get("schedule_id"),
        "cron": view.get("expression"),
        # Null, not computed: a paused schedule's rule is disabled, so it
        # has no next fire to report rather than a stale one.
        "next_run": next_runs[0] if next_runs else None,
        "last_run": view.get("last_fired_at"),
        "last_success": _last_success(view),
        "max_age_s": _max_age(view),
        "enabled": bool(view.get("enabled", True)),
        "health": {"state": health.get("state"), "reason": health.get("reason")},
    }


def collect_status(table_ref=None, *, now=None, workflows=None):
    """Build the full contract payload.

    Named and shaped after Relay's collector so the two read alike. The
    arguments are injection points — a table handle, a clock, an already
    loaded workflow list — never a second code path.
    """
    now = now or datetime.now(timezone.utc)
    if workflows is None:
        workflows = schedule_triggers.load_workflows()
    schedules = [
        _schedule_row(schedule_triggers.public_view(item, now=now, workflows=workflows))
        for item in _items(table_ref)
    ]
    return {
        "contract_version": CONTRACT_VERSION,
        "project": PROJECT,
        # dapier_cli/__init__.py holds the app version, and pyproject.toml
        # mirrors it for the distribution. The Lambda bundle ships the
        # package but no installed distribution metadata, so importlib would
        # have nothing to answer with in the deployed function.
        "version": __version__,
        # schedule_triggers' own ISO shape (a Z suffix), so every time in the
        # payload reads the same way.
        "generated_at": schedule_triggers._iso(now),
        "worker": _worker(),
        "queue": _queue(),
        "schedules": schedules,
        # Dapier's per-schedule verdict, folded into one number so a console
        # can headline "2 of 7 need attention" beside the rows.
        "schedules_attention": sum(1 for row in schedules if row["health"]["state"] == ATTENTION),
        # The contract's run-history fields, answered honestly rather than
        # filled in: a fire is one worker invocation, and the run log that
        # would populate these (durations, steps, per-workflow outcomes) is
        # a different subsystem the console reads through Dapier's own runs
        # views. The empty list is the truth — this projection publishes no
        # runs — and a null failure count says "not measured here" where a
        # zero would say "nothing failed".
        "recent_runs": [],
        "failures_24h": None,
    }


# --- the endpoint ---------------------------------------------------------------

def _authorised(event):
    """Constant-time bearer check against the configured token.

    Mirrors Relay's ``mailing/ops_views.py``: the token is the whole
    credential for a read-only machine GET, so the replay protection a
    signed request would add buys nothing. Compared as bytes because a
    non-ASCII header would make ``compare_digest`` raise.
    """
    expected = os.environ.get(TOKEN_ENV, "")
    if not expected:
        return False
    supplied = _bearer(event)
    if not supplied:
        return False
    return hmac.compare_digest(supplied.encode(), expected.encode())


def _bearer(event):
    """The request's bearer token, matched case-insensitively (API Gateway
    keeps whatever case the client sent)."""
    headers = event.get("headers") or {}
    value = next(
        (str(item) for key, item in headers.items() if key.lower() == "authorization"), "")
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer":
        return ""
    return token.strip()


def api_status(event, *, table_ref=None, now=None, workflows=None):
    """``GET /internal/ops/status``: the payload, or the 404 it answers.

    Both an unset and a wrong token get 404, never 401: a caller learns
    nothing about whether this endpoint exists on this host, which is the
    behaviour Relay's ops view chose and the reason this route keeps its own
    auth instead of sitting behind the operator gate.
    """
    if not _authorised(event):
        return 404, {"error": "not found"}
    return 200, collect_status(table_ref, now=now, workflows=workflows)
