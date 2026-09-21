"""Provider adapters for Dapier's named OAuth connections.

Each adapter describes one OAuth provider's authorization URL, token URL,
consent parameters, scope rules, account-identity check, and revocation.
Network access goes through an injectable ``transport`` callable so the logic
stays unit-testable without HTTP mocks:

    transport(method, url, *, headers, body) -> (status_code, body_bytes)
"""

import json
import urllib.parse
import urllib.request


class ProviderError(Exception):
    """A provider call failed. The message is already redacted."""


class UnknownProviderError(KeyError):
    """No adapter is registered for the requested provider."""


DROPBOX_AUTHORIZATION_URL = "https://www.dropbox.com/oauth2/authorize"
DROPBOX_TOKEN_URL = "https://api.dropboxapi.com/oauth2/token"
DROPBOX_ACCOUNT_URL = "https://api.dropboxapi.com/2/users/get_current_account"
DROPBOX_REVOKE_URL = "https://api.dropboxapi.com/2/auth/token/revoke"

GOOGLE_AUTHORIZATION_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
YOUTUBE_CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"

# Scopes sufficient for the account-identity check of each provider. A
# connection whose granted scopes fall outside these sets cannot be verified
# and must fail closed (see verify_account).
IDENTITY_SCOPES = {
    "youtube": (
        "https://www.googleapis.com/auth/youtube",
        "https://www.googleapis.com/auth/youtube.readonly",
        "https://www.googleapis.com/auth/youtube.upload",
    ),
}

PROVIDERS = {
    "dropbox": {
        "authorization_url": DROPBOX_AUTHORIZATION_URL,
        "token_url": DROPBOX_TOKEN_URL,
        # Dropbox grants a long-lived refresh token only with offline access.
        "extra": {"token_access_type": "offline"},
    },
    "youtube": {
        "authorization_url": GOOGLE_AUTHORIZATION_URL,
        "token_url": GOOGLE_TOKEN_URL,
        # Google re-issues a refresh token only with offline access plus an
        # explicit consent prompt.
        "extra": {"access_type": "offline", "prompt": "consent"},
    },
}

# Fields that must never appear in logs, audit records, or error responses.
_SECRET_FIELDS = (
    "client_secret",
    "refresh_token",
    "access_token",
    "code",
    "code_verifier",
)


def get(provider_name):
    """Return the adapter spec for ``provider_name`` or raise."""
    try:
        return PROVIDERS[provider_name]
    except KeyError:
        raise UnknownProviderError(provider_name)


def normalize_scopes(provider_name, scopes):
    """Return scopes as a sorted, deduplicated list of non-empty strings."""
    get(provider_name)
    cleaned = sorted({str(scope).strip() for scope in scopes or [] if str(scope).strip()})
    if provider_name == "youtube" and not cleaned:
        raise ProviderError("YouTube connections require at least one scope")
    return cleaned


def authorization_url(provider_name, *, client_id, redirect_uri, scopes, state, code_challenge=None):
    """Build the provider consent URL for an authorization-code + PKCE flow."""
    spec = get(provider_name)
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "state": state,
        **spec["extra"],
    }
    if scopes:
        params["scope"] = " ".join(scopes)
    if code_challenge is not None:
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"
    return f"{spec['authorization_url']}?{urllib.parse.urlencode(params)}"


def _default_transport(method, url, *, headers=None, body=None, timeout=15):
    request = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, response.read()


def _form_post(url, fields, *, transport, timeout=15):
    body = urllib.parse.urlencode(fields).encode()
    transport = transport or _default_transport
    try:
        status, raw = transport(
            "POST",
            url,
            headers={"content-type": "application/x-www-form-urlencoded"},
            body=body,
            timeout=timeout,
        )
    except Exception as exc:
        raise ProviderError(f"token endpoint unreachable: {type(exc).__name__}")
    try:
        data = json.loads(raw.decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        raise ProviderError(f"token endpoint returned HTTP {status} with an unreadable body")
    if status >= 300 or not isinstance(data, dict):
        raise ProviderError(f"token endpoint returned HTTP {status}: {redact(data)}")
    if data.get("error"):
        raise ProviderError(f"token endpoint error: {redact(data)}")
    return data


def exchange_code(provider_name, *, code, client_id, client_secret, redirect_uri,
                  code_verifier=None, transport=None):
    """Exchange an authorization code for tokens (PKCE when ``code_verifier``)."""
    spec = get(provider_name)
    fields = {
        "code": code,
        "grant_type": "authorization_code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
    }
    if client_secret:
        fields["client_secret"] = client_secret
    if code_verifier is not None:
        fields["code_verifier"] = code_verifier
    data = _form_post(spec["token_url"], fields, transport=transport)
    if "access_token" not in data:
        raise ProviderError("token endpoint response did not include an access token")
    return data


def refresh_access_token(provider_name, *, refresh_token, client_id, client_secret, transport=None):
    """Refresh an access token. May omit ``refresh_token`` on rotation."""
    spec = get(provider_name)
    fields = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }
    if client_secret:
        fields["client_secret"] = client_secret
    data = _form_post(spec["token_url"], fields, transport=transport)
    if "access_token" not in data:
        raise ProviderError("refresh response did not include an access token")
    return data


def normalize_token_data(data, *, previous_refresh_token=None, now=None):
    """Normalize a token response into Dapier's stored token shape.

    Preserves a valid refresh token when the provider omits ``refresh_token``
    on rotation. ``expires_at`` is an epoch timestamp with a 60s safety skew.
    """
    import time as _time

    now = int(now if now is not None else _time.time())
    refresh_token = data.get("refresh_token") or previous_refresh_token
    if not refresh_token and "refresh_token" in data and previous_refresh_token:
        refresh_token = previous_refresh_token
    try:
        lifetime = int(data.get("expires_in", 3600))
    except (TypeError, ValueError):
        lifetime = 3600
    return {
        "access_token": data["access_token"],
        "refresh_token": refresh_token,
        "expires_at": now + max(lifetime - 60, 0),
        "token_type": data.get("token_type", "Bearer"),
        "scope": data.get("scope", ""),
        "obtained_at": now,
    }


def is_expired(stored, *, now=None, skew_seconds=120):
    """True when the stored token should be refreshed before use."""
    import time as _time

    now = int(now if now is not None else _time.time())
    try:
        return int(stored.get("expires_at", 0)) <= now + skew_seconds
    except (TypeError, ValueError):
        return True


def _get_json(url, access_token, *, transport, timeout=15):
    transport = transport or _default_transport
    try:
        status, raw = transport(
            "GET", url,
            headers={"authorization": f"Bearer {access_token}"},
            body=None, timeout=timeout,
        )
    except Exception as exc:
        raise ProviderError(f"provider identity check unreachable: {type(exc).__name__}")
    try:
        data = json.loads(raw.decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        raise ProviderError(f"provider identity check returned HTTP {status}")
    if status >= 300 or not isinstance(data, dict):
        raise ProviderError(f"provider identity check returned HTTP {status}: {redact(data)}")
    return data


def verify_account(provider_name, access_token, *, transport=None):
    """Return ``(account_id, account_title)`` for the token's provider user.

    Fails closed: any provider error, empty result, or insufficient scope
    raises ProviderError instead of returning an unverified identity.
    """
    get(provider_name)
    if provider_name == "youtube":
        url = f"{YOUTUBE_CHANNELS_URL}?{urllib.parse.urlencode({'part': 'id,snippet', 'mine': 'true'})}"
        data = _get_json(url, access_token, transport=transport)
        items = data.get("items") or []
        if not items or not isinstance(items[0], dict) or not items[0].get("id"):
            raise ProviderError(
                "YouTube channel verification returned no channel; "
                "grant youtube.readonly (or youtube) scope or verify the Brand Account"
            )
        snippet = items[0].get("snippet") or {}
        return items[0]["id"], snippet.get("title") or snippet.get("customUrl") or items[0]["id"]
    url = DROPBOX_ACCOUNT_URL
    transport = transport or _default_transport
    try:
        status, raw = transport(
            "POST", url,
            headers={"authorization": f"Bearer {access_token}", "content-type": "application/json"},
            body=b"null", timeout=15,
        )
    except Exception as exc:
        raise ProviderError(f"provider identity check unreachable: {type(exc).__name__}")
    try:
        data = json.loads(raw.decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        raise ProviderError(f"provider identity check returned HTTP {status}")
    if status >= 300 or not isinstance(data, dict) or not data.get("account_id"):
        raise ProviderError(f"provider identity check returned HTTP {status}: {redact(data)}")
    name = data.get("name") or {}
    return data["account_id"], name.get("display_name") or data.get("email") or data["account_id"]


def revoke_token(provider_name, token, *, transport=None):
    """Best-effort provider-side revocation. Returns True when revoked."""
    get(provider_name)
    transport = transport or _default_transport
    try:
        if provider_name == "youtube":
            status, _ = transport(
                "POST", GOOGLE_REVOKE_URL,
                headers={"content-type": "application/x-www-form-urlencoded"},
                body=urllib.parse.urlencode({"token": token}).encode(), timeout=15,
            )
        else:
            status, _ = transport(
                "POST", DROPBOX_REVOKE_URL,
                headers={"authorization": f"Bearer {token}", "content-type": "application/json"},
                body=b"null", timeout=15,
            )
    except Exception:
        return False
    return status < 300


def redact(value):
    """Render ``value`` safe for logs and audit records (no secrets)."""
    if isinstance(value, dict):
        return {
            key: ("[redacted]" if key.lower() in _SECRET_FIELDS else redact(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        scrubbed = value
        for field in _SECRET_FIELDS:
            if field in scrubbed:
                return "[redacted string]"
        return scrubbed if len(scrubbed) <= 300 else scrubbed[:300] + "…"
    return value
