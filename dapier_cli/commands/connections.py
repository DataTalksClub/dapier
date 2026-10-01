"""Implementations of the `dapier connections` and `dapier token` commands."""

import json, os, subprocess, time, webbrowser
from datetime import datetime
from urllib.parse import urlencode

from .. import api

__all__ = ["TOKEN_ENV_VARS", "TOKEN_PROVIDERS", "YOUTUBE_HUB_URL", "connections_connect", "connections_create", "connections_delete", "connections_discover", "connections_edit", "connections_import", "connections_list", "connections_revoke", "connections_scopes", "connections_show", "connections_test", "print_connection", "print_connections", "print_discovery_items", "print_discovery_resources", "print_hook_setup", "token_exec", "token_write"]


# Documented child-process variables. The provider-specific alias exists so
# existing upload tooling works; DAPIER_ACCESS_TOKEN is always set too.
TOKEN_ENV_VARS = {
    "youtube": ("DAPIER_ACCESS_TOKEN", "YOUTUBE_ACCESS_TOKEN"),
    "dropbox": ("DAPIER_ACCESS_TOKEN", "DROPBOX_ACCESS_TOKEN"),
}


def _local_expiry(item):
    """The token expiry as a local timestamp, '-' when unknown."""
    if not item.get("token_expires_at"):
        return "-"
    try:
        stamp = datetime.fromisoformat(str(item["token_expires_at"])).astimezone()
        return stamp.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return "-"


def print_connections(items):
    print(f"{'CONNECTION':24} {'PROVIDER':10} {'STATUS':10} {'HEALTH':8} {'EXPIRES':17} {'USED IN':24} ACCOUNT")
    for item in items:
        account = item.get("account_title") or item.get("verified_account_id") or "-"
        scopes = ",".join((item.get("granted_scopes") or item.get("scopes") or [])[:2])
        extra = f" [{scopes}]" if scopes else ""
        print(f"{item.get('connection_id', ''):24} {item.get('provider', ''):10} "
              f"{item.get('status', ''):10} {item.get('health') or '-':8} {_local_expiry(item):17} "
              f"{_used_in_label(item):24} {account}{extra}")


def _used_in_label(item):
    """The USED IN column: up to two referencing workflows/hooks, then +N."""
    refs = sorted({str(entry.get("ref")) for entry in item.get("used_in") or []})
    if not refs:
        return "-"
    shown = ", ".join(refs[:2])
    if len(refs) > 2:
        shown += f" +{len(refs) - 2}"
    return shown


def print_connection(item):
    for key in ("connection_id", "provider", "display_name", "status", "health",
                "verified_account_id", "account_title", "expected_account_id",
                "granted_scopes", "scopes", "version", "updated_at", "connected_at"):
        if item.get(key) not in (None, "", []):
            value = item[key]
            if isinstance(value, list):
                value = " ".join(value)
            print(f"{key}: {value}")
    if item.get("token_expires_at"):
        print(f"token_expires_at: {_local_expiry(item)}")


def connections_list(api_url, debug=False, limit=None, next_token=None, list_all=False):
    """`dapier connections list`: the caller's grant-filtered connections, or
    every connection with --all (the API's operator mode). limit/--next page
    either flavor through the API's paging token."""
    params = {}
    if limit:
        params["limit"] = int(limit)
    if next_token:
        params["next"] = next_token
    if list_all:
        params["all"] = "true"
    query = f"?{urlencode(params)}" if params else ""
    data = api.call(api_url, "GET", f"/api/agent/connections{query}", debug=debug)
    items = data.get("connections", [])
    if not items:
        print("No connections yet." if list_all
              else "No connection grants. Ask an operator for access.")
        return 0
    print_connections(items)
    next_page = (data.get("paging") or {}).get("next")
    if next_page:
        print(f"\nnext page: {next_page}  (pass it to --next)")
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
            print_hook_setup(item.get("provider", ""), api_url)
            return 0
    print("Timed out waiting for consent. Re-run `dapier connections connect` to retry.")
    return 5


# Provider-side webhook setup steps, printed after a connection succeeds: the
# CLI twin of the console Manage-dialog setup blocks (zoom and slack print
# inline from connections_import below). The youtube and dropbox hooks are
# global endpoints, so unlike zoom/slack no per-connection id appears in them.
YOUTUBE_HUB_URL = "https://pubsubhubbub.appspot.com/subscribe"


def print_hook_setup(provider, api_url):
    base = api_url.rstrip("/")
    if provider == "youtube":
        print(f"To receive video pushes, subscribe the channel on the WebSub hub "
              f"({YOUTUBE_HUB_URL}) with callback {base}/hooks/youtube and topic "
              f"https://www.youtube.com/xml/feeds/videos.xml?channel_id=<CHANNEL_ID>; "
              f"the renewal schedule also re-subscribes every channel a youtube "
              f"workflow item filters on every five days.")
    elif provider == "dropbox":
        print(f"To receive file events, set the Dropbox app's Webhook URI to "
              f"{base}/hooks/dropbox in the App Console; deliveries are signed "
              f"with the app secret, so the dropbox OAuth client in Dapier must "
              f"hold the current App Console secret.")


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
                       token_path=None, root_path=None, display_name=None,
                       signing_secret_path=None):
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
        if signing_secret_path:
            if provider != "slack":
                print("--signing-secret-file applies only to Slack connections.")
                return 2
            try:
                with open(signing_secret_path, encoding="utf-8") as handle:
                    signing_secret = handle.read().strip()
            except OSError as exc:
                print(f"Cannot read {signing_secret_path}: {exc}")
                return 2
            if not signing_secret:
                print("The signing-secret file is empty.")
                return 2
            body["signing_secret"] = signing_secret
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
        if provider == "slack":
            print(f"To listen for messages, set the Slack app's Event Subscription Request URL to "
                  f"{api_url.rstrip('/')}/hooks/slack/{data.get('connection_id')} "
                  f"and subscribe to the message and app_mention event types.")
        elif provider in ("youtube", "dropbox"):
            print_hook_setup(provider, api_url)
    return 0
def connections_revoke(api_url, connection_id, debug=False):
    data = api.call(api_url, "DELETE",
                    f"/api/agent/connections/{connection_id}/tokens", debug=debug)
    print(f"Revoked tokens for {data.get('connection_id', connection_id)}; "
          f"status is now {data.get('status')}.")
    return 0


def connections_delete(api_url, connection_id, force=False, debug=False):
    """`dapier connections delete`: remove a connection outright. The API
    refuses (409) while workflows or hook triggers still reference it; the
    error names them, and --force accepts breaking those references."""
    path = f"/api/agent/connections/{connection_id}"
    if force:
        path += "?force=true"
    try:
        data = api.call(api_url, "DELETE", path, debug=debug)
    except api.ApiError as exc:
        if exc.status == 409:
            print(f"Not deleted: {exc}")
            print("Pass --force to delete it anyway and leave those references dangling.")
            return 1
        raise
    grants = data.get("grants_removed") or 0
    print(f"Deleted {data.get('connection_id', connection_id)}"
          + (f" ({grants} grant{'s' if grants != 1 else ''} removed)" if grants else "")
          + ". Its stored credential went with it.")
    return 0


def _param_label(param):
    """One params-table entry: `name*` when required, `name` when optional."""
    if isinstance(param, dict):
        name = str(param.get("name", "?"))
        required = bool(param.get("required"))
    else:
        name, required = str(param), False
    return f"{name}*" if required else name


def print_discovery_resources(resources):
    print(f"{'RESOURCE':20} {'LABEL':26} {'PARAMS':28} DESCRIPTION")
    for item in resources:
        params = ", ".join(_param_label(param) for param in item.get("params") or []) or "-"
        print(f"{item.get('name', ''):20} {item.get('label', ''):26} "
              f"{params:28} {item.get('description', '')}")
    print("* marks a required param; pass it with --param KEY=VALUE.")


def print_discovery_items(items):
    print(f"{'ID':40} {'NAME':28} DETAILS")
    for item in items:
        extra = " ".join(f"{key}={item[key]}" for key in sorted(item)
                         if key not in ("id", "name"))
        print(f"{str(item.get('id', '')):40} {str(item.get('name', '')):28} {extra}")


def _parse_params(pairs):
    """Flatten repeated --param KEY=VALUE values; None on a malformed pair."""
    params = {}
    for pair in pairs or []:
        key, sep, value = str(pair).partition("=")
        if not sep or not key:
            return None
        params[key] = value
    return params


def connections_discover(api_url, connection_id, resource=None, params=(), debug=False):
    """List a connection's discovery resources, or one resource's items."""
    if not resource:
        data = api.call(api_url, "GET",
                        f"/api/agent/connections/{connection_id}/discover", debug=debug)
        resources = data.get("resources") or []
        if not resources:
            print(f"Connection {connection_id} exposes no discovery resources.")
            return 0
        print_discovery_resources(resources)
        return 0
    query_params = _parse_params(params)
    if query_params is None:
        print("Params must be KEY=VALUE pairs (e.g. --param spreadsheet_id=abc).")
        return 2
    path = f"/api/agent/connections/{connection_id}/discover/{resource}"
    if query_params:
        path += f"?{urlencode(query_params)}"
    data = api.call(api_url, "GET", path, debug=debug)
    items = data.get("items") or []
    if not items:
        print(f"No {resource} found for {connection_id}.")
        return 0
    print_discovery_items(items)
    return 0


def connections_test(api_url, connection_id, debug=False):
    """Exercise the connection's stored tokens against its provider."""
    data = api.call(api_url, "POST", f"/api/agent/connections/{connection_id}/test",
                    body={}, debug=debug)
    print(f"{'OK' if data.get('ok') else 'FAILED'} — {data.get('detail') or ''}")
    identity = data.get("identity") or {}
    if identity:
        summary = ", ".join(f"{key}={value}" for key, value in sorted(identity.items()))
        print(f"Identity: {summary}")
    return 0 if data.get("ok") else 1

