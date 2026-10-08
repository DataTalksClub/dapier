"""Webhook activity: what each hook starts, what it received, and a test
request — the data behind the console's Hooks tab and ``dapier hooks
deliveries|delivery|test``.

Nothing here stores anything new. Deliveries are the trigger inbox's
webhook rows (every queued or inline webhook event is recorded there with
its request record — redacted headers, size, test flag), each closed out
with the workflows it matched; run outcomes come from run history by the
engine's run id (``<workflow>:<event id>``). The workflows a hook starts are
read from the published definitions whose webhook trigger filters on that
hook. Send test request drives the real intake in-process
(``router._webhook_hook``) with the hook's own credential, so the test is
verified, deduplicated, published and logged exactly like an outside call.
"""

import json
import logging

from ..triggers import hook_triggers, inbox, published_workflows
from ..engine import matching

logger = logging.getLogger(__name__)

WEBHOOK_CONNECTOR = "webhook"
# Hook kinds whose intake stamps ``data.hook`` on the events it publishes,
# by the connector those events carry. YouTube rides a shared WebSub
# callback keyed by channel, so its deliveries are not per-hook.
HOOK_CONNECTORS = {"webhook": "webhook", "telegram": "telegram", "mailchimp": "mailchimp"}
# Run lookups are one GSI query per (delivery, matched workflow); a page
# never pays for more than this many.
MAX_RUN_LOOKUPS = 60
TEST_SAMPLE = {"event": "test", "message": "Hello from Dapier",
               "sent_by": "Send test request"}


# --- which workflows a hook starts -------------------------------------

def _hook_rule_matches(rule, hook_id):
    try:
        return matching._matches_filter(hook_id, rule)
    except (KeyError, TypeError):
        return False


def bound_workflows(hook_id, workflows, kind="webhook"):
    """The published workflows a delivery to ``hook_id`` can start: a
    trigger on the hook kind's connector (webhook: ``request.received``)
    whose ``hook`` filter accepts the hook (or that has none — it takes
    every hook of that kind). ``conditional`` flags a trigger with further
    filters, which the payload must also pass."""
    connector = HOOK_CONNECTORS.get(kind)
    found = []
    for workflow in workflows or []:
        for trigger in matching.workflow_triggers(workflow):
            if trigger.get("connector") != connector or not connector:
                continue
            if kind == "webhook" and trigger.get("event") != hook_triggers.WEBHOOK_EVENT:
                continue
            filters = trigger.get("filters") or {}
            if "hook" in filters and not _hook_rule_matches(filters["hook"], hook_id):
                continue
            found.append({
                "id": workflow.get("id"),
                "description": workflow.get("description") or "",
                "enabled": bool(workflow.get("enabled", True)) and not workflow.get("auto_paused"),
                "conditional": any(key != "hook" for key in filters),
                "any_hook": "hook" not in filters,
            })
            break
    return found


def attach_workflows(hooks, workflows=None):
    """Stamp each hook in a hook list with the workflows it starts.

    Best-effort like the connections ``used_in`` labels: a storage hiccup
    leaves the list without the field rather than failing it."""
    if workflows is None:
        try:
            workflows = (published_workflows.load_workflows()
                         if published_workflows.configured() else [])
        except Exception:  # noqa: BLE001 — labels must never break the list
            logger.warning("hook workflow lookup failed")
            return hooks
    for hook in hooks or []:
        kind = hook.get("kind", "webhook")
        if kind in HOOK_CONNECTORS:
            hook["workflows"] = bound_workflows(hook.get("hook_id"), workflows, kind)
    return hooks


# --- deliveries ----------------------------------------------------------

def _run_outcome(workflow_id, event_id):
    """The run a matched workflow made of this event, summarised, or None
    when run history has no row for it (yet)."""
    from . import runs

    run_id = f"{workflow_id}:{event_id}"
    try:
        items = runs._run_items(run_id)
    except Exception:  # noqa: BLE001 — history is enrichment, not the row
        return None
    if not items:
        return None
    summary = runs.run_summary(run_id, items)
    return {key: summary.get(key) for key in (
        "run_id", "workflow_id", "status", "error", "failed_step",
        "started_at", "finished_at", "duration_ms")}


def outcome(event, runs_seen):
    """``(code, sentence)``: what became of the delivery, in plain words.

    The inbox status says whether a workflow claimed it; the runs say how
    that went. Codes: processing, failed, no_workflow, ignored, running,
    run_failed, filtered, ran."""
    status = event.get("status")
    matched = event.get("matched") or []
    if status == inbox.RECEIVED:
        return "processing", "Accepted; waiting for the worker to pick it up"
    if status == inbox.FAILED:
        # The error itself rides the row's ``error`` field (shown in the
        # delivery detail); the sentence stays one glanceable line.
        return "failed", "Failed before a workflow finished"
    if status == inbox.IGNORED:
        return "ignored", "Ignored; no workflow ran"
    if not matched:
        return "no_workflow", "No workflow matched this delivery"
    statuses = {run.get("status") for run in runs_seen if run}
    names = ", ".join(matched)
    if "failed" in statuses:
        failed = next(run for run in runs_seen if run and run.get("status") == "failed")
        return "run_failed", f"Run failed in {failed.get('workflow_id')}"
    if statuses & {"processing", "delayed"}:
        return "running", f"Running {names}"
    if statuses and statuses <= {"filtered"}:
        return "filtered", f"Stopped by a filter in {names}"
    return "ran", f"Ran {names}"


def _body_size(event):
    data = event.get("data") or {}
    payload = data.get("body") if "body" in data else data
    try:
        return len(json.dumps(payload, default=str).encode())
    except (TypeError, ValueError):
        return None


def delivery_view(event, *, with_runs=True, budget=None, detail=False):
    """One delivery row: inbox status, run outcomes, request metadata.

    ``budget`` is a one-item list counting down the page's run lookups.
    ``detail`` adds the request headers and the payload."""
    event_id = event.get("inbox_id")
    matched = event.get("matched") or []
    runs_seen = []
    if with_runs:
        for workflow_id in matched:
            if budget is not None:
                if budget[0] <= 0:
                    break
                budget[0] -= 1
            runs_seen.append(_run_outcome(workflow_id, event_id))
    request = event.get("request") or {}
    data = event.get("data") or {}
    code, text = outcome(event, runs_seen)
    view = {
        "delivery_id": event_id,
        "hook": data.get("hook") or event.get("source"),
        "kind": event.get("connector"),
        "event": event.get("event"),
        "received_at": event.get("received_at"),
        "processed_at": event.get("processed_at"),
        "status": event.get("status"),
        "outcome": code,
        "outcome_text": text,
        "matched": matched,
        "runs": [run for run in runs_seen if run],
        "response_status": request.get("response_status"),
        "size_bytes": request.get("size_bytes", _body_size(event)),
        "content_type": data.get("content_type"),
        "test": bool(request.get("test")),
        "replay": str(event_id or "").startswith("inbox-replay-"),
        "error": event.get("error"),
    }
    if detail:
        view["headers"] = request.get("headers") or {}
        # A webhook's payload is the caller's body; Telegram and Mailchimp
        # deliveries keep the parsed update as their event data.
        view["body"] = data.get("body") if event.get("connector") == WEBHOOK_CONNECTOR else data
        view["query"] = data.get("query") or {}
        view["truncated"] = bool(isinstance(data, dict) and data.get("truncated"))
    return view


def api_deliveries(hook=None, limit=25, next_token=None, visible=None):
    """Recent hook deliveries (webhook, Telegram, Mailchimp), newest
    first, optionally for one hook."""
    hook = str(hook or "").strip() or None
    try:
        status, payload = inbox.api_list(tuple(HOOK_CONNECTORS.values()), limit,
                                         next_token=next_token, visible=visible, hook=hook)
    except inbox.InboxError as exc:
        return 503, {"error": str(exc)}
    if status != 200:
        return status, payload
    budget = [MAX_RUN_LOOKUPS]
    return 200, {
        "hook": hook,
        "deliveries": [delivery_view(event, budget=budget) for event in payload["events"]],
        "paging": payload.get("paging") or {},
    }


def api_delivery(delivery_id, visible=None):
    """One delivery with its request headers, payload and run outcomes."""
    try:
        status, payload = inbox.api_get(delivery_id, visible=visible)
    except inbox.InboxError as exc:
        return 503, {"error": str(exc)}
    if status != 200:
        return status, payload
    event = payload["event"]
    if event.get("connector") not in HOOK_CONNECTORS.values():
        return 404, {"error": "Hook delivery not found"}
    return 200, {"delivery": delivery_view(event, detail=True)}


# --- send test request ---------------------------------------------------

def api_send_test(name, data=None):
    """POST a sample payload at a webhook hook through the real intake.

    Signed with the hook's secret, or carrying its bearer token, and marked
    ``x-dapier-test: 1`` so the delivery log labels it. It is a real
    delivery: matched workflows run. Returns the request sent (credential
    redacted), the response the intake gave, and the delivery id to look
    up in the log."""
    name = str(name or "").strip().lower()
    if not name:
        return 400, {"error": "name is required"}
    try:
        item = hook_triggers.get_item(name)
    except hook_triggers.TriggerError as exc:
        return 400, {"error": str(exc)}
    if not item:
        return 404, {"error": f"no hook named '{name}'"}
    if item.get("kind", "webhook") != "webhook":
        return 400, {"error": f"'{name}' is a {item.get('kind')} hook; the provider sends its "
                              "requests, so only webhook hooks take a test request"}
    if not item.get("enabled", True):
        return 409, {"error": f"'{name}' is disabled; enable it before sending a test request"}
    payload = TEST_SAMPLE if data is None else data
    if not isinstance(payload, (dict, list)):
        return 400, {"error": "data must be a JSON object or array"}
    from . import router

    body = json.dumps(payload).encode()
    if len(body) > router.MAX_HOOK_BODY_BYTES:
        return 413, {"error": "data is larger than a hook accepts"}
    headers = {
        "content-type": "application/json",
        "user-agent": "Dapier-Test/1.0",
        hook_triggers.TEST_HEADER: "1",
    }
    if item.get("secret"):
        headers[hook_triggers.signature_header_for(item)] = (
            hook_triggers.SIGNATURE_PREFIX + hook_triggers.signature_for(item["secret"], body))
    else:
        headers["authorization"] = f"Bearer {item.get('token') or ''}"
    request_event = {"headers": headers, "body": body.decode(), "isBase64Encoded": False}
    response = router._webhook_hook(request_event, item["hook_id"], body, {})
    raw = response.get("body")
    try:
        answer = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        answer = raw
    delivery_id = answer.get("event_id") if isinstance(answer, dict) else None
    return 200, {
        "hook_id": item["hook_id"],
        "url": item.get("url") or hook_triggers.hook_url("webhook", item["hook_id"]),
        "request": {
            "headers": hook_triggers.delivery_record(headers, body)["headers"],
            "body": payload,
        },
        "response": {"status": response.get("statusCode"), "body": answer},
        "delivery_id": delivery_id,
    }
