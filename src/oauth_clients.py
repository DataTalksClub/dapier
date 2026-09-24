"""Shared per-provider OAuth client configuration.

Client IDs and secrets are created once in the provider console and deploy
with the stack (CloudFormation parameters rendered into function environment
variables), so adding a connection never involves registering an OAuth
client or pasting its credentials. Provider tokens still live in the
credentials table; only the client credentials come from the environment.

A connection can still carry its own ``client_id``/``client_secret`` in the
credential record — legacy items and CLI imports of refresh tokens issued by
a different client keep working, because an explicit stored value wins over
the shared configuration.
"""

import os

from . import oauth_providers

_ENV_VARS = {
    "google": ("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET"),
    "youtube": ("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET"),
    "dropbox": ("DROPBOX_OAUTH_CLIENT_ID", "DROPBOX_OAUTH_CLIENT_SECRET"),
}


class ClientConfigError(RuntimeError):
    """The provider's OAuth client is not configured in the environment."""


def get(provider_name, *, client_id=None, client_secret=None):
    """Return ``(client_id, client_secret)`` for the provider.

    Explicit values win (the import path for refresh tokens issued by
    another client); otherwise the deploy-time environment provides both.
    Raises ``ClientConfigError`` with the missing variable names.
    """
    oauth_providers.get(provider_name)
    env_id, env_secret = _ENV_VARS[provider_name]
    resolved_id = str(client_id or "").strip() or os.environ.get(env_id, "").strip()
    resolved_secret = str(client_secret or "").strip() or os.environ.get(env_secret, "").strip()
    if not resolved_id or not resolved_secret:
        raise ClientConfigError(
            f"No OAuth client is configured for {provider_name}; "
            f"set {env_id} and {env_secret} at deploy time"
        )
    return resolved_id, resolved_secret
