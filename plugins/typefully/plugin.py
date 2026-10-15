"""Read-only Typefully v2 account discovery using a write-only API credential."""
import json
from src.dapier.connections import credentials
from src.dapier.connectors.registry import Discovery, ConnectionTest, register_discovery, register_connection_test
from src.dapier.engine.actions import base

API_ROOT = "https://api.typefully.com/v2"


def request(path, connection=None, *, transport=None):
    secret = credentials.get_credential((connection or {}).get("credential_id") or "typefully")
    key = secret.get("api_key")
    if not isinstance(key, str) or not key.strip():
        raise ValueError("Typefully API key is not configured")
    try:
        status, raw = (transport or base._default_transport)(
            "GET", API_ROOT + path, headers={"authorization": "Bearer " + key, "accept": "application/json"},
            body=None, timeout=15)
    except Exception:
        raise ValueError("Typefully is unreachable") from None
    if status != 200:
        raise ValueError(f"Typefully returned HTTP {status}")
    try:
        data = raw if isinstance(raw, dict) else json.loads(raw)
    except (ValueError, TypeError):
        raise ValueError("Typefully returned invalid JSON") from None
    if not isinstance(data, dict):
        raise ValueError("Typefully returned an invalid response")
    return data


def social_sets(connection, params):
    # Construct each offset locally; never forward credentials to a response URL.
    items = []
    for offset in range(0, 10000, 50):
        data = request(f"/social-sets?limit=50&offset={offset}", connection)
        page = data.get("results")
        if not isinstance(page, list):
            raise ValueError("Typefully returned invalid social sets")
        for item in page:
            if isinstance(item, dict) and item.get("id") is not None:
                items.append({"id": str(item["id"]), "name": item.get("name") or item.get("username") or str(item["id"]),
                              "username": item.get("username"), "team": item.get("team")})
        if not data.get("next"):
            return items
        if not page:
            raise ValueError("Typefully returned an empty page with more results")
    raise ValueError("Typefully social set listing exceeded the pagination limit")


def health(connection):
    try:
        user = request("/me", connection)
        if not user.get("id"):
            raise ValueError("Typefully returned no user identity")
        return {"ok": True, "detail": "Typefully API key verified", "identity": {
            "id": user["id"], "name": user.get("name"), "email": user.get("email")}}
    except Exception:
        # Provider bodies and transport errors can contain secrets; never echo them.
        return {"ok": False, "detail": "Typefully key check failed; check the saved key and provider availability"}


register_discovery(Discovery(name="social_sets", connector="typefully", label="Social sets",
    description="Typefully social accounts available to the saved API key", run=social_sets))
register_connection_test(ConnectionTest(connector="typefully", run=health))


from src.dapier.connectors.trigger_discovery import (
    TriggerDiscovery, register_trigger_discovery, options_from_registry, DEFAULT_LIMIT,
)


def social_set_options(event=None, connection_id=None, limit=DEFAULT_LIMIT):
    if connection_id == "typefully":
        connection_id = None
    return options_from_registry("typefully.social_sets", connection_id, limit,
        provider="typefully", credential_fallback="typefully")


register_trigger_discovery(TriggerDiscovery(connector="typefully", label="Typefully",
    kind="options", resource="typefully.social_sets", fetch=social_set_options))
