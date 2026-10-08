"""DTC-authenticated agent API: route dispatch over the per-domain modules.

The console's /api/admin/* dispatcher hands every /api/agent/* path to
``route`` below. Handlers live in the sibling modules and are re-exported
here so callers and tests reach them through this package namespace; the
shared request plumbing late-binds through it too (see common._LateBinding).
"""

import re
from urllib.parse import unquote

from . import access, common, connections, designer, device, ops, triggers

# A workflow ref: the managed file name, or a bare id — hook-backed
# workflows (webhook/telegram/... triggers) have no file and are addressed
# by id on the read/duplicate/test routes. The store gates every verb, so
# the id form still refuses writes with its own error.
_WORKFLOW_REF = r"([a-z0-9][a-z0-9._-]*\.yaml|[a-z0-9][a-z0-9_-]{0,62})"
from .common import (  # noqa: F401 - the facade tests patch through
    authenticate, require_operator, reset_rate_limits,
    _is_operator, _json_response)
from .access import *
from .connections import *
from .designer import *
from .device import *
from .ops import *
from .triggers import *

# Shared dependencies the tests (and external callers) reach through this
# package namespace; every binding is facade re-export (noqa: F401).
from ... import audit  # noqa: F401
from .. import overview, runs  # noqa: F401
from ...engine import usage  # noqa: F401
from ...auth import api_tokens, authz, session  # noqa: F401
from ...auth.dtc_auth import verify_id_token  # noqa: F401
from ...connections import tokens  # noqa: F401

# require_operator and authenticate call these through common's namespace;
# route them through the package so tests patching agent.<name> reach every
# caller (the facade imports below keep the real functions for direct use).
common.authenticate = common._LateBinding("authenticate")
common.verify_id_token = common._LateBinding("verify_id_token")
common.audit = common._LateBinding("audit")


def __getattr__(name):
    """Late-bind the shared request plumbing (rate limiting, the Dynamo
    table refs, the gates) for callers and tests that reach them through
    this package namespace."""
    import importlib

    return getattr(importlib.import_module(__package__ + ".common"), name)


def route(event, method, path):
    """Dispatch one ``/api/agent/*`` request.

    An unhandled exception escapes today as a bare API-Gateway 500 whose
    only trace is the Lambda log. DTC-authenticated operators instead get
    the traceback in the response body — the same evidence the log holds,
    reachable from the CLI without log access. Everyone else sees exactly
    the old failure (the exception re-raises).
    """
    try:
        return _route(event, method, path)
    except Exception as exc:
        subject, error = authenticate(event)
        if error or not _is_operator(event, subject):
            raise
        import traceback
        from ... import http
        return http._operator_error_500(event, exc, traceback_text=traceback.format_exc())


def _route(event, method, path):
    if method == "GET" and path == "/api/agent/config":
        return public_config()
    if method == "POST" and path == "/api/agent/device/start":
        return device_start(event)
    if method == "POST" and path == "/api/agent/device/token":
        return device_token(event)
    if method == "POST" and path == "/api/agent/device/confirm":
        return device_confirm(event)
    if method == "POST" and path == "/api/agent/device/refresh":
        return device_refresh(event)
    if method == "POST" and path == "/api/agent/device/revoke":
        return device_revoke(event)
    if method == "POST" and path == "/api/agent/token":
        return issue_token(event)
    if method == "GET" and path == "/api/agent/connections":
        return list_for_caller(event)
    if method == "PUT" and path == "/api/agent/connections":
        return create_connection(event)
    if method == "GET" and path == "/api/agent/connections/resolve":
        return resolve_connection(event)
    match = re.fullmatch(r"/api/agent/connections/([a-z0-9_-]+)", path)
    if match and method == "GET":
        return show_connection(event, match.group(1))
    if match and method == "DELETE":
        return delete_connection(event, match.group(1))
    if match and method == "PUT":
        return update_connection_metadata(event, match.group(1))
    connect_match = re.fullmatch(r"/api/agent/connections/([a-z0-9_-]+)/connect", path)
    if connect_match and method == "POST":
        return start_connect(event, connect_match.group(1))
    discover_match = re.fullmatch(r"/api/agent/connections/([a-z0-9_-]+)/discover", path)
    if discover_match and method == "GET":
        return connections_discover_api(event, discover_match.group(1))
    discover_resource_match = re.fullmatch(
        r"/api/agent/connections/([a-z0-9_-]+)/discover/([a-z0-9_-]+)", path)
    if discover_resource_match and method == "GET":
        return connections_discover_api(event, discover_resource_match.group(1),
                                        resource=discover_resource_match.group(2))
    test_connection_match = re.fullmatch(r"/api/agent/connections/([a-z0-9_-]+)/test", path)
    if test_connection_match and method == "POST":
        return connections_test_api(event, test_connection_match.group(1))
    if method == "POST" and path == "/api/agent/connections/import":
        return import_connection(event)
    if path == "/api/agent/email-triggers" and method == "GET":
        return email_triggers_api(event, method)
    if path == "/api/agent/email-from" and method in ("GET", "POST", "DELETE"):
        return email_from_api(event, method)
    if path == "/api/agent/hook-triggers" and method in ("GET", "PUT", "DELETE"):
        return hook_triggers_api(event, method)
    if path == "/api/agent/schedule-triggers" and method in ("GET", "PUT", "DELETE"):
        return schedule_triggers_api(event, method)
    if path == "/api/agent/poll-triggers" and method in ("GET", "PUT", "DELETE"):
        return poll_triggers_api(event, method)
    if path == "/api/agent/grants" and method in ("GET", "PUT", "DELETE"):
        return grants_api(event, method)
    if path == "/api/agent/tokens" and method in ("GET", "PUT", "DELETE"):
        return tokens_api(event, method)
    if path == "/api/agent/overview" and method == "GET":
        return operator_overview(event)
    if path == "/api/agent/runs" and method == "GET":
        return runs_api(event)
    if path == "/api/agent/agent-tasks" and method == "GET":
        return agent_tasks_api(event)
    if path == "/api/agent/workers" and method == "GET":
        return workers_api(event)
    if method == "POST" and path in (
        "/api/agent/host-jobs/claim", "/api/agent/host-jobs/heartbeat",
        "/api/agent/host-jobs/finish", "/api/agent/host-jobs/attachment",
    ):
        return host_jobs_api(event, path.rsplit("/", 1)[-1])
    if path == "/api/agent/runs/export" and method == "GET":
        return runs_export_api(event)
    if path == "/api/agent/runs/replay-failed" and method == "POST":
        return runs_replay_failed_api(event)
    if path == "/api/agent/usage" and method == "GET":
        return usage_api(event)
    if path == "/api/agent/quota" and method in ("GET", "PUT"):
        return quota_api(event, method)
    if path == "/api/agent/audit/export" and method == "GET":
        return audit_export_api(event)
    if path == "/api/agent/audit" and method == "GET":
        return audit_api(event)
    if path == "/api/agent/errors/summary" and method == "GET":
        return errors_summary_api(event)
    if path == "/api/agent/errors/digest" and method == "POST":
        return errors_digest_api(event)
    if path == "/api/agent/connections/expiry-digest" and method == "POST":
        return connections.expiry_digest_api(event)
    runs_match = re.fullmatch(r"/api/agent/runs/([^/]+)", path)
    if runs_match and method == "GET":
        return runs_api(event, run_id=unquote(runs_match.group(1)))
    runs_replay_match = re.fullmatch(r"/api/agent/runs/([^/]+)/replay", path)
    if runs_replay_match and method == "POST":
        return runs_replay_api(event, unquote(runs_replay_match.group(1)))
    runs_cancel_match = re.fullmatch(r"/api/agent/runs/([^/]+)/cancel", path)
    if runs_cancel_match and method == "POST":
        return runs_cancel_api(event, unquote(runs_cancel_match.group(1)))
    runs_resolve_match = re.fullmatch(r"/api/agent/runs/([^/]+)/resolve", path)
    if runs_resolve_match and method == "POST":
        return runs_resolve_api(event, unquote(runs_resolve_match.group(1)))
    if path == "/api/agent/triggers/inbox" and method == "GET":
        return inbox_api(event)
    if path == "/api/agent/triggers/sample" and method == "GET":
        return trigger_sample_api(event)
    inbox_replay_match = re.fullmatch(r"/api/agent/triggers/inbox/([^/]+)/replay", path)
    if inbox_replay_match and method == "POST":
        return inbox_replay_api(event, unquote(inbox_replay_match.group(1)))
    inbox_match = re.fullmatch(r"/api/agent/triggers/inbox/([^/]+)", path)
    if inbox_match and method == "GET":
        return inbox_api(event, inbox_id=unquote(inbox_match.group(1)))
    if path == "/api/agent/oauth-clients" and method == "GET":
        return oauth_clients_view(event)
    oauth_client_match = re.fullmatch(r"/api/agent/oauth-clients/([a-z]+)", path)
    if oauth_client_match and method == "PUT":
        return oauth_clients_api(event, oauth_client_match.group(1))
    credential_match = re.fullmatch(r"/api/agent/credentials/([a-z]+)", path)
    if credential_match and method == "PUT":
        return credentials_api(event, credential_match.group(1))
    revoke_match = re.fullmatch(r"/api/agent/connections/([a-z0-9_-]+)/tokens", path)
    if revoke_match and method == "DELETE":
        return revoke_connection_tokens(event, revoke_match.group(1))
    if path == "/api/agent/designer/workflows" and method in ("GET", "PUT"):
        return designer_api(event, method)
    if path == "/api/agent/designer/workflows/test" and method == "POST":
        return designer_test_api(event, None)
    if path == "/api/agent/designer/workflows/test-step" and method == "POST":
        return designer_test_step_api(event, None)
    if path == "/api/agent/designer/workflows/test-code" and method == "POST":
        return designer_test_code_api(event, None)
    if path == "/api/agent/designer/workflows/test-code" and method == "POST":
        return designer_test_code_api(event, None)
    if path == "/api/agent/designer/workflows/bulk" and method == "POST":
        return designer_bulk_api(event)
    if path == "/api/agent/designer/workflows/export-all" and method == "GET":
        return designer_export_all_api(event)
    if path == "/api/agent/designer/export" and method == "GET":
        return designer_export_api(event)
    if path == "/api/agent/discover" and method == "POST":
        return discover_samples_api(event)
    designer_match = re.fullmatch(r"/api/agent/designer/workflows/" + _WORKFLOW_REF, path)
    if designer_match and method == "GET":
        return designer_api(event, method, source=designer_match.group(1))
    if designer_match and method == "PUT":
        return designer_toggle_api(event, designer_match.group(1))
    if designer_match and method == "DELETE":
        return designer_delete_api(event, designer_match.group(1))
    designer_tags_match = re.fullmatch(
        r"/api/agent/designer/workflows/([a-z0-9][a-z0-9._-]*\.yaml)/tags", path)
    if designer_tags_match and method == "PUT":
        return designer_tags_api(event, designer_tags_match.group(1))
    designer_folder_match = re.fullmatch(
        r"/api/agent/designer/workflows/([a-z0-9][a-z0-9._-]*\.yaml)/folder", path)
    if designer_folder_match and method == "PUT":
        return designer_folder_api(event, designer_folder_match.group(1))
    designer_test_match = re.fullmatch(
        r"/api/agent/designer/workflows/" + _WORKFLOW_REF + r"/test", path)
    if designer_test_match and method == "POST":
        return designer_test_api(event, designer_test_match.group(1))
    designer_test_step_match = re.fullmatch(
        r"/api/agent/designer/workflows/" + _WORKFLOW_REF + r"/test-step", path)
    if designer_test_step_match and method == "POST":
        return designer_test_step_api(event, designer_test_step_match.group(1))
    designer_test_code_match = re.fullmatch(
        r"/api/agent/designer/workflows/" + _WORKFLOW_REF + r"/test-code", path)
    if designer_test_code_match and method == "POST":
        return designer_test_code_api(event, designer_test_code_match.group(1))
    designer_test_code_match = re.fullmatch(
        r"/api/agent/designer/workflows/" + _WORKFLOW_REF + r"/test-code", path)
    if designer_test_code_match and method == "POST":
        return designer_test_code_api(event, designer_test_code_match.group(1))
    designer_duplicate_match = re.fullmatch(
        r"/api/agent/designer/workflows/" + _WORKFLOW_REF + r"/duplicate", path)
    if designer_duplicate_match and method == "POST":
        return designer_duplicate_api(event, designer_duplicate_match.group(1))
    designer_versions_match = re.fullmatch(
        r"/api/agent/designer/workflows/([a-z0-9][a-z0-9._-]*\.yaml)/versions", path)
    if designer_versions_match and method == "GET":
        return designer_versions_api(event, designer_versions_match.group(1))
    designer_versions_diff_match = re.fullmatch(
        r"/api/agent/designer/workflows/([a-z0-9][a-z0-9._-]*\.yaml)/versions/diff", path)
    if designer_versions_diff_match and method == "GET":
        return designer_versions_diff_api(event, designer_versions_diff_match.group(1))
    designer_rollback_match = re.fullmatch(
        r"/api/agent/designer/workflows/([a-z0-9][a-z0-9._-]*\.yaml)/rollback", path)
    if designer_rollback_match and method == "POST":
        return designer_rollback_api(event, designer_rollback_match.group(1))
    # Draft vs live (G15): a save drafts; these promote or throw the draft.
    designer_publish_match = re.fullmatch(
        r"/api/agent/designer/workflows/([a-z0-9][a-z0-9._-]*\.yaml)/publish", path)
    if designer_publish_match and method == "POST":
        return designer_publish_api(event, designer_publish_match.group(1))
    designer_draft_match = re.fullmatch(
        r"/api/agent/designer/workflows/([a-z0-9][a-z0-9._-]*\.yaml)/draft", path)
    if designer_draft_match and method == "GET":
        return designer_draft_api(event, designer_draft_match.group(1))
    if designer_draft_match and method == "DELETE":
        return designer_discard_api(event, designer_draft_match.group(1))
    designer_draft_diff_match = re.fullmatch(
        r"/api/agent/designer/workflows/([a-z0-9][a-z0-9._-]*\.yaml)/draft/diff", path)
    if designer_draft_diff_match and method == "GET":
        return designer_draft_diff_api(event, designer_draft_diff_match.group(1))
    storage_match = re.fullmatch(r"/api/agent/storage/([^/]+)", path)
    if storage_match and method == "GET":
        return storage_read_api(event, unquote(storage_match.group(1)))
    if storage_match and method == "POST":
        return storage_write_api(event, unquote(storage_match.group(1)))
    if storage_match and method == "DELETE":
        return storage_delete_api(event, unquote(storage_match.group(1)))
    return _json_response(404, {"error": "Not found"})
