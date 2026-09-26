"""Implementations of the `dapier connections|token` commands."""

import json
import os
import subprocess
import sys
import time
import webbrowser
from urllib.parse import quote, urlencode

from . import api

# Documented child-process variables. The provider-specific alias exists so
# existing upload tooling works; DAPIER_ACCESS_TOKEN is always set too.
TOKEN_ENV_VARS = {
    "youtube": ("DAPIER_ACCESS_TOKEN", "YOUTUBE_ACCESS_TOKEN"),
    "dropbox": ("DAPIER_ACCESS_TOKEN", "DROPBOX_ACCESS_TOKEN"),
}


def print_connections(items):
    print(f"{'CONNECTION':24} {'PROVIDER':10} {'STATUS':10} {'ACCOUNT'}")
    for item in items:
        account = item.get("account_title") or item.get("verified_account_id") or "-"
        scopes = ",".join((item.get("granted_scopes") or item.get("scopes") or [])[:2])
        extra = f" [{scopes}]" if scopes else ""
        print(f"{item.get('connection_id', ''):24} {item.get('provider', ''):10} "
              f"{item.get('status', ''):10} {account}{extra}")


def print_connection(item):
    for key in ("connection_id", "provider", "display_name", "status",
                "verified_account_id", "account_title", "expected_account_id",
                "granted_scopes", "scopes", "version", "updated_at", "connected_at"):
        if item.get(key) not in (None, "", []):
            value = item[key]
            if isinstance(value, list):
                value = " ".join(value)
            print(f"{key}: {value}")


def connections_list(api_url, debug=False):
    data = api.call(api_url, "GET", "/api/agent/connections", debug=debug)
    items = data.get("connections", [])
    if not items:
        print("No connection grants. Ask an operator for access.")
        return 0
    print_connections(items)
    return 0


def connections_show(api_url, connection_id, agent=None, debug=False):
    path = f"/api/agent/connections/{connection_id}"
    if agent:
        path += f"?agent={agent}"
    try:
        item = api.call(api_url, "GET", path, debug=debug)
    except api.ApiError as exc:
        if exc.status == 404 and agent is None:
            print("Connection not found or no grant. Pass --agent <name> to check a specific agent.")
            return 4
        raise
    print_connection(item)
    return 0


def connections_connect(api_url, connection_id, agent, timeout=300, debug=False):
    data = api.call(api_url, "POST", f"/api/agent/connections/{connection_id}/connect",
                    {"agent": agent}, debug=debug)
    url = data.get("authorize_url", "")
    print("Opened the browser for provider consent.")
    print("If it did not open, visit this URL (it expires in 10 minutes):")
    print(url)
    webbrowser.open(url)
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(5)
        try:
            item = api.call(api_url, "GET",
                            f"/api/agent/connections/{connection_id}?agent={agent}")
        except api.ApiError:
            continue
        if item.get("status") == "connected":
            print(f"Connected {connection_id} "
                  f"({item.get('account_title') or item.get('verified_account_id')}).")
            return 0
    print("Timed out waiting for consent. Re-run `dapier connections connect` to retry.")
    return 5


def connections_create(api_url, connection_id, provider, scopes, *, display_name=None,
                       root_path=None, debug=False):
    body = {
        "connection_id": connection_id,
        "provider": provider,
        "scopes": list(scopes),
    }
    if display_name:
        body["display_name"] = display_name
    if root_path is not None:
        body["root_path"] = root_path
    data = api.call(api_url, "PUT", "/api/agent/connections", body, debug=debug)
    created_id = data.get("connection_id", connection_id)
    print(
        f"Created {created_id}. Start consent with `dapier connections connect "
        f"{created_id} --agent <agent-name>`."
    )
    return 0


def connections_edit(api_url, connection_id, *, display_name=None, scopes=None,
                     root_path=None, debug=False):
    body = {}
    if display_name is not None:
        body["display_name"] = display_name
    if scopes is not None:
        body["scopes"] = list(scopes)
    if root_path is not None:
        body["root_path"] = root_path
    if not body:
        print("Provide --display-name, --scopes, --root-path, or --clear-root-path.")
        return 2
    api.call(api_url, "PUT", f"/api/agent/connections/{connection_id}", body, debug=debug)
    print(f"Updated {connection_id}.")
    if "scopes" in body:
        print("Reconnect it to grant the updated scopes.")
    return 0


def connections_scopes(api_url, connection_id, scopes, debug=False):
    api.call(api_url, "PUT", f"/api/agent/connections/{connection_id}/scopes",
             {"scopes": list(scopes)}, debug=debug)
    print(f"Updated requested scopes for {connection_id}. Reconnect it to grant the new scopes.")
    return 0


def _fetch_token(api_url, connection_id, agent, debug=False):
    """Return ``(connection_view, token_data)`` after verifying the account binding."""
    view = api.call(api_url, "GET",
                    f"/api/agent/connections/{connection_id}?agent={agent}", debug=debug)
    expected = {value for value in (view.get("expected_account_id"), view.get("verified_account_id")) if value}
    if not expected:
        raise api.ApiError(
            f"Connection {connection_id} has no verified provider account; "
            "ask an operator to connect it first", status=4,
        )
    token = api.call(api_url, "POST", "/api/agent/token",
                     {"connection_id": connection_id, "agent": agent}, debug=debug)
    if token.get("provider_account_id") not in expected:
        raise api.ApiError(
            f"Provider account {token.get('provider_account_id')} does not match "
            f"connection {connection_id}; refusing to hand out the token",
            status=4,
        )
    return view, token


def token_exec(api_url, connection_id, agent, argv, debug=False):
    if not argv:
        print("No command given after `--`.")
        return 2
    view, token = _fetch_token(api_url, connection_id, agent, debug)
    names = TOKEN_ENV_VARS.get(view.get("provider", ""), ("DAPIER_ACCESS_TOKEN",))
    env = dict(os.environ)
    for name in names:
        env[name] = token["access_token"]
    # The token lives only in the child's environment; it is never printed,
    # never placed in argv, and the parent environment is left untouched.
    completed = subprocess.run(argv, env=env)
    return completed.returncode


def token_write(api_url, connection_id, agent, output, force=False, debug=False):
    _, token = _fetch_token(api_url, connection_id, agent, debug)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if force else os.O_EXCL)
    try:
        fd = os.open(output, flags, 0o600)
    except FileExistsError:
        print(f"Refusing to overwrite {output} (pass --force to replace it).")
        return 2
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(token["access_token"] + "\n")
    finally:
        try:
            os.chmod(output, 0o600)
        except OSError:
            pass
    print(output)
    return 0


TOKEN_PROVIDERS = ("slack", "telegram", "zoom")


def connections_import(api_url, connection_id, provider, client_id, client_secret_file,
                       authorized_user_path, expected_account_id=None, scopes=(), debug=False,
                       token_path=None, root_path=None, display_name=None):
    """Operator import over a Bearer identity (operator allowlist enforced server-side)."""
    body = {
        "connection_id": connection_id,
        "provider": provider,
    }
    if display_name:
        body["display_name"] = display_name
    if provider in TOKEN_PROVIDERS:
        # Token providers paste their credential directly; a refresh-token
        # file makes no sense for them.
        if not token_path:
            print(f"{provider} connections import with --token-file (the pasted provider token).")
            return 2
        try:
            with open(token_path, encoding="utf-8") as handle:
                token = handle.read().strip()
        except OSError as exc:
            print(f"Cannot read {token_path}: {exc}")
            return 2
        if not token:
            print("The token file is empty.")
            return 2
        body["token"] = token
    else:
        if not authorized_user_path:
            print("OAuth providers import with --authorized-user-file (a refresh-token JSON).")
            return 2
        try:
            authorized_user = json.loads(open(authorized_user_path, encoding="utf-8").read())
        except (OSError, ValueError) as exc:
            print(f"Cannot read {authorized_user_path}: {exc}")
            return 2
        if not isinstance(authorized_user, dict) or not authorized_user.get("refresh_token"):
            print("The authorized-user file has no refresh token.")
            return 2
        body["authorized_user"] = {"refresh_token": authorized_user["refresh_token"]}
    client_secret = ""
    if client_secret_file:
        try:
            with open(client_secret_file, encoding="utf-8") as handle:
                client_secret = handle.read().strip()
        except OSError as exc:
            print(f"Cannot read {client_secret_file}: {exc}")
            return 2
        if not client_secret:
            print("The client-secret file is empty.")
            return 2
    # Omitted client credentials fall back to the shared deploy-time OAuth
    # client on the server; explicit ones are for tokens issued by another client.
    if client_id:
        body["client_id"] = client_id
    if client_secret:
        body["client_secret"] = client_secret
    if expected_account_id:
        body["expected_account_id"] = expected_account_id
    if scopes:
        body["scopes"] = list(scopes)
    if root_path is not None:
        body["root_path"] = root_path
    # Secrets travel only in the TLS request body to the operator endpoint;
    # they never appear in argv, logs, or output. The local files are only read.
    data = api.call(api_url, "POST", "/api/agent/connections/import", body, debug=debug)
    if provider == "zoom":
        print(f"Created {data.get('connection_id')}. Set Zoom's Event Notification Endpoint URL to "
              f"{api_url.rstrip('/')}/hooks/zoom/{data.get('connection_id')} and subscribe to recording.completed.")
    else:
        print(f"Imported {data.get('connection_id')} "
              f"({data.get('account_title') or data.get('verified_account_id')}).")
    return 0


def _entry_label(item):
    """What a trigger runs: its flow binding or its inline action types."""
    if item.get("flow"):
        return f"flow={item['flow']}"
    return ",".join(action.get("type", "?") for action in item.get("actions") or []) or "-"


def print_flows(flows):
    if flows:
        print("Shared flows (bind with \"flow\": \"<name>\"): "
              + ", ".join(f"{flow['name']} ({flow.get('actions', 0)} action(s))" for flow in flows))


def print_trigger(item):
    for key in ("name", "address", "description", "flow", "enabled", "created_by",
                "created_at", "updated_at"):
        if item.get(key) not in (None, ""):
            print(f"{key}: {item[key]}")
    for index, action in enumerate(item.get("actions") or [], 1):
        print(f"action[{index}]: {json.dumps(action, sort_keys=True)}")


def triggers_list(api_url, debug=False):
    data = api.call(api_url, "GET", "/api/agent/email-triggers", debug=debug)
    items = data.get("triggers", [])
    if not items:
        print("No email triggers yet. Create one with `dapier triggers save`.")
    for item in items:
        enabled = "yes" if item.get("enabled", True) else "no"
        print(f"{item.get('name', ''):20} {item.get('address', ''):34} "
              f"enabled={enabled:3} {_entry_label(item)}")
    routes = data.get("yaml_routes") or []
    if routes:
        print(f"Routes handled by YAML workflows (not editable here): {', '.join(routes)}")
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


def _read_json_file(path):
    """Return the parsed JSON body, or ``(None, error_message)``."""
    try:
        with (sys.stdin if path == "-" else open(path, encoding="utf-8")) as handle:
            return json.loads(handle.read()), None
    except OSError as exc:
        return None, f"Cannot read {path}: {exc}"
    except ValueError as exc:
        return None, f"{path} is not valid JSON: {exc}"


def triggers_save(api_url, path, debug=False):
    body, error = _read_json_file(path)
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


def workflows_list(api_url, debug=False):
    data = api.call(api_url, "GET", "/api/agent/designer/workflows", debug=debug)
    items = data.get("workflows", [])
    if not items:
        print("No workflows deployed yet. Draw one at /designer or run `dapier workflows save`.")
    for item in items:
        enabled = "yes" if item.get("enabled", True) else "no"
        live = "live" if item.get("published") else "pending-deploy"
        trigger = f"{item.get('connector', '?')}.{item.get('event', '?')}"
        extra = (item.get("triggerCount") or 1) - 1
        if extra > 0:
            trigger = f"{trigger} +{extra}"
        print(f"{item.get('source', ''):36} {trigger:34} "
              f"{item.get('actionCount', 0)} action(s) enabled={enabled} {live}")
    sync = data.get("git_sync") or {}
    target = f"{sync.get('repo', '?')} ({sync.get('branch', '?')} branch)"
    if sync.get("configured"):
        print(f"Designer saves commit straight to {target}.")
    else:
        print(f"Saves are disabled: git sync to {target} is not configured.")
    return 0


def workflows_show(api_url, file, debug=False):
    data = api.call(api_url, "GET", f"/api/agent/designer/workflows/{file}", debug=debug)
    print(json.dumps(data.get("workflow", {}), indent=2))
    return 0


def workflows_save(api_url, path, rename_from, debug=False):
    try:
        with (sys.stdin if path == "-" else open(path, encoding="utf-8")) as handle:
            yaml_text = handle.read()
    except OSError as exc:
        print(f"Cannot read {path}: {exc}")
        return 2
    return _save_workflow_yaml(api_url, yaml_text, rename_from, debug=debug)


def _save_workflow_yaml(api_url, yaml_text, rename_from, debug=False):
    """The `workflows save` wire call; also the --save tail of `workflows draft`."""
    body = {"yaml": yaml_text}
    if rename_from:
        body["renameFrom"] = rename_from
    data = api.call(api_url, "PUT", "/api/agent/designer/workflows", body, debug=debug)
    if data.get("published"):
        print(f"Committed {data.get('file')} ({str(data.get('commit', ''))[:7]}) and published it live.")
    else:
        print(f"Committed {data.get('file')} ({str(data.get('commit', ''))[:7]}). "
              "The deploy pipeline publishes it in a few minutes.")
    return 0


def workflows_draft(api_url, prompt, save=False, debug=False):
    """Copilot draft: print the model's YAML; --save pipes it through the
    existing save path (the API validates again there). The draft itself is
    never saved implicitly."""
    data = api.call(api_url, "POST", "/api/agent/copilot/draft", {"prompt": prompt},
                    timeout=120, debug=debug)
    yaml_text = data.get("yaml") or ""
    print(yaml_text)
    errors = data.get("errors") or []
    if errors:
        print("\nValidation errors (a save would reject this draft):")
        for error in errors:
            print(f"  - {error}")
    if not save:
        return 0
    if errors or not yaml_text:
        print("Not saving: fix the errors above, then run `dapier workflows save <file>`.")
        return 5
    return _save_workflow_yaml(api_url, yaml_text, None, debug=debug)


def workflows_set_enabled(api_url, file, enabled, debug=False):
    data = api.call(api_url, "PUT", f"/api/agent/designer/workflows/{file}",
                    {"enabled": enabled}, debug=debug)
    state = "Enabled" if enabled else "Disabled"
    print(f"{state} {data.get('file') or file} — live now.")
    if data.get("commit"):
        print(f"Committed {str(data['commit'])[:7]}.")
    if data.get("git_sync_error"):
        print(f"Warning: the git commit failed ({data['git_sync_error']}); "
              "the next deploy may revert this toggle.")
    return 0


def workflows_test(api_url, path, event_spec, execute=False, debug=False):
    """Test-run a workflow YAML against a sample event, via the agent API.

    Default is a dry-run: per-step rendered inputs with no side effects.
    --execute really runs the actions. Exits 0 when every step is ok and a
    trigger actually matched; 1 when the run found problems (a failed step,
    an unsupported action, or no trigger match), so it can gate scripts.
    """
    try:
        with (sys.stdin if path == "-" else open(path, encoding="utf-8")) as handle:
            yaml_text = handle.read()
    except OSError as exc:
        print(f"Cannot read {path}: {exc}")
        return 2
    try:
        if event_spec.startswith("@"):
            with open(event_spec[1:], encoding="utf-8") as handle:
                sample = json.load(handle)
        else:
            sample = json.loads(event_spec)
    except OSError as exc:
        print(f"Cannot read {event_spec[1:]}: {exc}")
        return 2
    except ValueError as exc:
        print(f"Invalid sample event JSON: {exc}")
        return 2
    if not isinstance(sample, dict):
        print("The sample event must be a JSON object.")
        return 2
    data = api.call(api_url, "POST", "/api/agent/designer/workflows/test",
                    {"yaml": yaml_text, "event": sample, "execute": execute}, debug=debug)
    label = data.get("file") or path
    matched = bool(data.get("matched"))
    enabled = bool(data.get("enabled", True))
    print(f"{'Executed' if data.get('mode') == 'execute' else 'Dry run'}: {label} — "
          f"{'a trigger matches' if matched else 'NO trigger matches'} the sample event"
          + ("" if enabled else " (the workflow is disabled)"))
    for index, step in enumerate(data.get("steps") or [], 1):
        head = f"  {index}. {step.get('action_id', '?')} ({step.get('action_type', '?')})"
        if step.get("ok"):
            print(f"{head}: ok")
        else:
            print(f"{head}: {step.get('error') or 'failed'}")
        if step.get("rendered_input") is not None:
            print(f"     input: {json.dumps(step['rendered_input'], sort_keys=True)}")
        if step.get("output") is not None:
            print(f"     output: {json.dumps(step['output'], sort_keys=True)}")
    if data.get("error"):
        print(f"Run error: {data['error']}")
    if not data.get("ok") or not matched:
        if data.get("ok") and not matched:
            print("Nothing would run: no trigger matches this event.")
        return 1
    return 0


def print_hook(item):
    for key in ("hook_id", "kind", "url", "connection_id", "description", "flow",
                "enabled", "created_by", "created_at", "updated_at"):
        if item.get(key) not in (None, ""):
            print(f"{key}: {item[key]}")
    if item.get("kind") == "telegram":
        print("secret header: x-telegram-bot-api-secret-token (managed by Telegram)")
    else:
        print(f"auth header: {item.get('header', 'authorization')}: Bearer {item.get('token', '')}")
    for index, action in enumerate(item.get("actions") or [], 1):
        print(f"action[{index}]: {json.dumps(action, sort_keys=True)}")


def hooks_list(api_url, kind=None, debug=False):
    query = f"?kind={kind}" if kind else ""
    data = api.call(api_url, "GET", f"/api/agent/hook-triggers{query}", debug=debug)
    items = data.get("hooks", [])
    if not items:
        print("No hook triggers yet. Create one with `dapier hooks save`.")
    for item in items:
        enabled = "yes" if item.get("enabled", True) else "no"
        print(f"{item.get('hook_id', ''):20} {item.get('kind', ''):9} "
              f"enabled={enabled:3} {item.get('url', '')} {_entry_label(item)}")
    print_flows(data.get("flows") or [])
    return 0


def hooks_show(api_url, name, debug=False):
    data = api.call(api_url, "GET", "/api/agent/hook-triggers", debug=debug)
    item = next((h for h in data.get("hooks", []) if h.get("hook_id") == name), None)
    if item is None:
        print(f"No hook trigger named '{name}'.")
        return 4
    print_hook(item)
    return 0


def hooks_save(api_url, path, debug=False):
    body, error = _read_json_file(path)
    if error:
        print(error)
        return 2
    data = api.call(api_url, "PUT", "/api/agent/hook-triggers", body, debug=debug)
    verb = "Created" if data.get("created") else "Updated"
    print(f"{verb} {data.get('kind', 'webhook')} hook '{data.get('hook_id')}'. "
          "It is live immediately; no deploy needed.")
    if data.get("kind") == "telegram":
        print(f"  Telegram delivery URL: {data.get('url')}")
        if data.get("connection_id"):
            print(f"  Bot connection: {data.get('connection_id')}")
    else:
        print(f"  URL: {data.get('url')}")
        print(f"  Callers send: {data.get('header', 'authorization')}: Bearer {data.get('token', '')}")
        url, token = data.get("url", ""), data.get("token", "")
        print("  Try it: curl -X POST '" + url + "' "
              "-H 'authorization: Bearer " + token + "' "
              "-H 'content-type: application/json' -d '{\"hello\":\"world\"}'")
    return 0


def hooks_delete(api_url, name, kind=None, debug=False):
    query = f"name={name}" + (f"&kind={kind}" if kind else "")
    data = api.call(api_url, "DELETE", f"/api/agent/hook-triggers?{query}", debug=debug)
    print(f"Deleted {data.get('kind') or 'hook'} trigger '{data.get('hook_id') or name}'.")
    return 0


def schedules_list(api_url, debug=False):
    data = api.call(api_url, "GET", "/api/agent/schedule-triggers", debug=debug)
    items = data.get("schedules", [])
    if not items:
        print("No schedule triggers yet. Create one with `dapier schedules save`.")
    for item in items:
        state = "enabled" if item.get("enabled", True) else "disabled"
        print(f"{item.get('schedule_id', ''):20} {item.get('expression', ''):40} "
              f"{state:9} {_entry_label(item)}")
    print_flows(data.get("flows") or [])
    return 0


def schedules_save(api_url, path, debug=False):
    body, error = _read_json_file(path)
    if error:
        print(error)
        return 2
    data = api.call(api_url, "PUT", "/api/agent/schedule-triggers", body, debug=debug)
    verb = "Created" if data.get("created") else "Updated"
    state = "enabled" if data.get("enabled", True) else "disabled"
    print(f"{verb} schedule '{data.get('schedule_id')}' ({data.get('expression')}, {state}).")
    print(f"  EventBridge rule: {data.get('rule')}")
    print("  It fires on the worker immediately per the schedule; no deploy needed.")
    return 0


def schedules_delete(api_url, name, debug=False):
    api.call(api_url, "DELETE", f"/api/agent/schedule-triggers?name={name}", debug=debug)
    print(f"Deleted schedule trigger '{name}' and its EventBridge rule.")
    return 0


def polls_list(api_url, debug=False):
    data = api.call(api_url, "GET", "/api/agent/poll-triggers", debug=debug)
    items = data.get("polls", [])
    if not items:
        print("No poll triggers yet. Create one with `dapier polls save`.")
    for item in items:
        state = "enabled" if item.get("enabled", True) else "disabled"
        print(f"{item.get('poll_id', ''):20} {item.get('expression', ''):40} "
              f"{state:9} {item.get('url', '')}")
    print_flows(data.get("flows") or [])
    return 0


def polls_save(api_url, path, debug=False):
    body, error = _read_json_file(path)
    if error:
        print(error)
        return 2
    data = api.call(api_url, "PUT", "/api/agent/poll-triggers", body, debug=debug)
    verb = "Created" if data.get("created") else "Updated"
    state = "enabled" if data.get("enabled", True) else "disabled"
    print(f"{verb} poll trigger '{data.get('poll_id')}' ({data.get('expression')}, {state}).")
    print(f"  EventBridge rule: {data.get('rule')}")
    print(f"  Watches {data.get('method')} {data.get('url')} — one event per new item, no deploy needed.")
    return 0


def polls_delete(api_url, name, debug=False):
    api.call(api_url, "DELETE", f"/api/agent/poll-triggers?name={name}", debug=debug)
    print(f"Deleted poll trigger '{name}' and its EventBridge rule.")
    return 0


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
        events = ", ".join(entry.get("events") or []) or "(any event)"
        print(f"  {entry.get('name', ''):20} {entry.get('label', ''):20} {events}")
    return 0


CREDENTIAL_FIELDS = {"slack": "token", "mailchimp": "api_key"}


def credentials_set(api_url, provider, path, debug=False):
    """Store a provider credential; the value travels only in the request body."""
    field = CREDENTIAL_FIELDS.get(provider)
    if not field:
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
    data = api.call(api_url, "PUT", f"/api/agent/credentials/{provider}",
                    {field: value}, debug=debug)
    print(f"Stored the {data.get('provider', provider)} credential. "
          "It is live immediately; the value is never shown again.")
    return 0


def print_grants(items):
    print(f"{'CONNECTION':24} {'SUBJECT':34} {'AGENT':24} {'OPERATIONS':18} EXPIRES")
    for item in items:
        operations = ",".join(item.get("operations") or [])
        expires = item.get("expires_at") or "-"
        print(f"{item.get('connection_id', ''):24} {item.get('subject', ''):34} "
              f"{item.get('agent', ''):24} {operations:18} {expires}")


def grants_list(api_url, connection_id=None, debug=False):
    query = f"?{urlencode({'connection_id': connection_id})}" if connection_id else ""
    data = api.call(api_url, "GET", f"/api/agent/grants{query}", debug=debug)
    items = data.get("grants", [])
    if not items:
        print("No grants. Create one with `dapier grants save`.")
        return 0
    print_grants(items)
    return 0


def grants_save(api_url, path, debug=False):
    body, error = _read_json_file(path)
    if error:
        print(error)
        return 2
    data = api.call(api_url, "PUT", "/api/agent/grants", body, debug=debug)
    print(f"Granted {data.get('subject')}#{data.get('agent')} on {data.get('connection_id')} "
          f"({', '.join(data.get('operations') or [])}).")
    return 0


def grants_delete(api_url, connection_id, grantee, debug=False):
    query = urlencode({"connection_id": connection_id, "grantee": grantee})
    api.call(api_url, "DELETE", f"/api/agent/grants?{query}", debug=debug)
    print(f"Revoked {grantee} on {connection_id}.")
    return 0


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


def tokens_create(api_url, name, agent, debug=False):
    data = api.call(api_url, "PUT", "/api/agent/tokens",
                    {"token_id": name, "agent": agent}, debug=debug)
    print(f"Created API token {data.get('token_id')} "
          f"(subject {data.get('subject')}, agent {data.get('agent')}).")
    print(f"Grant it access with `dapier grants save` using subject {data.get('subject')}.")
    print("Store the value now; it is not retrievable again:")
    print(data.get("token", ""))
    return 0


def tokens_revoke(api_url, name, debug=False):
    query = urlencode({"token_id": name})
    api.call(api_url, "DELETE", f"/api/agent/tokens?{query}", debug=debug)
    print(f"Revoked API token {name}. Presented values stop authenticating immediately.")
    return 0


def connections_revoke(api_url, connection_id, debug=False):
    data = api.call(api_url, "DELETE",
                    f"/api/agent/connections/{connection_id}/tokens", debug=debug)
    print(f"Revoked tokens for {data.get('connection_id', connection_id)}; "
          f"status is now {data.get('status')}.")
    return 0


def print_overview(data):
    print(f"{data.get('service', 'dapier')} in {data.get('region', '-')}")
    workflows = data.get("workflows") or []
    enabled = sum(1 for item in workflows if item.get("enabled", True))
    print(f"\nWORKFLOWS ({enabled}/{len(workflows)} enabled)")
    for item in workflows:
        state = "" if item.get("enabled", True) else " (disabled)"
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


def overview(api_url, debug=False):
    data = api.call(api_url, "GET", "/api/agent/overview", debug=debug)
    print_overview(data)
    return 0


def print_runs(items):
    print(f"{'RUN':42} {'STATUS':12} {'STEPS':5} STARTED")
    for item in items:
        started = (item.get("started_at") or "-")[:19]
        print(f"{item.get('run_id', ''):42} {item.get('status', ''):12} "
              f"{item.get('steps', 0):<5} {started}")


def runs_list(api_url, limit=25, debug=False):
    data = api.call(api_url, "GET", f"/api/agent/runs?limit={int(limit)}", debug=debug)
    items = data.get("runs", [])
    if not items:
        print("No runs recorded yet. Runs appear once a workflow handles a trigger event.")
        return 0
    print_runs(items)
    return 0


def runs_show(api_url, run_id, debug=False):
    data = api.call(api_url, "GET", f"/api/agent/runs/{quote(run_id, safe='')}", debug=debug)
    run = data.get("run") or {}
    print(f"run: {run.get('run_id') or run_id}")
    for key in ("workflow_id", "status", "connector", "event_type", "steps",
                "started_at", "finished_at", "duration_ms", "failed_step", "error"):
        if run.get(key) not in (None, ""):
            print(f"{key}: {run[key]}")
    for index, step in enumerate(data.get("steps") or [], 1):
        label = step.get("action_id") or "?"
        if step.get("action_type"):
            label = f"{label} ({step['action_type']})"
        print(f"\nstep[{index}]: {label}  {step.get('status', '?')}"
              + (f"  {step['duration_ms']}ms" if step.get("duration_ms") is not None else ""))
        if step.get("error"):
            print(f"  error: {step['error']}")
        for field in ("input", "output"):
            value = step.get(field)
            if value not in (None, "", [], {}):
                print(f"  {field}: {json.dumps(value, sort_keys=True, default=str)}")
    return 0


def runs_replay(api_url, run_id, debug=False):
    data = api.call(api_url, "POST", f"/api/agent/runs/{quote(run_id, safe='')}/replay", body={}, debug=debug)
    print(f"Replay accepted for {data.get('replayed_from') or run_id}.")
    print(f"The re-injected run ({data.get('run_id') or 'pending'}) appears in `dapier runs list` "
          "once the worker picks it up.")
    return 0


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
