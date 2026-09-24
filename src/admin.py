"""Back-compat shim; canonical home: src/dapier/api/admin/."""
from .dapier.api.admin import *
from .dapier.api.admin import login, routes
from .dapier.api.overview import overview
from .dapier.auth.session import (
    SESSION_COOKIE, OAUTH_COOKIE, AUTH_STATE_COOKIE, SESSION_TTL_SECONDS,
    _audit_event, _b64decode, _b64encode, _cookie, _csrf_ok, _credentials,
    _header, _session_payload, _session_subject, _sign, _verify,
    authenticated, require_operator, subject_fallback,
)
from .dapier.api.admin.routes import _connection
from .dapier.connections.oauth_flow import (
    oauth_callback, oauth_callback_url, oauth_start,
)
from .dapier.api.admin.login import (
    auth_callback, auth_error, auth_login, auth_logout,
)
