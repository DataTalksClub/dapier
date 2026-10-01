"""Discovery sample and trigger-sample endpoints."""
from ... import http
import json
from .. import runs
from ...auth import session
from ...connectors import trigger_discovery


def discover_samples(event, operator):
    """Console mirror of the CLI's trigger sample pull (same domain dispatch).

    Zapier's 'pull in sample data': a realistic event envelope for one
    trigger connector — live where the connector can fetch, else the newest
    recorded run, else a documented example. The designer's test panel
    and `dapier triggers sample` land here via their own surfaces.
    """
    try:
        body = http._request_json(event)
    except (ValueError, json.JSONDecodeError) as exc:
        return http._json_response(400, {"error": str(exc) or "Invalid request"})
    status, payload = trigger_discovery.api_discover(body)
    session._audit_event(
        str((body or {}).get("connector") or payload.get("connector") or "unknown"),
        "triggers.sample", operator or "unknown",
        outcome="ok" if status == 200 else "error", error=payload.get("error"))
    return http._json_response(status, payload)


def trigger_sample(event, operator, visible=None):
    """The workflow's own last trigger input, for the inspector's template
    autofill: the newest run's recorded input, else the trigger-discovery
    sample for its connector (runs.api_trigger_sample, shared verbatim with
    the agent route the CLI calls). ``visible`` hides a workflow the caller
    may not see behind the same 404 an unknown one gets."""
    query = event.get("queryStringParameters") or {}
    status, payload = runs.api_trigger_sample(
        query.get("workflow") or query.get("workflow_id"), visible=visible)
    session._audit_event(
        str(query.get("workflow") or query.get("workflow_id") or "unknown"),
        "triggers.sample", operator or "unknown",
        outcome="ok" if status == 200 else "error", error=payload.get("error"))
    return http._json_response(status, payload)

