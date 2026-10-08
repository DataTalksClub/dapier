"""The operator overview: workflows, executions, connections, credentials."""
import os

import boto3

from ..auth import api_tokens, visibility
from .. import http
from ..triggers import connection_usage, email_from, email_triggers, published_workflows
from ..engine import usage
from . import runs
from ..connections import records as connection_records
from ..connections.credentials import (
    CREDENTIAL_SPECS,
    credential_status,
    get_credential_record,
)
from ..connections.providers import oauth_clients


def _workflows(visible=None, owners=None):
    from ..triggers import failure_counts

    counts = failure_counts.all_counts()
    result = {}
    if published_workflows.configured():
        for item in published_workflows.load_items():
            workflow = item.get("workflow")
            if not isinstance(workflow, dict) or not workflow.get("id"):
                continue
            if visible is not None and not visible.owner_visible(
                    visibility.owner_of_item(item)):
                continue
            result[str(workflow["id"])] = _workflow_view(
                workflow, item.get("file"), published=True,
                failures=counts.get(str(workflow["id"]), 0))
    return sorted(result.values(), key=lambda workflow: str(workflow["id"]))


def _workflow_view(workflow, source, *, published, failures=0):
    """One overview row; the primary trigger stays in ``trigger`` for the
    console, with ``triggerCount`` covering multi-trigger workflows. The
    auto-pause fields are engine-stamped runtime state (designer_store.
    api_auto_pause sets them; re-enabling clears them) and ``failures`` is
    the live consecutive-failure count behind them."""
    from ..engine import matching

    triggers = matching.workflow_triggers(workflow)
    view = {
        "id": workflow["id"],
        "enabled": workflow.get("enabled", True),
        "description": str(workflow.get("description") or ""),
        "trigger": triggers[0] if triggers else {},
        "triggerCount": len(triggers),
        "source": source,
        "actions": _action_views(workflow.get("actions")),
        "published": published,
        # Zapier-style labels (designer_store._tags_of semantics): [] for
        # hand-written YAML that carries something else under ``tags``.
        "tags": [
            str(tag).strip() for tag in (workflow.get("tags") or [])
            if isinstance(tag, str) and str(tag).strip()
        ] if isinstance(workflow.get("tags"), list) else [],
        # Zapier-style flat folder (designer_store._folder_of semantics): ""
        # for hand-written YAML that carries something else under ``folder``.
        "folder": str(workflow["folder"]).strip()
        if isinstance(workflow.get("folder"), str) else "",
        # Zapier-style trip wire: paused by the engine after consecutive
        # failed runs, with the moment and the last error; re-enabling is
        # the resume verb. ``failures`` is the live streak count.
        "auto_paused": workflow.get("auto_paused") is True,
        "auto_paused_at": str(workflow.get("auto_paused_at") or ""),
        "auto_paused_reason": str(workflow.get("auto_paused_reason") or ""),
        "failures": int(failures or 0),
    }
    if len(triggers) > 1:
        view["triggers"] = triggers
    return view


def _workflow_matches(view, query):
    """Case-insensitive ?q= match: workflow id, description, the trigger's
    connector and event, the action step types, the tags, and the folder."""
    text = " ".join(
        [str(view.get("id") or ""), str(view.get("description") or ""),
         str((view.get("trigger") or {}).get("connector") or ""),
         str((view.get("trigger") or {}).get("event") or "")]
        + [str(action.get("type") or "") for action in view.get("actions") or []]
        + [str(tag) for tag in view.get("tags") or []]
        + [str(view.get("folder") or "")]
    )
    return query in text.lower()

def _action_views(actions):
    return [
        {**action, "id": action.get("id", str(index)), "type": action["type"]}
        for index, action in enumerate(actions or [])
    ]

def _scan(table_name, limit=50):
    table = boto3.resource("dynamodb").Table(table_name)
    return table.scan(Limit=limit).get("Items", [])

def _credential_status(provider):
    spec = CREDENTIAL_SPECS[provider]
    return {"provider": provider, **credential_status(spec["credential_id"])}


def _connection_views(items):
    """Connections as ``public_view`` metadata plus token health.

    The console flags connections whose stored token is past its expiry
    ("needs reconnection"), so each row carries ``health`` and
    ``token_expires_at`` — read from the credentials store, never the
    secrets themselves. A store hiccup degrades to the status-only view
    rather than blocking the console.
    """
    views = []
    for item in items:
        try:
            record = get_credential_record(
                connection_records.credential_id_for(item.get("connection_id")))
            value = record.get("value")
            stored = value if isinstance(value, dict) else {}
        except Exception:  # noqa: BLE001 — health is best-effort, never block the console
            stored = {}
        views.append(connection_records.public_view(item, stored))
    from ..connections import refs as connection_refs
    return connection_refs.with_refs(views)

def _oauth_client_status(provider):
    return oauth_clients.status(provider)

def _email_triggers():
    """Email entry points projected from workflows, with explicit read failures."""
    from ..triggers.email_routes import inventory
    try:
        return inventory()
    except Exception:
        return {"domain": email_triggers.trigger_domain(), "addresses": [],
                "subscriptions": [], "watchers": [],
                "error": "Email configuration could not be loaded. Refresh to retry."}

def _email_from():
    """The shared sender list. Empty when the table is not configured."""
    try:
        return email_from.api_list()[1]["addresses"]
    except Exception:  # noqa: BLE001 — the overview must render without the table
        return []


def _usage(visible=None, owners=None):
    """The 3-month usage block; empty when the rollup table is not wired.

    ``visible`` read-filters the per-workflow rows like the usage endpoint
    (the account-wide quota block below is not workflow-owned and stays
    whole for everyone)."""
    if not os.environ.get("TASK_USAGE_TABLE"):
        return []
    rows = usage.api_usage(3)[1].get("usage", [])
    if visible is not None:
        rows = [row for row in rows
                if visible.workflow_visible(row.get("workflow_id"), owners or {})]
    return rows


def _quota():
    """The monthly task budget; None when the rollup table is not wired."""
    if not os.environ.get("TASK_USAGE_TABLE"):
        return None
    return usage.quota_status()


def overview(event=None, visible=None):
    """The operator overview. ``?section=`` reads only the requested block;
    omitting it preserves the complete response. ``?q=`` filters the workflows
    list (same match
    text as the designer list: id, description, trigger, action types, tags,
    folder), ``?tag=`` narrows to workflows carrying that tag, and
    ``?folder=`` narrows to workflows sitting in that folder (both
    case-insensitive). ``workflow_tags`` and ``workflow_folders`` aggregate
    the distinct tags and folders in use (computed before both filters), the
    lists the console's filter dropdowns offer.

    ``visible`` (an auth.visibility.Visibility, None = unrestricted) applies
    the G17 Phase 2 read filter to every workflow-tied block: the workflow
    list (owner on the item), the executions and runs blocks and the usage
    rows (owner resolved from the published store via workflow_id; a row
    whose workflow is gone stays visible). Connections, credentials, tokens,
    and the trigger registries are not workflow-owned and are untouched."""
    query = (event or {}).get("queryStringParameters") or {}
    sections = {"workflows", "activity", "usage", "connections", "credentials", "tokens", "emails"}
    section = query.get("section")
    if section is not None and section not in sections:
        return http._json_response(400, {"error": "Unknown overview section"})
    selected = {section} if section is not None else sections
    payload = {
        "service": "dapier",
        "region": os.environ.get("AWS_REGION", "eu-west-1"),
    }
    owners = visibility.owners_for(visible) if selected & {"workflows", "activity", "usage"} else None
    if "workflows" in selected:
        workflows = _workflows(visible, owners)
        workflow_tags = sorted({str(tag) for view in workflows for tag in view.get("tags") or []})
        workflow_folders = sorted({str(view.get("folder") or "").strip() for view in workflows
                                   if str(view.get("folder") or "").strip()})
        search = str(query.get("q") or "").strip().lower()
        if search:
            workflows = [view for view in workflows if _workflow_matches(view, search)]
        tag = str(query.get("tag") or "").strip().lower()
        if tag:
            workflows = [view for view in workflows
                         if tag in {str(existing).lower() for existing in view.get("tags") or []}]
        folder = str(query.get("folder") or "").strip().lower()
        if folder:
            workflows = [view for view in workflows
                         if str(view.get("folder") or "").strip().lower() == folder]
        payload.update(workflows=workflows, workflow_tags=workflow_tags,
                       workflow_folders=workflow_folders)
    if "activity" in selected:
        # Newest first by the moment each step actually started — execution_id
        # is ``workflow:action:event``, so its string order is not chronological.
        executions = sorted(
            (item for item in _scan(os.environ["EXECUTIONS_TABLE"])
             if visible is None or visible.workflow_visible(
                 item.get("workflow_id"), owners)),
            key=lambda item: (str(item.get("started_at") or ""),
                              str(item.get("execution_id") or "")),
            reverse=True,
        )
        payload.update(executions=executions[:25], runs=runs.recent(25, visible=visible))
    if "usage" in selected:
        payload.update(usage=_usage(visible, owners), quota=_quota())
    if "connections" in selected:
        # ``used_in`` (which workflows and hook triggers reference each
        # connection) rides along so the console snapshot matches the paged
        # list — that map is also what flags a connection as safe to delete.
        connections = connection_usage.attach(
            _connection_views(_scan(os.environ["CONNECTIONS_TABLE"])))
        payload["connections"] = sorted(connections, key=lambda item: item.get("display_name", ""))
    if "credentials" in selected:
        payload.update(
            credentials=[_credential_status(provider) for provider in CREDENTIAL_SPECS],
            oauth_clients=[_oauth_client_status(provider) for provider in oauth_clients.CANONICAL_PROVIDERS],
        )
    if "tokens" in selected:
        payload["api_tokens"] = [api_tokens.public_view(item) for item in api_tokens.list_all()]
    if "emails" in selected:
        payload.update(email_triggers=_email_triggers(), email_from=_email_from())
    return http._json_response(200, payload)
