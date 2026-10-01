"""Implementation of the `dapier credentials set` command."""

import json, sys

from .. import api

__all__ = ["credentials_set"]


CREDENTIAL_FIELDS = {"slack": ["token"], "mailchimp": ["api_key"],
                     "aws": ["access_key_id", "secret_access_key"]}


def credentials_set(api_url, provider, path, debug=False):
    """Store a provider credential; the value travels only in the request body.

    Single-field providers take the raw value; multi-field providers (aws)
    take a JSON object with exactly their fields.
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
    if len(fields) == 1:
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


