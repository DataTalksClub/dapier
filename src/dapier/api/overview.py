"""The operator overview: workflows, executions, connections, credentials."""
import os
from pathlib import Path

import boto3
import yaml

from ..auth import api_tokens
from .. import http
from ..triggers import published_workflows
from . import runs
from ..connections.credentials import CREDENTIAL_SPECS, credential_status
from ..connections.providers import oauth_clients


def _workflows():
    from ..engine import matching

    root = Path(os.environ.get("WORKFLOWS_DIR", Path(__file__).resolve().parents[3] / "workflows"))
    result = {}
    for path in sorted(root.glob("*.yaml")):
        workflow = yaml.safe_load(path.read_text())
        if not isinstance(workflow, dict) or not workflow.get("id") \
                or not matching.workflow_triggers(workflow):
            continue  # not a workflow (e.g. a flows-only file)
        result[str(workflow["id"])] = _workflow_view(workflow, path.name, published=False)
    # Published overrides (designer saves) are the live state; show them even
    # when the deploy pipeline has not picked them up yet.
    if published_workflows.configured():
        for item in published_workflows.load_items():
            workflow = item.get("workflow")
            if not isinstance(workflow, dict) or not workflow.get("id"):
                continue
            result[str(workflow["id"])] = _workflow_view(
                workflow, item.get("file"), published=True)
    return sorted(result.values(), key=lambda workflow: str(workflow["id"]))


def _workflow_view(workflow, source, *, published):
    """One overview row; the primary trigger stays in ``trigger`` for the
    console, with ``triggerCount`` covering multi-trigger workflows and
    ``actions`` showing the resolved chain of a flow-bound workflow."""
    from ..engine import matching

    triggers = matching.workflow_triggers(workflow)
    resolved = matching.resolve_workflow(workflow) or {}
    view = {
        "id": workflow["id"],
        "enabled": workflow.get("enabled", True),
        "trigger": triggers[0] if triggers else {},
        "triggerCount": len(triggers),
        "flow": workflow.get("flow"),
        "source": source,
        "actions": _action_views(resolved.get("actions")),
        "published": published,
    }
    if len(triggers) > 1:
        view["triggers"] = triggers
    return view

def _action_views(actions):
    return [
        {**action, "id": action.get("id", str(index)), "type": action["type"]}
        for index, action in enumerate(actions or [])
    ]

def _workflows_edit_base():
    return os.environ.get(
        "WORKFLOWS_REPO_URL", "https://github.com/DataTalksClub/dapier"
    ).rstrip("/") + "/edit/main/workflows"

def _scan(table_name, limit=50):
    table = boto3.resource("dynamodb").Table(table_name)
    return table.scan(Limit=limit).get("Items", [])

def _credential_status(provider):
    spec = CREDENTIAL_SPECS[provider]
    return {"provider": provider, **credential_status(spec["credential_id"])}

def _oauth_client_status(provider):
    return oauth_clients.status(provider)

def overview():
    executions = sorted(
        _scan(os.environ["EXECUTIONS_TABLE"]),
        key=lambda item: item.get("execution_id", ""),
        reverse=True,
    )
    connections = _scan(os.environ["CONNECTIONS_TABLE"])
    return http._json_response(200, {
        "service": "dapier",
        "region": os.environ.get("AWS_REGION", "eu-west-1"),
        "workflows": _workflows(),
        "workflows_edit_base": _workflows_edit_base(),
        "executions": executions[:25],
        "runs": runs.recent(25),
        "connections": sorted(connections, key=lambda item: item.get("display_name", "")),
        "credentials": [_credential_status(provider) for provider in CREDENTIAL_SPECS],
        "oauth_clients": [_oauth_client_status(provider) for provider in oauth_clients.CANONICAL_PROVIDERS],
        "api_tokens": [api_tokens.public_view(item) for item in api_tokens.list_all()],
    })
