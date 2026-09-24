"""Implementations of the `dapier connections|token` commands."""

import json
import os
import subprocess
import sys
import time
import webbrowser

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


def connections_import(api_url, connection_id, provider, client_id, client_secret_file,
                       authorized_user_path, expected_account_id=None, scopes=(), debug=False):
    """Operator import over a Bearer identity (operator allowlist enforced server-side)."""
    try:
        authorized_user = json.loads(open(authorized_user_path, encoding="utf-8").read())
    except (OSError, ValueError) as exc:
        print(f"Cannot read {authorized_user_path}: {exc}")
        return 2
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
    if not isinstance(authorized_user, dict) or not authorized_user.get("refresh_token"):
        print("The authorized-user file has no refresh token.")
        return 2
    body = {
        "connection_id": connection_id,
        "provider": provider,
        "authorized_user": {"refresh_token": authorized_user["refresh_token"]},
    }
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
    # Secrets travel only in the TLS request body to the operator endpoint;
    # they never appear in argv, logs, or output. The local files are only read.
    data = api.call(api_url, "POST", "/api/admin/connections/import", body, debug=debug)
    print(f"Imported {data.get('connection_id')} "
          f"({data.get('account_title') or data.get('verified_account_id')}).")
    return 0


def print_trigger(item):
    for key in ("name", "address", "description", "enabled", "created_by",
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
        types = ",".join(action.get("type", "?") for action in item.get("actions") or [])
        enabled = "yes" if item.get("enabled", True) else "no"
        print(f"{item.get('name', ''):20} {item.get('address', ''):34} "
              f"enabled={enabled:3} {types}")
    routes = data.get("yaml_routes") or []
    if routes:
        print(f"Routes handled by YAML workflows (not editable here): {', '.join(routes)}")
    return 0


def triggers_show(api_url, name, debug=False):
    data = api.call(api_url, "GET", "/api/agent/email-triggers", debug=debug)
    item = next((t for t in data.get("triggers", []) if t.get("name") == name), None)
    if item is None:
        print(f"No trigger named '{name}'.")
        return 4
    print_trigger(item)
    return 0


def _read_trigger_file(path):
    """Return the parsed JSON body, or ``(None, error_message)``."""
    try:
        with (sys.stdin if path == "-" else open(path, encoding="utf-8")) as handle:
            return json.loads(handle.read()), None
    except OSError as exc:
        return None, f"Cannot read {path}: {exc}"
    except ValueError as exc:
        return None, f"{path} is not valid JSON: {exc}"


def triggers_save(api_url, path, debug=False):
    body, error = _read_trigger_file(path)
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
        trigger = f"{item.get('connector', '?')}.{item.get('event', '?')}"
        print(f"{item.get('source', ''):36} {trigger:34} "
              f"{item.get('actionCount', 0)} action(s) enabled={enabled}")
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
    body = {"yaml": yaml_text}
    if rename_from:
        body["renameFrom"] = rename_from
    data = api.call(api_url, "PUT", "/api/agent/designer/workflows", body, debug=debug)
    print(f"Committed {data.get('file')} ({str(data.get('commit', ''))[:7]}). "
          "The deploy pipeline publishes it in a few minutes.")
    return 0


def workflows_list(api_url, debug=False):
    data = api.call(api_url, "GET", "/api/agent/designer/workflows", debug=debug)
    items = data.get("workflows", [])
    if not items:
        print("No workflows deployed yet. Draw one at /designer or run `dapier workflows save`.")
    for item in items:
        enabled = "yes" if item.get("enabled", True) else "no"
        trigger = f"{item.get('connector', '?')}.{item.get('event', '?')}"
        print(f"{item.get('source', ''):36} {trigger:34} "
              f"{item.get('actionCount', 0)} action(s) enabled={enabled}")
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
    body = {"yaml": yaml_text}
    if rename_from:
        body["renameFrom"] = rename_from
    data = api.call(api_url, "PUT", "/api/agent/designer/workflows", body, debug=debug)
    print(f"Committed {data.get('file')} ({str(data.get('commit', ''))[:7]}). "
          "The deploy pipeline publishes it in a few minutes.")
    return 0
