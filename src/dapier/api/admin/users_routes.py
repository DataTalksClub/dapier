"""Console user-management endpoints (admin-only): thin JSON wrappers over
the shared role store in auth.roles.

The CLI drives the same domain functions through /api/agent/users; the audit
rows (``users.set-role`` / ``users.remove``) are written by the domain layer,
so both surfaces record every mutation. Registration lives in the admin
dispatcher's route() — the routes themselves answer only after
``session.require_role(event, roles.minimum_for_route(method, path))``
cleared them, which for these paths means the ``admin`` band.
"""

from ... import http
from ...auth import roles


def list_users(event):
    """Every stored user: subject, role, display name, disabled flag."""
    status, payload = roles.api_list_users()
    return http._json_response(status, payload)


def set_user_role(event, operator):
    """Assign one user's role: body ``{subject, role, display_name?, disabled?}``.

    Refuses (409) to demote, disable, or remove the last admin.
    """
    try:
        body = http._request_json(event)
    except (ValueError, AttributeError):
        return http._json_response(400, {"error": "Invalid request"})
    status, payload = roles.api_set_role(body, operator=operator)
    return http._json_response(status, payload)


def remove_user(event, subject, operator):
    """Remove one user's stored role; the allowlist fallback applies again."""
    status, payload = roles.api_remove_role(subject, operator=operator)
    return http._json_response(status, payload)
