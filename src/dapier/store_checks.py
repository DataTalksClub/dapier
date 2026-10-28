"""Operator store-integrity counts: the legacy-shaped stored rows that
stored-row compat fallbacks exist to serve.

One count per shape (the audit t1825 follow-ups): execution rows without
``run_id``, processing steps without ``lease_until``, published rows missing
the ``owner`` stamp, connections still carrying the retired ``display_name``,
and poll rows still bound by the retired ``flow`` key. A zero means the
matching fallback is dead code and its removal is safe — these counts are
the evidence those removals run on. Read-only Scans; the API route
(/api/agent/store-check) is operator-gated.
"""
import os

import boto3


def _table(env):
    return boto3.resource("dynamodb").Table(os.environ[env])


def _scan(table, projection, *, names=None):
    """Every row's projected attributes: the stores checked here are small
    except Executions, which carries bulky step payloads the projection
    keeps out of the pages."""
    kwargs = {"ProjectionExpression": projection, "Limit": 500}
    if names:
        kwargs["ExpressionAttributeNames"] = names
    items = []
    while True:
        page = table.scan(**kwargs)
        items.extend(page.get("Items", []))
        start = page.get("LastEvaluatedKey")
        if not start:
            return items
        kwargs["ExclusiveStartKey"] = start


def _non_empty(item, key):
    return bool(str(item.get(key) or "").strip())


def legacy_row_counts(*, executions=None, published=None, connections=None,
                      polls=None):
    executions = executions if executions is not None else _table("EXECUTIONS_TABLE")
    published = published if published is not None else _table("PUBLISHED_WORKFLOWS_TABLE")
    connections = connections if connections is not None else _table("CONNECTIONS_TABLE")
    polls = polls if polls is not None else _table("POLL_TRIGGERS_TABLE")

    steps = _scan(executions, "execution_id, run_id, #st, lease_until",
                  names={"#st": "status"})
    published_rows = _scan(published, "workflow_id, version_of, draft_of, owner")
    live = [item for item in published_rows
            if not item.get("version_of") and not item.get("draft_of")]
    versions = [item for item in published_rows if item.get("version_of")]
    drafts = [item for item in published_rows if item.get("draft_of")]
    connection_rows = _scan(connections, "connection_id, display_name")
    poll_rows = _scan(polls, "poll_id, flow")

    return {
        "executions_without_run_id": sum(1 for item in steps if not item.get("run_id")),
        "executions_processing_without_lease": sum(
            1 for item in steps
            if item.get("status") == "processing" and not item.get("lease_until")),
        "published_live_without_owner": sum(
            1 for item in live if not _non_empty(item, "owner")),
        "published_versions_without_owner": sum(
            1 for item in versions if not _non_empty(item, "owner")),
        "published_drafts_without_owner": sum(
            1 for item in drafts if not _non_empty(item, "owner")),
        "connections_with_display_name": sum(
            1 for item in connection_rows if _non_empty(item, "display_name")),
        "polls_with_flow": sum(1 for item in poll_rows if _non_empty(item, "flow")),
    }
