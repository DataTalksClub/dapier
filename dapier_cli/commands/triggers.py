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
    addresses = data.get("addresses") or []
    if not addresses and not data.get("subscriptions") and not data.get("watchers"):
        print("No email entry points. Create one with `dapier workflows save` and publish it.")
    for row in addresses:
        for handler in row.get("handlers") or []:
            print(f"{row['address']:34} {handler['status']:12} workflow={handler['workflow']} "
                  + " → ".join(handler.get("action_types") or [])
                  + (" [unpublished changes]" if handler.get("has_draft") else "")
                  + (" [convert: dapier emails migrate " + handler['legacy_name'] + "]" if handler.get("legacy") else ""))
    for handler in data.get("subscriptions") or []:
        print(f"Address rule: {json.dumps(handler['filters'].get('route', 'all'))} "
              f"{handler['status']} workflow={handler['workflow']}")
    for handler in data.get("watchers") or []:
        print(f"Feedback: {handler['event']} {handler['status']} workflow={handler['workflow']}")
    return 0


def triggers_show(api_url, name, debug=False):
    data = api.call(api_url, "GET", "/api/agent/email-triggers", debug=debug)
    item = next((row for row in data.get("addresses", [])
                 if name in (row.get("name"), row.get("address"))), None)
    if item is None:
        print(f"No email address named '{name}'.")
        return 4
    print(json.dumps(item, indent=2))
    return 0


def triggers_save(api_url, path, debug=False):
    print("Email flows are defined only in Workflows. Use `dapier workflows save flow.yaml`, then `dapier workflows publish <id>`.")
    return 2


def triggers_delete(api_url, name, debug=False):
    print("Edit or delete the owning workflow with `dapier workflows`. Email addresses have no separate flow definition.")
    return 2


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
