"""Implementations of the `dapier triggers` commands (email triggers and samples)."""

import json
from urllib.parse import quote

from .. import api
from .shared import entry_label, print_flows, read_json_file

__all__ = ["print_trigger", "triggers_delete", "triggers_list", "triggers_sample", "triggers_save", "triggers_show", "triggers_workflow_sample"]


def print_trigger(item):
    for key in ("name", "address", "description", "flow", "enabled", "created_by",
                "created_at", "updated_at"):
        if item.get(key) not in (None, ""):
            print(f"{key}: {item[key]}")
    for index, action in enumerate(item.get("actions") or [], 1):
        print(f"action[{index}]: {json.dumps(action, sort_keys=True)}")


def triggers_list(api_url, debug=False):
    data = api.call(api_url, "GET", "/api/agent/email-triggers", debug=debug)
    domain = data.get("domain", "")
    # Workflow-claimed routes sit in the same list: every address the domain
    # answers is one row, and each row says what runs when mail arrives.
    entries = [
        (item.get("name", ""), item.get("address", ""),
         "yes" if item.get("enabled", True) else "no", entry_label(item))
        for item in data.get("triggers", [])
    ]
    entries += [
        (route.get("name", ""), f"{route.get('name', '')}@{domain}",
         "yes" if route.get("status", "enabled") == "enabled" else "no",
         f"workflow={route.get('workflow', '')} (edit: dapier workflows)")
        for route in data.get("managed_routes") or []
    ]
    if not entries:
        print("No email triggers yet. Create one with `dapier triggers save`.")
    for name, address, enabled, label in sorted(entries):
        print(f"{name:20} {address:34} enabled={enabled:3} {label}")
    print_flows(data.get("flows") or [])
    return 0


def triggers_show(api_url, name, debug=False):
    data = api.call(api_url, "GET", "/api/agent/email-triggers", debug=debug)
    item = next((t for t in data.get("triggers", []) if t.get("name") == name), None)
    if item is None:
        print(f"No trigger named '{name}'.")
        return 4
    print_trigger(item)
    return 0


def triggers_save(api_url, path, debug=False):
    body, error = read_json_file(path)
    if error:
        print(error)
        return 2
    data = api.call(api_url, "PUT", "/api/agent/email-triggers", body, debug=debug)
    verb = "Created" if data.get("created") else "Updated"
    print(f"{verb} {data.get('address') or data.get('name')}. "
          "It is live immediately; no deploy needed.")
    return 0


def triggers_delete(api_url, name, debug=False):
    data = api.call(api_url, "DELETE", f"/api/agent/email-triggers?name={name}", debug=debug)
    print(f"Deleted {data.get('address') or name}.")
    return 0


def triggers_sample(api_url, connector, event=None, connection_id=None, limit=None,
                    resource=None, debug=False):
    """Pull a sample event for a trigger connector (Zapier's 'pull in sample
    data'): live from the connected account, else the newest recorded run,
    else a documented example. --resource fetches a field's option list."""
    body = {"connector": connector, "kind": "options" if resource else "sample"}
    if resource:
        body["resource"] = resource
    if event:
        body["event"] = event
    if connection_id:
        body["connection_id"] = connection_id
    if limit:
        body["limit"] = limit
    data = api.call(api_url, "POST", "/api/agent/discover", body, debug=debug)
    if resource:
        print(f"{data.get('connector')}.{data.get('resource')} "
              f"({data.get('connection_id') or 'no connection'}):")
        for option in data.get("options") or []:
            print(f"{option.get('value', ''):34} {option.get('label', '')}")
        return 0
    sample = data.get("sample") or {}
    print(f"{data.get('connector')}.{sample.get('event')} "
          f"[{data.get('source')}] {sample.get('occurred_at', '')}")
    print(json.dumps(sample.get("data"), indent=2, sort_keys=True))
    return 0


def triggers_workflow_sample(api_url, workflow, debug=False):
    """The workflow's own last trigger input, for filling {trigger.*}
    templates: the newest run's recorded input, else the trigger-discovery
    sample for the workflow's connector. The `--workflow` mode of
    `triggers sample`; the designer's inspector offers the same fields as
    click-to-insert chips."""
    data = api.call(api_url, "GET",
                    f"/api/agent/triggers/sample?workflow={quote(workflow)}",
                    debug=debug)
    print(f"{data.get('connector') or '?'}.{data.get('event') or '?'} "
          f"[{data.get('source') or '?'}] {data.get('occurred_at') or ''}")
    fields = data.get("data") if isinstance(data.get("data"), dict) else {}
    print(json.dumps(fields, indent=2, sort_keys=True))
    for key in sorted(fields):
        print(f"{{trigger.{key}}}")
    return 0
