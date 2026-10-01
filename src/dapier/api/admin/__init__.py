"""Console admin API: the /api/admin/* and /auth/* dispatcher.

The dispatch itself lives in :mod:`.dispatch` (with the designer routers
in :mod:`.dispatch_designer`); this package is the facade — ``route`` for
the API router, plus every endpoint re-exported so callers can use
``admin.save_credential`` / ``admin.list_grants`` directly.
"""
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
from .dispatch import route  # noqa: F401
from .login import auth_callback, auth_error, auth_login, auth_logout  # noqa: F401
from .routes import *  # noqa: F401
