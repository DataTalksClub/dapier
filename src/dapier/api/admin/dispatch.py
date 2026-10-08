"""The /api/admin/* dispatcher: one small router per domain.

The console's routes live in the sibling modules; :func:`route` runs the
auth gate, then tries each domain router in order. Every router has the
same shape — the original branch chain, verbatim, returning the first
match's response or ``None`` — so the branch order and resolution style
(``routes.<handler>`` at call time, so tests can patch the hub) are
unchanged.
"""
import re
from urllib.parse import unquote

from ... import http
from ...auth import authz, session, visibility
from ...connections import oauth_flow
from ...connections.oauth_flow import oauth_start
from .. import agent, overview
from . import login, routes  # noqa: F401 (resolved via the module ref)


def _read_scope(payload):
    """G17 visibility for the owned-workflow surfaces, built from the same
    effective-role verdict the gate above just applied: operators (and
    admins) see everything and may write anything, everyone else is
    owner-scoped to their subject. Phase 2 filters the reads (designer
    list, overview, runs, inbox, usage) with it; Phase 3 gates the
    workflow WRITE routes with it (the handlers call ensure_can_write).
    Computed only on the routes that scope, not on every request."""
    return visibility.for_session(payload)


def _pre_routes(event, method, path):
    """The routes answered before the operator gate: login/logout, the
    OAuth callback, the agent delegation, the session and CSRF checks,
    and ``/api/admin/me``. Returns the response, or ``None`` to run the
    operator gate and the domain routers."""
    if method == "GET" and path == "/auth/login":
        return login.auth_login(event)
    if method == "GET" and path == "/auth/callback":
        return login.auth_callback(event)
    if method == "GET" and path == "/auth/error":
        return login.auth_error()
    if method == "GET" and path == "/auth/logout":
        return login.auth_logout()
    if method == "GET" and path == "/oauth/callback":
        return oauth_flow.oauth_callback(event)
    if path.startswith("/api/agent/"):
        return agent.route(event, method, path)
    if not session.authenticated(event):
        return http._json_response(401, {"error": "Authentication required"})
    if method == "GET" and path == "/api/admin/me":
        payload = session._session_payload(event)
        return http._json_response(200, {
            "username": payload.get("sub"),
            "operator": authz.is_operator(payload),
        })
    if not session._csrf_ok(event, method):
        return http._json_response(403, {"error": "Cross-site request rejected"})
    return None


def _gate(event, method, path):
    """Run :func:`_pre_routes`, then the operator gate. Returns
    ``(response, operator_payload, operator_subject)``; ``response`` is
    not None when the request is answered or rejected before the operator
    routes run."""
    response = _pre_routes(event, method, path)
    if response is not None:
        return response, None, None
    operator_payload, operator_error = session.require_operator(event)
    if operator_error:
        return operator_error, None, None
    return None, operator_payload, session.subject_fallback(operator_payload)


def _route_overview_runs(event, method, path, operator_payload, operator_subject):
    """The overview, audit trail, and run endpoints."""
    if method == "GET" and path == "/api/admin/overview":
        return overview.overview(event, visible=_read_scope(operator_payload))
    if method == "GET" and path == "/api/admin/audit/export":
        return routes.export_audit(event, operator_subject)
    if method == "GET" and path == "/api/admin/audit":
        return routes.list_audit(event)
    if method == "GET" and path == "/api/admin/runs":
        return routes.list_runs(event, visible=_read_scope(operator_payload))
    if method == "GET" and path == "/api/admin/runs/export":
        return routes.export_runs(event, operator_subject,
                                  visible=_read_scope(operator_payload))
    if method == "POST" and path == "/api/admin/runs/replay-failed":
        return routes.replay_failed_runs(event, operator_subject)
    return None


def _route_usage_errors(event, method, path, operator_payload, operator_subject):
    """Usage, quota, error digests, and the per-run lookups."""
    if method == "GET" and path == "/api/admin/usage":
        return routes.usage(event, visible=_read_scope(operator_payload))
    if method == "GET" and path == "/api/admin/quota":
        return routes.quota_get(event)
    if method == "PUT" and path == "/api/admin/quota":
        return routes.quota_save(event, operator_subject)
    if method == "GET" and path == "/api/admin/errors/summary":
        return routes.errors_summary(event, visible=_read_scope(operator_payload))
    if method == "POST" and path == "/api/admin/errors/digest":
        return routes.send_error_digest(event, operator_subject)
    run_replay_match = re.fullmatch(r"/api/admin/runs/([^/]+)/replay", path)
    if method == "POST" and run_replay_match:
        return routes.replay_run(unquote(run_replay_match.group(1)),
                                 operator_subject, event)
    run_cancel_match = re.fullmatch(r"/api/admin/runs/([^/]+)/cancel", path)
    if method == "POST" and run_cancel_match:
        return routes.cancel_run(unquote(run_cancel_match.group(1)), operator_subject)
    run_resolve_match = re.fullmatch(r"/api/admin/runs/([^/]+)/resolve", path)
    if method == "POST" and run_resolve_match:
        return routes.resolve_run(unquote(run_resolve_match.group(1)),
                                  operator_subject, event)
    run_match = re.fullmatch(r"/api/admin/runs/([^/]+)", path)
    if method == "GET" and run_match:
        return routes.get_run(unquote(run_match.group(1)),
                              visible=_read_scope(operator_payload))
    return None


def _route_inbox(event, method, path, operator_payload, operator_subject):
    """The trigger inbox and the trigger sample pull."""
    if method == "GET" and path == "/api/admin/triggers/inbox":
        return routes.list_inbox(event, visible=_read_scope(operator_payload))
    if method == "GET" and path == "/api/admin/triggers/sample":
        return routes.trigger_sample(event, operator_subject,
                                     visible=_read_scope(operator_payload))
    inbox_replay_match = re.fullmatch(r"/api/admin/triggers/inbox/([^/]+)/replay", path)
    if method == "POST" and inbox_replay_match:
        return routes.replay_inbox_event(unquote(inbox_replay_match.group(1)), operator_subject)
    inbox_match = re.fullmatch(r"/api/admin/triggers/inbox/([^/]+)", path)
    if method == "GET" and inbox_match:
        return routes.get_inbox_event(unquote(inbox_match.group(1)),
                                      visible=_read_scope(operator_payload))
    return None


def _route_connections(event, method, path, operator_payload, operator_subject):
    """Credentials, OAuth clients, the connection save/list
flows, discovery, and the health check."""
    if method == "PUT" and path.startswith("/api/admin/credentials/"):
        return routes.save_credential(path.rsplit("/", 1)[1], event)
    if method == "GET" and path == "/api/admin/oauth-clients":
        return routes.oauth_clients_view()
    if method == "PUT" and path.startswith("/api/admin/oauth-clients/"):
        return routes.save_oauth_client(path.rsplit("/", 1)[1], event)
    if method == "PUT" and path == "/api/admin/connections":
        return routes.save_connection(event)
    if method == "GET" and path == "/api/admin/connections":
        return routes.list_connections(event)
    discover_resource_match = re.fullmatch(
        r"/api/admin/connections/([a-z0-9_-]+)/discover/([a-z0-9_-]+)", path)
    if method == "GET" and discover_resource_match:
        return routes.discover_connection(discover_resource_match.group(1), event,
                                          resource=discover_resource_match.group(2))
    discover_match = re.fullmatch(r"/api/admin/connections/([a-z0-9_-]+)/discover", path)
    if method == "GET" and discover_match:
        return routes.discover_connection(discover_match.group(1), event)
    if method == "POST" and path == "/api/admin/discover":
        return routes.discover_samples(event, operator_subject)
    test_connection_match = re.fullmatch(r"/api/admin/connections/([a-z0-9_-]+)/test", path)
    if method == "POST" and test_connection_match:
        return routes.test_connection(test_connection_match.group(1), event, operator_subject)
    return None


def _route_grants_tokens(event, method, path, operator_payload, operator_subject):
    """Grants and API tokens."""
    if method == "GET" and path == "/api/admin/grants":
        return routes.list_grants(event)
    if method == "PUT" and path == "/api/admin/grants":
        return routes.save_grant(event, operator_subject)
    if method == "DELETE" and path == "/api/admin/grants":
        return routes.delete_grant(event, operator_subject)
    if method == "GET" and path == "/api/admin/tokens":
        return routes.list_api_tokens(event)
    if method == "PUT" and path == "/api/admin/tokens":
        return routes.create_api_token(event, operator_subject)
    if method == "DELETE" and path == "/api/admin/tokens":
        return routes.delete_api_token(event, operator_subject)
    return None


def _route_ops_email(event, method, path, operator_payload, operator_subject):
    """The email inventory, the sender allowlist, host tasks, workers."""
    if method == "GET" and path == "/api/admin/email-triggers":
        return routes.list_email_triggers(event)
    if method == "GET" and path == "/api/admin/email-from":
        return routes.email_from_list(event)
    if method == "POST" and path == "/api/admin/email-from":
        return routes.email_from_add(event, operator_subject)
    if method == "DELETE" and path == "/api/admin/email-from":
        return routes.email_from_remove(event, operator_subject)
    if method == "GET" and path == "/api/admin/agent-tasks":
        return routes.agent_tasks_list(event)
    if method == "GET" and path == "/api/admin/workers":
        return routes.workers_list(event)
    return None


def _route_storage(event, method, path, operator_payload, operator_subject):
    """Per-workflow key/value storage."""
    storage_match = re.fullmatch(r"/api/admin/storage/([^/]+)", path)
    if method == "GET" and storage_match:
        return routes.storage_read(event, unquote(storage_match.group(1)),
                                   visible=_read_scope(operator_payload))
    if method == "POST" and storage_match:
        return routes.storage_write(event, unquote(storage_match.group(1)),
                                    visible=_read_scope(operator_payload),
                                    operator=operator_subject)
    if method == "DELETE" and storage_match:
        return routes.storage_delete(event, unquote(storage_match.group(1)),
                                     visible=_read_scope(operator_payload),
                                     operator=operator_subject)
    return None


def _route_triggers(event, method, path, operator_payload, operator_subject):
    """Hook, schedule, and poll trigger management."""
    if method == "GET" and path == "/api/admin/hook-triggers":
        return routes.list_hook_triggers(event)
    if method == "PUT" and path == "/api/admin/hook-triggers":
        return routes.save_hook_trigger(event, operator_subject)
    if method == "GET" and path == "/api/admin/hook-triggers/deliveries":
        return routes.list_hook_deliveries(event, visible=_read_scope(operator_payload))
    delivery_match = re.fullmatch(r"/api/admin/hook-triggers/deliveries/([^/]+)", path)
    if method == "GET" and delivery_match:
        return routes.get_hook_delivery(unquote(delivery_match.group(1)),
                                        visible=_read_scope(operator_payload))
    if method == "POST" and path == "/api/admin/hook-triggers/test":
        return routes.test_hook_trigger(event, operator_subject)
    if method == "DELETE" and path == "/api/admin/hook-triggers":
        return routes.delete_hook_trigger(event, operator_subject)
    if method == "GET" and path == "/api/admin/schedule-triggers":
        return routes.list_schedule_triggers(event)
    if method == "PUT" and path == "/api/admin/schedule-triggers":
        return routes.save_schedule_trigger(event, operator_subject)
    if method == "DELETE" and path == "/api/admin/schedule-triggers":
        return routes.delete_schedule_trigger(event, operator_subject)
    if method == "GET" and path == "/api/admin/schedule-triggers/upcoming":
        return routes.upcoming_schedule_triggers(event)
    schedule_action = re.fullmatch(
        r"/api/admin/schedule-triggers/([a-z0-9-]+)/(run|pause|resume)", path)
    if method == "POST" and schedule_action:
        name, action = schedule_action.groups()
        if action == "run":
            return routes.run_schedule_trigger(name, operator_subject)
        return routes.set_schedule_trigger_enabled(name, action == "resume", operator_subject)
    if method == "GET" and path == "/api/admin/poll-triggers":
        return routes.list_poll_triggers(event)
    if method == "PUT" and path == "/api/admin/poll-triggers":
        return routes.save_poll_trigger(event, operator_subject)
    if method == "DELETE" and path == "/api/admin/poll-triggers":
        return routes.delete_poll_trigger(event, operator_subject)
    return None


def _route_connection_tokens(event, method, path, operator_payload, operator_subject):
    """The connection OAuth start plus the token revoke/delete/issue
ops that sit at the chain's tail."""
    match = re.fullmatch(r"/api/admin/oauth/([a-z0-9_-]+)/start", path)
    if method == "GET" and match:
        return oauth_start(event, match.group(1))
    revoke_match = re.fullmatch(r"/api/admin/connections/([a-z0-9_-]+)/tokens", path)
    if method == "DELETE" and revoke_match:
        return routes.revoke_connection_tokens(revoke_match.group(1), operator_subject)
    connection_match = re.fullmatch(r"/api/admin/connections/([a-z0-9_-]+)", path)
    if method == "DELETE" and connection_match:
        return routes.delete_connection(event, connection_match.group(1), operator_subject)
    token_issue_match = re.fullmatch(r"/api/admin/connections/([a-z0-9_-]+)/token", path)
    if method == "POST" and token_issue_match:
        return routes.issue_connection_token(unquote(token_issue_match.group(1)), operator_subject)
    return None


def route(event, method, path):
    """The one dispatch behind every ``/api/admin/*`` and ``/auth/*`` path."""
    response, operator_payload, operator_subject = _gate(event, method, path)
    if response is not None:
        return response
    # The operator gate passed, so a crash in the domain routers can carry
    # its traceback in the body — the evidence the Lambda log holds,
    # reachable from the console without log access. Pre-gate paths (login,
    # the OAuth callback) keep the bare failure.
    try:
        return _route_domains(event, method, path, operator_payload, operator_subject)
    except Exception as exc:
        import traceback
        return http._operator_error_500(event, exc, traceback_text=traceback.format_exc())


def _route_domains(event, method, path, operator_payload, operator_subject):
    for router in (_route_overview_runs, _route_usage_errors, _route_inbox,
                   _route_connections, _route_grants_tokens, _route_ops_email,
                   _route_storage, _route_triggers, _route_connection_tokens):
        response = router(event, method, path, operator_payload, operator_subject)
        if response is not None:
            return response
    # Late import: dispatch_designer resolves its helpers through this
    # module, so a module-level import here would be circular.
    from . import dispatch_designer

    response = dispatch_designer._route_designer_all(
        event, method, path, operator_payload, operator_subject)
    if response is not None:
        return response
    return http._json_response(404, {"error": "Not found"})
