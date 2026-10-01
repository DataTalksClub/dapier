"""Implementations of the `dapier tokens` commands."""

import os
from pathlib import Path
from urllib.parse import urlencode

from .. import api

__all__ = ["print_tokens", "tokens_create", "tokens_delete", "tokens_list", "tokens_revoke"]


def print_tokens(items):
    print(f"{'TOKEN':24} {'AGENT':24} {'STATUS':10} {'CREATED':20} LAST USED")
    for item in items:
        status = "revoked" if item.get("revoked_at") else "active"
        created = (item.get("created_at") or "-")[:19]
        last_used = (item.get("last_used_at") or "never")[:19]
        print(f"{item.get('token_id', ''):24} {item.get('agent', ''):24} "
              f"{status:10} {created:20} {last_used}")


def tokens_list(api_url, debug=False):
    data = api.call(api_url, "GET", "/api/agent/tokens", debug=debug)
    items = data.get("tokens", [])
    if not items:
        print("No API tokens. Issue one with `dapier tokens create`.")
        return 0
    print_tokens(items)
    return 0


def tokens_create(api_url, name, agent, debug=False, output=None):
    if output:
        if Path(output).expanduser().exists():
            raise ValueError(f"Token file already exists: {output}")
    data = api.call(api_url, "PUT", "/api/agent/tokens",
                    {"token_id": name, "agent": agent}, debug=debug)
    print(f"Created API token {data.get('token_id')} "
          f"(subject {data.get('subject')}, agent {data.get('agent')}).")
    if agent != "host-worker":
        print(f"Grant it access with `dapier grants save` using subject {data.get('subject')}.")
    if output:
        path = Path(output).expanduser()
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data["token"] + "\n")
        print(f"Stored the one-time token in {path} (owner-only).")
    else:
        print("Store the value now; it is not retrievable again:")
        print(data.get("token", ""))
    return 0


def tokens_revoke(api_url, name, debug=False):
    query = urlencode({"token_id": name})
    api.call(api_url, "DELETE", f"/api/agent/tokens?{query}", debug=debug)
    print(f"Revoked API token {name}. Presented values stop authenticating immediately.")
    return 0


def tokens_delete(api_url, name, debug=False):
    query = urlencode({"token_id": name, "purge": "1"})
    data = api.call(api_url, "DELETE", f"/api/agent/tokens?{query}", debug=debug)
    grants = data.get("grants_removed") or 0
    suffix = f" and {grants} connection grant{'s' if grants != 1 else ''}" if grants else ""
    print(f"Removed revoked API token {name}{suffix}.")
    return 0


