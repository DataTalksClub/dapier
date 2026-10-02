"""Implementation of the `dapier overview` command."""


import json

from .. import api

__all__ = ["overview", "print_overview"]


def print_overview(data):
    print(f"{data.get('service', 'dapier')} in {data.get('region', '-')}")
    workflows = data.get("workflows") or []
    on = sum(1 for item in workflows if item.get("enabled", True))
    print(f"\nWORKFLOWS ({on}/{len(workflows)} On)")
    for item in workflows:
        state = "On" if item.get("enabled", True) else "Off"
        if item.get("auto_paused"):
            state += " (auto-paused)"
        print(f"  {item.get('id', ''):32} {state}")
    print("\nCONNECTIONS")
    for item in data.get("connections") or []:
        print(f"  {item.get('connection_id', ''):24} {item.get('provider', ''):10} "
              f"{item.get('status', '')}")
    print("\nCREDENTIALS")
    for item in data.get("credentials") or []:
        state = "configured" if item.get("configured") else "not set"
        updated = f" (updated {item['updated_at']})" if item.get("updated_at") else ""
        print(f"  {item.get('provider', '?'):12} {state}{updated}")
    oauth_clients = data.get("oauth_clients") or []
    if oauth_clients:
        print("\nOAUTH CLIENTS")
        for item in oauth_clients:
            state = f"configured ({item.get('source')})" if item.get("configured") else "not set"
            print(f"  {item.get('provider', '?'):12} {state}")
    executions = (data.get("executions") or [])[:10]
    if executions:
        print(f"\nRECENT EXECUTIONS ({len(executions)} latest)")
        for item in executions:
            print(f"  {item.get('workflow_id', '?')}.{item.get('action_id', '?'):24} "
                  f"{item.get('status', ''):12} {item.get('started_at', '')}")


def overview(api_url, debug=False, section=None):
    path = "/api/agent/overview"
    if section:
        path += f"?section={section}"
    data = api.call(api_url, "GET", path, debug=debug)
    if section:
        print(json.dumps(data, indent=2))
    else:
        print_overview(data)
    return 0


