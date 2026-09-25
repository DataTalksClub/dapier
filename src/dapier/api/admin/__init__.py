"""Console admin API: the /api/admin/* and /auth/* dispatcher."""
import re
from urllib.parse import unquote

from ... import http
from ...auth import authz, session
from ...connections import oauth_flow
from .. import agent, overview
from . import login, routes  # noqa: F401 (used via module refs)


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
        })
    if not session._csrf_ok(event, method):
        return http._json_response(403, {"error": "Cross-site request rejected"})
    operator_payload, operator_error = session.require_operator(event)
    if operator_error:
        return operator_error
    operator_subject = session.subject_fallback(operator_payload)
    if method == "GET" and path == "/api/admin/overview":
        return overview.overview()
    if method == "GET" and path == "/api/admin/runs":
        return routes.list_runs(event)
    run_match = re.fullmatch(r"/api/admin/runs/([^/]+)", path)
    if method == "GET" and run_match:
        return routes.get_run(unquote(run_match.group(1)))
    if method == "PUT" and path.startswith("/api/admin/credentials/"):
        return routes.save_credential(path.rsplit("/", 1)[1], event)
    if method == "GET" and path == "/api/admin/oauth-clients":
        return routes.oauth_clients_view()
    if method == "PUT" and path.startswith("/api/admin/oauth-clients/"):
        return routes.save_oauth_client(path.rsplit("/", 1)[1], event)
    if method == "PUT" and path == "/api/admin/connections":
        return routes.save_connection(event)
    if method == "POST" and path == "/api/admin/connections/import":
        return routes.import_connection(event, operator_subject)
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
        return routes.revoke_api_token(event, operator_subject)
    if method == "GET" and path == "/api/admin/email-triggers":
        return routes.list_email_triggers(event)
    if method == "PUT" and path == "/api/admin/email-triggers":
        return routes.save_email_trigger(event, operator_subject)
    if method == "DELETE" and path == "/api/admin/email-triggers":
        return routes.delete_email_trigger(event, operator_subject)
    if method == "GET" and path == "/api/admin/designer/workflows":
        return routes.designer_list(event)
    if method == "PUT" and path == "/api/admin/designer/workflows":
        return routes.save_designer_workflow(event, operator_subject)
    designer_match = re.fullmatch(r"/api/admin/designer/workflows/([a-z0-9][a-z0-9._-]*\.yaml)", path)
    if method == "GET" and designer_match:
        return routes.designer_get(designer_match.group(1))
    if method == "PUT" and designer_match:
        return routes.toggle_designer_workflow(event, operator_subject, designer_match.group(1))
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
    match = re.fullmatch(r"/api/admin/oauth/([a-z0-9_-]+)/start", path)
    if method == "GET" and match:
        return oauth_start(event, match.group(1))
    revoke_match = re.fullmatch(r"/api/admin/connections/([a-z0-9_-]+)/tokens", path)
    if method == "DELETE" and revoke_match:
        return routes.revoke_connection_tokens(revoke_match.group(1), operator_subject)
    return http._json_response(404, {"error": "Not found"})

# Facade re-exports: the dispatcher plus every endpoint, so callers can use
# admin.save_credential / admin.list_grants directly.
from .login import auth_callback, auth_error, auth_login, auth_logout  # noqa: F401
from .routes import (  # noqa: F401
    create_api_token,
    delete_email_trigger,
    delete_grant,
    delete_hook_trigger,
    delete_schedule_trigger,
    designer_get,
    designer_list,
    get_run,
    import_connection,
    list_api_tokens,
    list_email_triggers,
    list_grants,
    list_hook_triggers,
    list_runs,
    list_schedule_triggers,
    oauth_clients_view,
    revoke_api_token,
    revoke_connection_tokens,
    save_connection,
    save_credential,
    save_designer_workflow,
    toggle_designer_workflow,
    save_email_trigger,
    save_grant,
    save_hook_trigger,
    save_oauth_client,
    save_schedule_trigger,
)
from ...connections.oauth_flow import (  # noqa: F401
    oauth_callback,
    oauth_callback_url,
    oauth_start,
)
from ...auth.session import (  # noqa: F401
    SESSION_COOKIE,
    SESSION_TTL_SECONDS,
    OAUTH_COOKIE,
    AUTH_STATE_COOKIE,
    authenticated,
    require_operator,
    subject_fallback,
)
