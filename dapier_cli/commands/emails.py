"""Implementations of the `dapier emails` commands."""

from urllib.parse import quote

from .. import api

__all__ = [
    "emails_from_add", "emails_from_list", "emails_from_mode", "emails_from_remove",
]


def _route_arg(value):
    name = str(value or "").strip().lower()
    if "@" in name:
        name = name.split("@", 1)[0].strip()
    return name


def _print_policy(name, policy, *, inherited=False):
    mode = (policy or {}).get("mode") or "allowlist"
    suffix = ", default" if inherited else ""
    print(f"{name} ({mode}{suffix})")
    if mode == "anyone":
        return
    addresses = (policy or {}).get("addresses") or []
    if not addresses:
        print("  (empty — every message is refused)")
        return
    for address in addresses:
        print(f"  {address}")


def emails_from_list(api_url, debug=False, route=None):
    data = api.call(api_url, "GET", "/api/agent/email-from", debug=debug)
    default = data.get("default") or {"mode": "allowlist", "addresses": data.get("addresses") or []}
    routes = data.get("routes") or {}
    name = _route_arg(route) if route else ""
    if name:
        if name in routes:
            _print_policy(name, routes[name])
        else:
            _print_policy(name, default, inherited=True)
        return 0
    _print_policy("default", default)
    for item in sorted(routes):
        _print_policy(item, routes[item])
    return 0


def emails_from_add(api_url, address, debug=False, route=None):
    body = {"address": address}
    name = _route_arg(route)
    if name:
        body["route"] = name
    data = api.call(api_url, "POST", "/api/agent/email-from", body, debug=debug)
    target = f" to {name}" if name else ""
    print("Already on the list." if not data.get("added") else f"Added {address}{target}.")
    return 0


def emails_from_remove(api_url, address, debug=False, route=None):
    name = _route_arg(route)
    path = f"/api/agent/email-from?address={quote(address, safe='')}"
    if name:
        path += f"&route={quote(name, safe='')}"
    data = api.call(api_url, "DELETE", path, debug=debug)
    target = f" from {name}" if name else ""
    if data.get("removed"):
        print(f"Removed {address}{target}.")
    else:
        print(f"{address} was not on the list.")
    return 0


def emails_from_mode(api_url, name, mode, debug=False):
    route = _route_arg(name)
    api.call(api_url, "POST", "/api/agent/email-from",
             {"route": route, "mode": mode}, debug=debug)
    if mode == "anyone":
        print(f"{route} now accepts anyone.")
    else:
        print(f"{route} now uses an allow-list.")
    return 0
