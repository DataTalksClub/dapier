"""Implementations of the `dapier oauth-clients` commands."""

import sys

from .. import api

__all__ = ["oauth_clients_list", "oauth_clients_set", "print_oauth_clients"]


def print_oauth_clients(items):
    print(f"{'PROVIDER':12} {'CLIENT ID':46} {'SOURCE':8} CONFIGURED")
    for item in items:
        client_id = item.get("client_id") or "-"
        print(f"{item.get('provider', ''):12} {client_id:46} "
              f"{item.get('source', ''):8} {'yes' if item.get('configured') else 'no'}")


def oauth_clients_list(api_url, debug=False):
    data = api.call(api_url, "GET", "/api/agent/oauth-clients", debug=debug)
    print_oauth_clients(data.get("clients") or [])
    return 0


def oauth_clients_set(api_url, provider, client_id, secret_path, debug=False):
    """Store the shared OAuth client; the secret travels only in the request body."""
    try:
        with (sys.stdin if secret_path == "-" else open(secret_path, encoding="utf-8")) as handle:
            client_secret = handle.read().strip()
    except OSError as exc:
        print(f"Cannot read {secret_path}: {exc}")
        return 2
    if not client_id.strip() or not client_secret:
        print("Both --client-id and the client secret (from --client-secret-file) are required.")
        return 2
    data = api.call(api_url, "PUT", f"/api/agent/oauth-clients/{provider}",
                    {"client_id": client_id, "client_secret": client_secret}, debug=debug)
    print(f"Stored the OAuth client for {data.get('provider', provider)}. "
          "It is live immediately; the secret is never shown again.")
    return 0


