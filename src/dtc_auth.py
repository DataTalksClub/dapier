"""DTC shared-auth helpers (used by browser login and CLI Bearer verification)."""

import json
import os
import urllib.parse
import urllib.request


def auth_config():
    issuer = os.environ.get("AUTH_ISSUER", "").rstrip("/")
    return {
        "base_url": os.environ.get("AUTH_BASE_URL", "").rstrip("/"),
        "client_id": os.environ.get("AUTH_CLIENT_ID", ""),
        "cli_client_id": os.environ.get("AUTH_CLI_CLIENT_ID", "").strip(),
        "callback_url": os.environ.get("AUTH_CALLBACK_URL", ""),
        "logout_url": os.environ.get("AUTH_LOGOUT_URL", ""),
        "issuer": issuer,
        "jwks_url": os.environ.get("AUTH_JWKS_URL", f"{issuer}/.well-known/jwks.json" if issuer else ""),
    }


def exchange_auth_code(code, verifier):
    config = auth_config()
    body = urllib.parse.urlencode({
        "grant_type": "authorization_code", "client_id": config["client_id"],
        "code": code, "redirect_uri": config["callback_url"], "code_verifier": verifier,
    }).encode()
    request = urllib.request.Request(
        f'{config["base_url"]}/oauth2/token', data=body,
        headers={"content-type": "application/x-www-form-urlencoded"}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read())


def verify_id_token(id_token, audience=None):
    """Verify a DTC-issued ID token. Checks issuer, audience, expiry, subject."""
    import jwt

    config = auth_config()
    key = jwt.PyJWKClient(config["jwks_url"]).get_signing_key_from_jwt(id_token)
    return jwt.decode(
        id_token, key.key, algorithms=["RS256"],
        audience=audience or config["client_id"],
        issuer=config["issuer"], options={"require": ["exp", "iat", "iss", "aud", "sub"]},
    )
