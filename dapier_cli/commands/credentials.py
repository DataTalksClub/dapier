"""Implementation of the `dapier credentials set` command."""

import json, sys

from .. import api

__all__ = ["credentials_list", "credentials_set"]


CREDENTIAL_FIELDS = {"typefully": ["api_key"], "taskdeck": ["token"], "slack": ["token"],
                     "aws": ["role_arn"],
                     "openrouter": ["api_key"]}


def credentials_set(api_url, provider, path, debug=False):
    """Store a provider credential; the value travels only in the request body.

    Single-field providers take the raw value; aws takes a JSON object with
    a role_arn (plus optional external_id, region, buckets).
    """
    fields = CREDENTIAL_FIELDS.get(provider)
    if not fields:
        known = ", ".join(sorted(CREDENTIAL_FIELDS))
        print(f"Unknown credential provider '{provider}'. Known providers: {known}.")
        return 2
    try:
        with (sys.stdin if path == "-" else open(path, encoding="utf-8")) as handle:
            value = handle.read().strip()
    except OSError as exc:
        print(f"Cannot read {path}: {exc}")
        return 2
    if not value:
        print("The credential value is empty.")
        return 2
    if provider == "aws":
        try:
            body = json.loads(value)
        except ValueError:
            print("The aws credential must be a JSON object with a role_arn key "
                  "(plus optional external_id, region, buckets).")
            return 2
        if not isinstance(body, dict) or not str(body.get("role_arn") or "").strip():
            print("The aws credential must be a JSON object with a role_arn key "
                  "(plus optional external_id, region, buckets).")
            return 2
    elif len(fields) == 1:
        body = {fields[0]: value}
    else:
        try:
            parsed = json.loads(value)
        except ValueError:
            print(f"The {provider} credential must be a JSON object with keys: "
                  f"{', '.join(fields)}.")
            return 2
        if not isinstance(parsed, dict) or sorted(parsed) != sorted(fields) or not all(
                isinstance(parsed.get(field), str) and parsed.get(field).strip()
                for field in fields):
            print(f"The {provider} credential must be a JSON object with keys: "
                  f"{', '.join(fields)}.")
            return 2
        body = parsed
    data = api.call(api_url, "PUT", f"/api/agent/credentials/{provider}",
                    body, debug=debug)
    print(f"Stored the {data.get('provider', provider)} credential. "
          "It is live immediately; the value is never shown again.")
    return 0


def credentials_list(api_url, debug=False):
    """Every stored credential's shape: key names only, never values."""
    data = api.call(api_url, "GET", "/api/agent/credentials", debug=debug)
    shapes = data.get("credentials") or []
    if not shapes:
        print("No credentials stored.")
        return 0
    width = max(len(shape.get("credential_id") or "") for shape in shapes)
    for shape in shapes:
        keys = ", ".join(shape.get("keys") or []) or "(no named keys)"
        updated = (shape.get("updated_at") or "").split(".")[0]
        print(f"{shape.get('credential_id') or '':<{width}}  "
              f"{shape.get('provider') or '?':<12} {updated:<20} {keys}")
    return 0

