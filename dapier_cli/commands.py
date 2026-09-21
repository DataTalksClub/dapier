"""Implementations of the `dapier connections|token` commands."""

import json
import os
import subprocess
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
    try:
        with open(client_secret_file, encoding="utf-8") as handle:
            client_secret = handle.read().strip()
    except OSError as exc:
        print(f"Cannot read {client_secret_file}: {exc}")
        return 2
    if not isinstance(authorized_user, dict) or not authorized_user.get("refresh_token"):
        print("The authorized-user file has no refresh token.")
        return 2
    if not client_secret:
        print("The client-secret file is empty.")
        return 2
    body = {
        "connection_id": connection_id,
        "provider": provider,
        "client_id": client_id,
        "client_secret": client_secret,
        "authorized_user": {"refresh_token": authorized_user["refresh_token"]},
    }
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
