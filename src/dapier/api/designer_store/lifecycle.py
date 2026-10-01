"""Enable/disable, auto-pause, and deletion — plus the YouTube playlist
resync the save, publish and delete paths share."""
import os
import re
from datetime import datetime, timezone

from ...triggers import failure_counts, published_workflows
from .github import SyncConfigError, SyncError, TOKEN_SECRET_ENV
from .validation import AUTO_PAUSE_KEYS, FILE_PATTERN, _LateBinding
from .listing import api_get, workflow_yaml_text


def _sync_youtube(*, previous=None, workflow=None):
    """Best-effort YouTube WebSub sync after a live definition change: a save
    subscribes newly watched channels, a disable/delete unsubscribes orphaned
    ones (youtube_subscriptions.reconcile). Reconcile never raises and returns
    warning strings for the response payload — a hub outage must not block a
    save, and the renewal schedule re-subscribes what a failed call missed."""
    from ...triggers.intake import youtube_subscriptions

    return youtube_subscriptions.reconcile(previous, workflow)


def api_toggle(source, body, operator=None):
    """Flip a workflow's enabled flag live, then commit the flipped YAML.

    The published store is updated first — the toggle runs on the next event —
    and the git commit follows best-effort so the next deploy agrees. A git
    failure is reported in the payload but does not undo the live toggle.

    Enabling is also the resume verb for the auto-pause trip wire: the
    ``auto_paused`` flag comes off the definition and the failure streak
    resets, so a workflow the engine paused runs again on the next event.
    """
    if not isinstance(body, dict) or not isinstance(body.get("enabled"), bool):
        return 400, {"error": 'body must be {"enabled": true|false}'}
    if not published_workflows.configured():
        return 503, {"error": "published workflows are not configured"}
    if not FILE_PATTERN.fullmatch(source or ""):
        return 400, {"error": f"invalid workflow file name: {source!r}"}
    status, payload = api_get(source)
    if status != 200:
        return status, payload
    workflow = {**payload["workflow"], "enabled": body["enabled"]}
    if body["enabled"]:
        _clear_auto_pause(workflow)
        # The streak that tripped the pause must not trip it again on the
        # first failure after resuming; the reset is best-effort.
        failure_counts.reset(str(workflow["id"]))
    try:
        previous = published_workflows.get_item(workflow["id"])
        published_workflows.publish(workflow, operator=operator, previous=previous, cause="toggle")
    except Exception as exc:
        return 502, {"error": f"publish failed: {exc}"}
    result = {
        "file": source,
        "enabled": body["enabled"],
        "published": True,
    }
    warnings = _sync_youtube(previous=(previous or {}).get("workflow"), workflow=workflow)
    if warnings:
        result["warnings"] = warnings
    try:
        committed = commit_workflow(
            workflow_yaml_text(workflow),
            message=f"designer: {'enable' if body['enabled'] else 'disable'} workflow {workflow['id']}",
        )
        result["commit"] = committed["commit"]
    except (SyncConfigError, SyncError) as exc:
        result["git_sync_error"] = str(exc)
    return 200, result


def _clear_auto_pause(workflow):
    """Drop the auto-pause bookkeeping keys from a definition — the resume
    half of the trip wire: re-enabling is resuming."""
    for key in AUTO_PAUSE_KEYS:
        workflow.pop(key, None)
    return workflow


def api_auto_pause(workflow_id, *, error="", operator=None):
    """Pause a workflow the engine gave up on (Zapier-style auto-disable).

    Called by the worker (engine.worker._auto_pause_on_failure) when a
    workflow's consecutive-failure streak trips: the live definition gains
    ``auto_paused`` plus the moment and the last error — the same keys the
    matcher refuses the way it refuses ``enabled: false`` — and the item is
    re-published (a version record with cause "auto-pause", like a toggle).
    Returns the published item, or None when there is nothing to pause: the
    store is unconfigured, the id is not a managed workflow (a stored
    trigger's synthetic definition lives in its own table), or the flag is
    already set — the pause is stamped once, not re-stamped on every
    past-threshold failure. Runtime state, not an operator edit: no git
    commit — the re-enable toggle (api_toggle) is what writes YAML again,
    and its commit carries the cleared keys.
    """
    if not published_workflows.configured():
        return None
    item = published_workflows.get_item(str(workflow_id))
    workflow = (item or {}).get("workflow")
    if not isinstance(workflow, dict) or not workflow.get("id"):
        return None
    if workflow.get("auto_paused") is True:
        return None
    paused = {**workflow,
              "auto_paused": True,
              "auto_paused_at": datetime.now(timezone.utc).isoformat(),
              "auto_paused_reason": str(error or "")[:500]}
    try:
        return published_workflows.publish(
            paused, operator=str(operator or "auto-pause"),
            previous=item, cause="auto-pause")
    except Exception:
        return None


def api_delete(source, *, operator=None):
    """Delete a workflow everywhere it lives: the live published item goes
    first (the engine stops matching it on the next event), then one atomic
    git tree-delete commit removes workflows/<file> so the next deploy cannot
    resurrect it. Run history is untouched — past runs stay readable, and
    Zapier-style, deleting stops the automation rather than erasing the
    audit trail.

    The ``#v<n>`` version records are kept: they are history, not live
    state, and the versions endpoint keeps answering for a deleted id
    (nothing flagged current) the way past runs do.

    Refuses with 409 while any run of the workflow is still parked on a
    delay: the queue would resume its continuation into a definition that no
    longer exists. The refusal names the blocking run ids. Cancel (or wait
    out) the parked runs first. A workflow that does not exist — including
    one already deleted, deploy lag or not — is a 404, so a delete is safe
    to call twice. A git failure after a successful unpublish reports
    ``git_sync_error`` but still counts as deleted live — the same contract
    as the toggle.

    Derived state: the published item plus, for YouTube push triggers, the
    WebSub subscriptions of the channels only this workflow watched — those
    are unsubscribed best-effort here (a hub failure comes back as
    ``warnings``, never blocks the delete) and the renewal schedule
    reconciles anything missed. EventBridge rules (`dapier-schedule-*`,
    `dapier-poll-*`) belong to the standalone stored schedule/poll triggers
    (their own synthetic workflow ids), not to workflow YAML triggers, so
    there is nothing else to clean up.
    """
    del operator  # recorded by the calling route
    if not FILE_PATTERN.fullmatch(source or ""):
        return 400, {"error": f"invalid workflow file name: {source!r}"}
    status, payload = api_get(source)
    if status != 200:
        return status, payload
    workflow = payload["workflow"]
    workflow_id = str(workflow["id"])
    still_published = bool(published_workflows.configured()
                           and published_workflows.get_item(workflow_id))
    if not still_published and os.environ.get(TOKEN_SECRET_ENV):
        # Not live: the file must still be committed in git, or there is
        # nothing left to delete — a copy only the deploy-time bundle still
        # carries is gone from every managed surface. This is what keeps a
        # second delete a 404 even before the next deploy drops the bundle
        # copy (the same lookup api_get's last resort uses).
        try:
            fetch_workflow(source)
        except KeyError:
            return 404, {"error": f"no such workflow: {source}"}
        except SyncError as exc:
            return 502, {"error": f"git fetch failed: {exc}"}
    from .. import runs

    parked = runs.delayed_runs(workflow_id)
    if parked:
        run_ids = ", ".join(str(run.get("run_id") or "?") for run in parked)
        return 409, {
            "error": (f"workflow {workflow_id} still has {len(parked)} parked run(s) "
                      f"on a delay ({run_ids}) — cancel or wait them out before deleting"),
            "delayed_runs": parked,
        }
    was_published = payload.get("published", False)
    warnings = []
    if still_published:
        try:
            published_workflows.unpublish(workflow_id)
            # The draft dies with the workflow — deleting the live definition
            # and leaving an orphaned draft would resurrect confusion, not
            # the workflow.
            published_workflows.delete_draft(workflow_id)
        except Exception as exc:
            return 502, {"error": f"unpublish failed: {exc}"}
        warnings = _sync_youtube(previous=payload["workflow"], workflow=None)
    result = {
        "file": source,
        "workflow_id": workflow_id,
        "deleted": True,
        "published": False,
        "was_published": was_published,
    }
    if warnings:
        result["warnings"] = warnings
    try:
        committed = commit_delete(source,
                                  message=f"designer: delete workflow {workflow_id}")
        result["commit"] = committed["commit"]
        result["html_url"] = committed.get("html_url")
    except (SyncConfigError, SyncError) as exc:
        result["git_sync_error"] = str(exc)
    return 200, result




# The git-sync seams resolve through the package at call time, so tests
# patching designer_store.<name> reach every caller; the facade re-binds
# the real implementations after the star imports.
_real__sync_youtube = _sync_youtube
commit_workflow = _LateBinding("commit_workflow")
commit_delete = _LateBinding("commit_delete")
fetch_workflow = _LateBinding("fetch_workflow")
_sync_youtube = _LateBinding("_sync_youtube")
