"""Shared per-provider OAuth client configuration.

Client IDs and secrets are created once in each provider console and stored
at runtime in the credentials table under ``oauth-client#<provider>`` —
operators set or rotate them from the console without a redeploy. The
deploy-time environment (CloudFormation parameters rendered into function
env vars) remains a fallback so a fresh stack works before anyone configures
anything. Resolution order: explicit values (the import path for refresh
tokens issued by another client), then the stored config record, then the
environment.

A connection can still carry its own ``client_id``/``client_secret`` in the
credential record — legacy items and CLI imports keep working, because an
explicit stored value on the connection wins over the shared configuration.
"""

import os
import time

from . import oauth_providers

# Canonical configuration providers: youtube shares the Google client.
CANONICAL_PROVIDERS = ("dropbox", "google")

_ENV_VARS = {
    "google": ("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET"),
    "youtube": ("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET"),
    "dropbox": ("DROPBOX_OAUTH_CLIENT_ID", "DROPBOX_OAUTH_CLIENT_SECRET"),
}

_RECORD_PREFIX = "oauth-client#"
_CACHE_TTL_SECONDS = 60
# provider -> (monotonic timestamp, stored value dict or None)
_cache = {}


class ClientConfigError(RuntimeError):
    """The provider's OAuth client is not configured anywhere we look."""


def canonical_provider(provider_name):
    """Fold provider aliases onto the config record they share."""
    return "google" if provider_name == "youtube" else provider_name


def env_vars(provider_name):
    """The deploy-time fallback environment variable names."""
    return _ENV_VARS[provider_name]


def stored_client(provider_name):
    """The credentials-table config record for the shared client, or None.

    Reads are cached for a minute so consent/refresh hot paths do not pay a
    GetItem each time, and any storage problem counts as "not configured" —
    the environment fallback still applies. Set DAPIER_SKIP_CONFIG_DB=1 to
    keep hermetic unit tests off the network entirely.
    """
    now = time.monotonic()
    cached = _cache.get(provider_name)
    if cached and now - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1]
    value = None
    if os.environ.get("DAPIER_SKIP_CONFIG_DB") != "1":
        try:
            import boto3

            record = (
                boto3.resource("dynamodb")
                .Table(os.environ["CREDENTIALS_TABLE"])
                .get_item(Key={"credential_id": f"{_RECORD_PREFIX}{provider_name}"})
                .get("Item")
            )
            if isinstance(record, dict) and isinstance(record.get("value"), dict):
                value = record["value"]
        except Exception:  # noqa: BLE001 — any storage problem falls back to env
            value = None
    _cache[provider_name] = (now, value)
    return value


def invalidate_cache():
    """Drop cached lookups so a runtime reconfiguration is visible at once."""
    _cache.clear()


def get(provider_name, *, client_id=None, client_secret=None):
    """Return ``(client_id, client_secret)`` for the provider.

    Explicit values win (the import path for refresh tokens issued by
    another client); then the runtime config record; then the deploy-time
    environment. Raises ``ClientConfigError`` when nothing is configured.
    """
    oauth_providers.get(provider_name)
    env_id, env_secret = _ENV_VARS[provider_name]
    resolved_id = str(client_id or "").strip()
    resolved_secret = str(client_secret or "").strip()
    if not resolved_id or not resolved_secret:
        stored = stored_client(canonical_provider(provider_name)) or {}
        resolved_id = resolved_id or str(stored.get("client_id", "")).strip()
        resolved_secret = resolved_secret or str(stored.get("client_secret", "")).strip()
    if not resolved_id or not resolved_secret:
        resolved_id = resolved_id or os.environ.get(env_id, "").strip()
        resolved_secret = resolved_secret or os.environ.get(env_secret, "").strip()
    if not resolved_id or not resolved_secret:
        raise ClientConfigError(
            f"No OAuth client is configured for {provider_name}; save it in the "
            f"console under Credentials → OAuth clients (or set {env_id} and "
            f"{env_secret} at deploy time)"
        )
    return resolved_id, resolved_secret
