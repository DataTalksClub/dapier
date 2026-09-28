"""Console admin API: the /api/admin/* and /auth/* dispatcher."""
import re
from urllib.parse import unquote

from ... import http
from ...auth import authz, roles, session, visibility
from ...connections import oauth_flow
from .. import agent, overview
from . import login, routes, users_routes  # noqa: F401 (used via module refs)


def _read_scope(payload):
    """G17 visibility for the owned-workflow surfaces, built from the same
    effective-role verdict the gate above just applied: operators (and
    admins) see everything and may write anything, everyone else is
    owner-scoped to their subject. Phase 2 filters the reads (designer
    list, overview, runs, inbox, usage) with it; Phase 3 gates the
    workflow WRITE routes with it (the handlers call ensure_can_write).
    Computed only on the routes that scope, not on every request."""
    return visibility.for_session(payload)


def route(event, method, path):
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
            "role": roles.effective_role(payload),
        })
    if not session._csrf_ok(event, method):
        return http._json_response(403, {"error": "Cross-site request rejected"})
    # Roles v1: the least privilege band this route accepts (viewer reads,
    # editor workflow-editing, operator everything else, admin user
    # management). With no stored role assignments this answers "admin" for
    # every route — the historical gate, unchanged.
    operator_payload, operator_error = session.require_role(
        event, roles.minimum_for_route(method, path))
    if operator_error:
        return operator_error
    operator_subject = session.subject_fallback(operator_payload)
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
    if method == "GET" and path == "/api/admin/usage":
        return routes.usage(event, visible=_read_scope(operator_payload))
    if method == "GET" and path == "/api/admin/quota":
        return routes.quota_get(event)
    if method == "PUT" and path == "/api/admin/quota":
        return routes.quota_save(event, operator_subject)
    if method == "GET" and path == "/api/admin/errors/summary":
        return routes.errors_summary(event)
    if method == "POST" and path == "/api/admin/errors/digest":
        return routes.send_error_digest(event, operator_subject)
    run_replay_match = re.fullmatch(r"/api/admin/runs/([^/]+)/replay", path)
    if method == "POST" and run_replay_match:
        return routes.replay_run(unquote(run_replay_match.group(1)),
                                 operator_subject, event)
    run_cancel_match = re.fullmatch(r"/api/admin/runs/([^/]+)/cancel", path)
    if method == "POST" and run_cancel_match:
        return routes.cancel_run(unquote(run_cancel_match.group(1)), operator_subject)
    run_match = re.fullmatch(r"/api/admin/runs/([^/]+)", path)
    if method == "GET" and run_match:
        return routes.get_run(unquote(run_match.group(1)))
    if method == "GET" and path == "/api/admin/triggers/inbox":
        return routes.list_inbox(event, visible=_read_scope(operator_payload))
    if method == "GET" and path == "/api/admin/triggers/sample":
        return routes.trigger_sample(event, operator_subject)
    inbox_replay_match = re.fullmatch(r"/api/admin/triggers/inbox/([^/]+)/replay", path)
    if method == "POST" and inbox_replay_match:
        return routes.replay_inbox_event(unquote(inbox_replay_match.group(1)), operator_subject)
    inbox_match = re.fullmatch(r"/api/admin/triggers/inbox/([^/]+)", path)
    if method == "GET" and inbox_match:
        return routes.get_inbox_event(unquote(inbox_match.group(1)))
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
    if method == "POST" and path == "/api/admin/connections/import":
        return routes.import_connection(event, operator_subject)
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
    if method == "GET" and path == "/api/admin/grants":
        return routes.list_grants(event)
    if method == "PUT" and path == "/api/admin/grants":
        return routes.save_grant(event, operator_subject)
    if method == "DELETE" and path == "/api/admin/grants":
        return routes.delete_grant(event, operator_subject)
    if method == "GET" and path == "/api/admin/users":
        return users_routes.list_users(event)
    if method == "POST" and path == "/api/admin/users":
        return users_routes.set_user_role(event, operator_subject)
    user_match = re.fullmatch(r"/api/admin/users/([^/]+)", path)
    if method == "DELETE" and user_match:
        return users_routes.remove_user(event, unquote(user_match.group(1)),
                                        operator_subject)
    if method == "GET" and path == "/api/admin/tokens":
        return routes.list_api_tokens(event)
    if method == "PUT" and path == "/api/admin/tokens":
        return routes.create_api_token(event, operator_subject)
    if method == "DELETE" and path == "/api/admin/tokens":
        return routes.revoke_api_token(event, operator_subject)
    if method == "GET" and path == "/api/admin/email-triggers":
        return routes.list_email_triggers(event)
    if method == "PUT" and path == "/api/admin/email-triggers":
        return routes.save_email_trigger(event, operator_subject)
    if method == "DELETE" and path == "/api/admin/email-triggers":
        return routes.delete_email_trigger(event, operator_subject)
    if method == "GET" and path == "/api/admin/email-from":
        return routes.email_from_list(event)
    if method == "POST" and path == "/api/admin/email-from":
        return routes.email_from_add(event, operator_subject)
    if method == "DELETE" and path == "/api/admin/email-from":
        return routes.email_from_remove(event, operator_subject)
    if method == "GET" and path == "/api/admin/agent-tasks":
        return routes.agent_tasks_list(event)
    if method == "GET" and path == "/api/admin/designer/workflows":
        return routes.designer_list(event, visible=_read_scope(operator_payload))
    if method == "GET" and path == "/api/admin/designer/catalog":
        from ...connectors import registry

        return http._json_response(200, registry.catalog())
    if method == "PUT" and path == "/api/admin/designer/workflows":
        return routes.save_designer_workflow(event, operator_subject,
                                             visible=_read_scope(operator_payload))
    if method == "POST" and path == "/api/admin/designer/workflows/test":
        return routes.test_designer_workflow(event, operator_subject)
    if method == "POST" and path == "/api/admin/designer/workflows/test-step":
        return routes.test_designer_step(event, operator_subject)
    if method == "POST" and path == "/api/admin/designer/workflows/bulk":
        return routes.bulk_designer_workflow(event, operator_subject,
                                             visible=_read_scope(operator_payload))
    if method == "GET" and path == "/api/admin/designer/workflows/export-all":
        return routes.export_all_designer_workflows(event, operator_subject)
    if method == "GET" and path == "/api/admin/designer/export":
        return routes.export_designer_workflows(event, operator_subject)
    if method == "POST" and path == "/api/admin/copilot/draft":
        return routes.copilot_draft(event, operator_subject)
    designer_match = re.fullmatch(r"/api/admin/designer/workflows/([a-z0-9][a-z0-9._-]*\.yaml)", path)
    if method == "GET" and designer_match:
        return routes.designer_get(designer_match.group(1))
    if method == "PUT" and designer_match:
        return routes.toggle_designer_workflow(event, operator_subject, designer_match.group(1),
                                               visible=_read_scope(operator_payload))
    if method == "DELETE" and designer_match:
        return routes.delete_designer_workflow(event, operator_subject, designer_match.group(1),
                                               visible=_read_scope(operator_payload))
    designer_tags_match = re.fullmatch(
        r"/api/admin/designer/workflows/([a-z0-9][a-z0-9._-]*\.yaml)/tags", path)
    if method == "PUT" and designer_tags_match:
        return routes.tags_designer_workflow(event, operator_subject, designer_tags_match.group(1),
                                             visible=_read_scope(operator_payload))
    designer_folder_match = re.fullmatch(
        r"/api/admin/designer/workflows/([a-z0-9][a-z0-9._-]*\.yaml)/folder", path)
    if method == "PUT" and designer_folder_match:
        return routes.folder_designer_workflow(event, operator_subject, designer_folder_match.group(1),
                                               visible=_read_scope(operator_payload))
    designer_test_match = re.fullmatch(
        r"/api/admin/designer/workflows/([a-z0-9][a-z0-9._-]*\.yaml)/test", path)
    if method == "POST" and designer_test_match:
        return routes.test_designer_workflow(event, operator_subject, designer_test_match.group(1),
                                             visible=_read_scope(operator_payload))
    designer_test_step_match = re.fullmatch(
        r"/api/admin/designer/workflows/([a-z0-9][a-z0-9._-]*\.yaml)/test-step", path)
    if method == "POST" and designer_test_step_match:
        return routes.test_designer_step(event, operator_subject, designer_test_step_match.group(1),
                                         visible=_read_scope(operator_payload))
    designer_duplicate_match = re.fullmatch(
        r"/api/admin/designer/workflows/([a-z0-9][a-z0-9._-]*\.yaml)/duplicate", path)
    if method == "POST" and designer_duplicate_match:
        return routes.duplicate_designer_workflow(event, operator_subject, designer_duplicate_match.group(1))
    if method == "GET" and path == "/api/admin/designer/templates":
        return routes.templates_list(event)
    designer_template_apply_match = re.fullmatch(
        r"/api/admin/designer/templates/([a-z0-9][a-z0-9._-]*\.yaml)/apply", path)
    if method == "POST" and designer_template_apply_match:
        return routes.apply_designer_template(event, operator_subject, designer_template_apply_match.group(1))
    designer_template_match = re.fullmatch(
        r"/api/admin/designer/workflows/([a-z0-9][a-z0-9._-]*\.yaml)/template", path)
    if method == "PUT" and designer_template_match:
        return routes.template_flag_designer_workflow(event, operator_subject, designer_template_match.group(1),
                                                      visible=_read_scope(operator_payload))
    designer_versions_match = re.fullmatch(
        r"/api/admin/designer/workflows/([a-z0-9][a-z0-9._-]*\.yaml)/versions", path)
    if method == "GET" and designer_versions_match:
        return routes.versions_designer_workflow(designer_versions_match.group(1))
    designer_versions_diff_match = re.fullmatch(
        r"/api/admin/designer/workflows/([a-z0-9][a-z0-9._-]*\.yaml)/versions/diff", path)
    if method == "GET" and designer_versions_diff_match:
        return routes.diff_designer_workflow(event, designer_versions_diff_match.group(1))
    designer_rollback_match = re.fullmatch(
        r"/api/admin/designer/workflows/([a-z0-9][a-z0-9._-]*\.yaml)/rollback", path)
    if method == "POST" and designer_rollback_match:
        return routes.rollback_designer_workflow(event, operator_subject, designer_rollback_match.group(1),
                                                 visible=_read_scope(operator_payload))
    # Draft vs live (G15): a save drafts; these promote or throw the draft.
    designer_publish_match = re.fullmatch(
        r"/api/admin/designer/workflows/([a-z0-9][a-z0-9._-]*\.yaml)/publish", path)
    if method == "POST" and designer_publish_match:
        return routes.publish_designer_workflow(event, operator_subject, designer_publish_match.group(1),
                                                visible=_read_scope(operator_payload))
    designer_draft_match = re.fullmatch(
        r"/api/admin/designer/workflows/([a-z0-9][a-z0-9._-]*\.yaml)/draft", path)
    if method == "GET" and designer_draft_match:
        return routes.draft_designer_workflow(designer_draft_match.group(1))
    if method == "DELETE" and designer_draft_match:
        return routes.discard_designer_draft(event, operator_subject, designer_draft_match.group(1),
                                             visible=_read_scope(operator_payload))
    designer_draft_diff_match = re.fullmatch(
        r"/api/admin/designer/workflows/([a-z0-9][a-z0-9._-]*\.yaml)/draft/diff", path)
    if method == "GET" and designer_draft_diff_match:
        return routes.draft_diff_designer_workflow(designer_draft_diff_match.group(1))
    storage_match = re.fullmatch(r"/api/admin/storage/([^/]+)", path)
    if method == "GET" and storage_match:
        return routes.storage_read(event, unquote(storage_match.group(1)))
    if method == "POST" and storage_match:
        return routes.storage_write(event, unquote(storage_match.group(1)),
                                    visible=_read_scope(operator_payload),
                                    operator=operator_subject)
    if method == "DELETE" and storage_match:
        return routes.storage_delete(event, unquote(storage_match.group(1)),
                                     visible=_read_scope(operator_payload),
                                     operator=operator_subject)
    if method == "GET" and path == "/api/admin/hook-triggers":
        return routes.list_hook_triggers(event)
    if method == "PUT" and path == "/api/admin/hook-triggers":
        return routes.save_hook_trigger(event, operator_subject)
    if method == "DELETE" and path == "/api/admin/hook-triggers":
        return routes.delete_hook_trigger(event, operator_subject)
    if method == "GET" and path == "/api/admin/schedule-triggers":
        return routes.list_schedule_triggers(event)
    if method == "PUT" and path == "/api/admin/schedule-triggers":
        return routes.save_schedule_trigger(event, operator_subject)
    if method == "DELETE" and path == "/api/admin/schedule-triggers":
        return routes.delete_schedule_trigger(event, operator_subject)
    if method == "GET" and path == "/api/admin/poll-triggers":
        return routes.list_poll_triggers(event)
    if method == "PUT" and path == "/api/admin/poll-triggers":
        return routes.save_poll_trigger(event, operator_subject)
    if method == "DELETE" and path == "/api/admin/poll-triggers":
        return routes.delete_poll_trigger(event, operator_subject)
    match = re.fullmatch(r"/api/admin/oauth/([a-z0-9_-]+)/start", path)
    if method == "GET" and match:
        return oauth_start(event, match.group(1))
    revoke_match = re.fullmatch(r"/api/admin/connections/([a-z0-9_-]+)/tokens", path)
    if method == "DELETE" and revoke_match:
        return routes.revoke_connection_tokens(revoke_match.group(1), operator_subject)
    token_issue_match = re.fullmatch(r"/api/admin/connections/([a-z0-9_-]+)/token", path)
    if method == "POST" and token_issue_match:
        return routes.issue_connection_token(unquote(token_issue_match.group(1)), operator_subject)
    return http._json_response(404, {"error": "Not found"})

# Facade re-exports: the dispatcher plus every endpoint, so callers can use
# admin.save_credential / admin.list_grants directly.
from .login import auth_callback, auth_error, auth_login, auth_logout  # noqa: F401
from .routes import (  # noqa: F401
    agent_tasks_list,
    copilot_draft,
    create_api_token,
    delete_email_trigger,
    delete_grant,
    delete_hook_trigger,
    delete_poll_trigger,
    delete_schedule_trigger,
    designer_get,
    designer_list,
    delete_designer_workflow,
    bulk_designer_workflow,
    tags_designer_workflow,
    folder_designer_workflow,
    discover_connection,
    duplicate_designer_workflow,
    apply_designer_template,
    template_flag_designer_workflow,
    templates_list,
    errors_summary,
    send_error_digest,
    get_inbox_event,
    get_run,
    import_connection,
    issue_connection_token,
    list_api_tokens,
    list_audit,
    list_connections,
    list_email_triggers,
    export_audit,
    export_all_designer_workflows,
    export_designer_workflows,
    export_runs,
    list_grants,
    list_hook_triggers,
    list_inbox,
    list_poll_triggers,
    list_runs,
    list_schedule_triggers,
    oauth_clients_view,
    cancel_run,
    replay_failed_runs,
    replay_inbox_event,
    revoke_api_token,
    replay_run,
    revoke_connection_tokens,
    save_connection,
    save_credential,
    save_designer_workflow,
    publish_designer_workflow,
    discard_designer_draft,
    draft_designer_workflow,
    draft_diff_designer_workflow,
    storage_delete,
    storage_read,
    storage_write,
    test_designer_step,
    test_designer_workflow,
    toggle_designer_workflow,
    save_email_trigger,
    save_grant,
    save_hook_trigger,
    save_oauth_client,
    save_poll_trigger,
    save_schedule_trigger,
    test_connection,
    trigger_sample,
    rollback_designer_workflow,
    diff_designer_workflow,
    versions_designer_workflow,
)
from ...connections.oauth_flow import (  # noqa: F401
    oauth_callback,
    oauth_callback_url,
    oauth_start,
)
from .users_routes import list_users, remove_user, set_user_role  # noqa: F401
from ...auth.session import (  # noqa: F401
    SESSION_COOKIE,
    SESSION_TTL_SECONDS,
    OAUTH_COOKIE,
    AUTH_STATE_COOKIE,
    authenticated,
    require_operator,
    subject_fallback,
)
