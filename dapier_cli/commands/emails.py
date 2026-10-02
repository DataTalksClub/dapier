"""Implementations of the `dapier emails` commands."""

from urllib.parse import quote

from .. import api

__all__ = ["emails_from_add", "emails_from_list", "emails_from_remove"]


def emails_from_list(api_url, debug=False):
    data = api.call(api_url, "GET", "/api/agent/email-from", debug=debug)
    addresses = data.get("addresses") or []
    if not addresses:
        print("No senders. An empty list ignores every message.")
        return 0
    for address in addresses:
        print(address)
    return 0


def emails_from_add(api_url, address, debug=False):
    data = api.call(api_url, "POST", "/api/agent/email-from",
                    {"address": address}, debug=debug)
    print("Already on the list." if not data.get("added") else f"Added {address}.")
    return 0


def emails_from_remove(api_url, address, debug=False):
    data = api.call(api_url, "DELETE",
                    f"/api/agent/email-from?address={quote(address, safe='')}",
                    debug=debug)
    print(f"Removed {address}." if data.get("removed") else f"{address} was not on the list.")
    return 0
