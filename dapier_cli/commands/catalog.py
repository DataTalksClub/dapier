"""Implementation of the `dapier catalog` command."""

import json

from .. import api

__all__ = ["catalog_show"]


def catalog_show(api_url, debug=False, as_json=False):
    """The action/trigger/logic catalog behind the designer palette (public, read-only)."""
    data = api.call(api_url, "GET", "/api/catalog", debug=debug)
    if as_json:
        print(json.dumps(data, indent=2))
        return 0
    print("Actions:")
    for entry in data.get("actions", []):
        print(f"  {entry.get('type', ''):20} {entry.get('label', '')}")
    print("Trigger connectors:")
    for entry in data.get("connectors", []):
        info = entry.get("event_info") or []
        if not info:
            events = ", ".join(entry.get("events") or []) or "(any event)"
            print(f"  {entry.get('name', ''):20} {entry.get('label', ''):20} {events}")
            continue
        print(f"  {entry.get('name', ''):20} {entry.get('label', '')}")
        for event in info:
            description = event.get("description") or ""
            line = f"    {event.get('event', ''):32} {event.get('label', '')}"
            print(f"{line} — {description}" if description else line)
    return 0



