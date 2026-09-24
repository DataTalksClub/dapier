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


def status(provider_name):
    """Presence and source of a shared OAuth client — never its secret."""
    provider = canonical_provider(str(provider_name or "").strip().lower())
    stored = stored_client(provider) or {}
    if stored.get("client_id") and stored.get("client_secret"):
        return {"provider": provider, "client_id": stored["client_id"], "source": "config", "configured": True}
    env_id, env_secret = env_vars(provider)
    if os.environ.get(env_id, "").strip() and os.environ.get(env_secret, "").strip():
        return {"provider": provider, "client_id": os.environ[env_id].strip(), "source": "deploy", "configured": True}
    return {"provider": provider, "client_id": "", "source": "none", "configured": False}


def api_save_client(provider_name, client_id, client_secret):
    """Validate and store the shared OAuth client for a provider.

    Shared by the console (cookie) and CLI (bearer) API layers. Returns
    ``(status, payload)``; the secret is write-only — the payload reports
    presence, not the value.
    """
    provider = canonical_provider(str(provider_name or "").strip().lower())
    if provider not in CANONICAL_PROVIDERS:
        return 404, {"error": "Unknown OAuth client provider"}
    client_id = str(client_id or "").strip()
    client_secret = str(client_secret or "").strip()
    if not client_id or not client_secret:
        return 400, {"error": "Both the client ID and the client secret are required"}
    from ..credentials import put_credential  # kept local: this module stays import-light

    put_credential(
        f"oauth-client#{provider}",
        {"client_id": client_id, "client_secret": client_secret},
        provider=provider,
    )
    invalidate_cache()
    return 200, {"provider": provider, "client_id": client_id, "source": "config", "configured": True}


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
            f"No OAuth client is configured for {provider_name}; save it with "
            f"`dapier oauth-clients set` or the console's Credentials → OAuth "
            f"clients (or set {env_id} and {env_secret} at deploy time)"
        )
    return resolved_id, resolved_secret
