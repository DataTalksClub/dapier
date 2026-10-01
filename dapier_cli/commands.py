"""Implementations of the `dapier connections|token` commands."""

import base64
import json
import os
import subprocess
import sys
import time
import webbrowser
from urllib.parse import quote, urlencode
from datetime import datetime

from . import api, config

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
    domain = data.get("domain", "")
    # Workflow-claimed routes sit in the same list: every address the domain
    # answers is one row, and each row says what runs when mail arrives.
    entries = [
        (item.get("name", ""), item.get("address", ""),
         "yes" if item.get("enabled", True) else "no", _entry_label(item))
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


def workflows_list(api_url, debug=False, search=None, tag=None, folder=None):
    params = []
    if search:
        params.append(f"q={quote(search, safe='')}")
    if tag:
        params.append(f"tag={quote(tag, safe='')}")
    if folder:
        params.append(f"folder={quote(folder, safe='')}")
    query = f"?{'&'.join(params)}" if params else ""
    data = api.call(api_url, "GET", f"/api/agent/designer/workflows{query}", debug=debug)
    items = data.get("workflows", [])
    if not items:
        print("No workflows match this search." if search or tag or folder else
              "No workflows yet. Create one in the console or run `dapier workflows save`.")
    for item in items:
        state = "On" if item.get("enabled", True) else "Off"
        if not item.get("published", True):
            # Draft-only: saved in the designer but never published — it
            # fires nothing until `workflows publish` promotes it.
            state = "draft (never published)"
        elif item.get("has_draft"):
            state += " [draft]"
        if item.get("auto_paused"):
            # The engine paused it after consecutive failed runs; `workflows on`
            # is the resume verb (the enable toggle clears the pause).
            state += " (auto-paused)"
        trigger = f"{item.get('connector', '?')}.{item.get('event', '?')}"
        extra = (item.get("triggerCount") or 1) - 1
        if extra > 0:
            trigger = f"{trigger} +{extra}"
        tags = item.get("tags") or []
        item_folder = item.get("folder") or ""
        print(f"{item.get('source', ''):36} {trigger:34} "
              f"{item.get('actionCount', 0)} action(s) {state}"
              + (f" [folder: {item_folder}]" if item_folder else "")
              + (f" [{' '.join(tags)}]" if tags else ""))
    sync = data.get("git_sync") or {}
    target = f"{sync.get('repo', '?')} ({sync.get('branch', '?')} branch)"
    if sync.get("configured"):
        print(f"Workflow saves publish live and sync to {target}.")
    else:
        print("Workflow saves publish live; Git sync is not configured.")
    return 0


def workflows_show(api_url, file, debug=False):
    data = api.call(api_url, "GET", f"/api/agent/designer/workflows/{file}", debug=debug)
    print(json.dumps(data.get("workflow", {}), indent=2))
    return 0


def workflows_export(api_url, file, output=None, debug=False):
    """The workflow's canonical YAML (api_get's `yaml` field) to stdout or -o.

    The round-trip twin of `workflows save`: exporting a saved workflow and
    saving the export back reproduces the stored definition byte for byte.
    """
    data = api.call(api_url, "GET", f"/api/agent/designer/workflows/{file}", debug=debug)
    yaml_text = data.get("yaml")
    if not yaml_text:
        print(f"The API returned no YAML for {file}.")
        return 5
    if output:
        try:
            with open(output, "w", encoding="utf-8") as handle:
                handle.write(yaml_text)
        except OSError as exc:
            print(f"Cannot write {output}: {exc}")
            return 2
        print(f"Exported {file} to {output}.")
    else:
        print(yaml_text)
    return 0


def workflows_export_all(api_url, output=None, debug=False):
    """`workflows export --all`: the server-built zip of every workflow.

    Thin client over the operator-gated bundle endpoint — the same domain
    function the console's Export-all button drives. The zip (one canonical
    YAML per workflow under its source file name, plus a manifest.json
    listing file, enabled state, tags, folder, and latest version) is built
    server-side, arrives base64 in the JSON body, and is only decoded and
    written here; nothing is fetched per workflow or assembled client-side.
    """
    data = api.call(api_url, "GET", "/api/agent/designer/export", debug=debug)
    count = int(data.get("count") or 0)
    if not count:
        print("No workflows to export. Create one in the console or run `dapier workflows save`.")
        return 2
    try:
        raw = base64.b64decode(data.get("b64") or "")
    except (ValueError, TypeError):
        print("The API returned an unreadable bundle.")
        return 5
    # The server suggests the filename; basename keeps a hostile suggestion
    # from writing outside the caller's directory.
    target = output or os.path.basename(data.get("filename") or "") or "dapier-workflows.zip"
    try:
        with open(target, "wb") as handle:
            handle.write(raw)
    except OSError as exc:
        print(f"Cannot write {target}: {exc}")
        return 2
    skipped = data.get("skipped") or []
    note = f" (skipped: {', '.join(skipped)})" if skipped else ""
    print(f"Exported {count} workflow(s) to {target}{note}.")
    print("Each workflow is workflows/<file>.yaml; manifest.json describes the bundle.")
    return 0


def workflows_export_all_bundle(api_url, out=None, debug=False):
    """`workflows export-all`: the server-built zip of every workflow's YAML.

    Thin client over the operator-gated export-all endpoint — the same domain
    function the console's Export-all button drives. The zip arrives base64
    in the JSON body and is decoded here; unlike `workflows export --all`
    nothing is fetched per workflow or assembled client-side.
    """
    data = api.call(api_url, "GET", "/api/agent/designer/workflows/export-all", debug=debug)
    try:
        raw = base64.b64decode(data.get("b64") or "")
    except (ValueError, TypeError):
        print("The API returned an unreadable bundle.")
        return 5
    # The server suggests the filename; basename keeps a hostile suggestion
    # from writing outside the caller's directory.
    target = out or os.path.basename(data.get("filename") or "") or "dapier-workflows.zip"
    try:
        with open(target, "wb") as handle:
            handle.write(raw)
    except OSError as exc:
        print(f"Cannot write {target}: {exc}")
        return 2
    skipped = data.get("skipped") or []
    note = f" (skipped: {', '.join(skipped)})" if skipped else ""
    print(f"Exported {data.get('count', 0)} workflow(s) to {target}{note}")
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
    """The `workflows save` wire call; also the --save tail of `workflows draft`.

    A save writes a draft (nothing goes live); the API answers published:
    false with a draft block. Only a historical live-publish response
    (published: true) prints the live message."""
    body = {"yaml": yaml_text}
    if rename_from:
        body["renameFrom"] = rename_from
    data = api.call(api_url, "PUT", "/api/agent/designer/workflows", body, debug=debug)
    if data.get("published"):
        print(f"Published {data.get('file')} live."
              + (f" Synced commit {str(data['commit'])[:7]}." if data.get("commit") else ""))
    else:
        draft = data.get("draft") or {}
        stale = " behind the live revision" if draft.get("stale") else ""
        print(f"Saved {data.get('file')} as a draft{stale} — "
              f"publish it with `dapier workflows publish {data.get('file')}`.")
    if data.get("git_sync_error"):
        print(f"Warning: Git sync failed ({data['git_sync_error']}).")
    for warning in data.get("warnings") or []:
        print(f"Warning: {warning}")
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


def workflows_set_enabled(api_url, files, enabled, debug=False):
    """Turn one or more workflows on/off through the bulk endpoint (one call,
    per-id results; the API applies the usual toggle semantics to each)."""
    if isinstance(files, str):
        files = [files]
    data = api.call(api_url, "POST", "/api/agent/designer/workflows/bulk",
                    {"ids": list(files), "action": "enable" if enabled else "disable"},
                    debug=debug)
    state = "On" if enabled else "Off"
    for result in data.get("results") or []:
        label = result.get("file") or result.get("id") or "?"
        if result.get("ok"):
            print(f"{label} is {state} — live now.")
            if result.get("commit"):
                print(f"Committed {str(result['commit'])[:7]}.")
            for warning in result.get("warnings") or []:
                print(f"Warning: {warning}")
        else:
            print(f"{label}: {result.get('error') or 'failed'}")
    return 0


def workflows_bulk_enabled(api_url, enabled, tag=None, all_workflows=False, debug=False):
    """Bulk enable/disable by scope (`workflows enable|disable --tag x|--all`):
    every workflow carrying a tag, or all of them, through the same bulk
    endpoint the explicit file list uses. Failures print per workflow —
    nothing fails silently."""
    if not tag and not all_workflows:
        print("Nothing to do: pass --tag <tag> or --all (or name workflow files).")
        return 2
    body = {"action": "enable" if enabled else "disable"}
    if tag:
        body["tag"] = tag
    else:
        body["all"] = True
    data = api.call(api_url, "POST", "/api/agent/designer/workflows/bulk", body, debug=debug)
    state = "On" if enabled else "Off"
    print(f"{data.get('ok', 0)} of {data.get('requested', 0)} workflow(s) set {state} "
          f"[{data.get('scope', '?')}].")
    for result in data.get("results") or []:
        if not result.get("ok"):
            print(f"{result.get('id') or '?'}: {result.get('error') or 'failed'}")
    return 0


def workflows_duplicate(api_url, file, name=None, debug=False):
    """Copy a saved workflow under a new id (the server slugifies `--name`,
    default `<id>-copy`) through the same publish path as a save."""
    body = {"name": name} if name else {}
    data = api.call(api_url, "POST", f"/api/agent/designer/workflows/{file}/duplicate",
                    body, debug=debug)
    new_file = data.get("file") or file
    if data.get("published"):
        print(f"Duplicated {file} as {new_file} and published it live.")
    else:
        print(f"Duplicated {file} as {new_file}.")
    return 0


def workflows_versions(api_url, file, debug=False):
    """Version history for one workflow: who published what, when, and why."""
    data = api.call(api_url, "GET", f"/api/agent/designer/workflows/{file}/versions", debug=debug)
    print(f"{data.get('workflow') or file} is at revision {data.get('revision', '?')}.")
    draft = data.get("draft")
    if draft:
        stale = " (stale — publish would refuse it; save a fresh draft)" if draft.get("stale") else ""
        by = f" by {draft['drafted_by']}" if draft.get("drafted_by") else ""
        print(f"draft: based on v{draft.get('base_revision', 0)}{by}{stale}")
    versions = data.get("versions") or []
    if not versions:
        print("No version history yet — versions are recorded from now on.")
        return 0
    for version in versions:
        marker = " (current)" if version.get("current") else ""
        state = "On" if version.get("enabled", True) else "Off"
        print(f"v{version.get('revision')}  {version.get('published_at', '')}  "
              f"{version.get('cause') or 'save':8} {version.get('published_by') or 'unknown'} "
              f"{state}{marker}")
    return 0


def workflows_rollback(api_url, file, revision, debug=False):
    """Restore an old version through the managed workflow save path."""
    body = {} if revision is None else {"revision": int(revision)}
    label = f"v{revision}" if revision is not None else "the previous version"
    data = api.call(api_url, "POST", f"/api/agent/designer/workflows/{file}/rollback",
                    body, debug=debug)
    if data.get("published"):
        print(f"Rolled {file} back to {label} and published it live.")
    else:
        print(f"Rolled {file} back to {label}.")
    return 0


def workflows_publish(api_url, file, debug=False):
    """Promote a workflow's saved draft live (the agent POST .../publish route):
    the promotion runs through the ordinary publish path, so the git commit,
    version record, and YouTube reconcile behave like any publish; a
    draft-only workflow becomes v1. A stale draft (the live definition moved
    past its base revision) is refused 409."""
    data = api.call(api_url, "POST", f"/api/agent/designer/workflows/{file}/publish",
                    {}, debug=debug)
    revision = data.get("revision")
    suffix = f" as v{revision}" if revision else ""
    print(f"Published {data.get('file') or file}{suffix} — live now."
          + (f" Synced commit {str(data['commit'])[:7]}." if data.get("commit") else ""))
    for warning in data.get("warnings") or []:
        print(f"Warning: {warning}")
    if data.get("git_sync_error"):
        print(f"Warning: Git sync failed ({data['git_sync_error']}).")
    return 0


def workflows_discard(api_url, file, assume_yes=False, debug=False):
    """Throw a workflow's saved draft away (the agent DELETE .../draft route);
    the live definition is untouched."""
    if not assume_yes:
        try:
            answer = input(f"Discard the draft of {file}? The live workflow is "
                           "untouched; the drafted edits are lost [y/N]: ")
        except EOFError:
            # No interactive stdin (scripts, CI): never guess on a destroy.
            print("No terminal to confirm on; pass --yes to discard without a prompt.")
            return 2
        if answer.strip().lower() not in ("y", "yes"):
            print("Cancelled.")
            return 1
    data = api.call(api_url, "DELETE", f"/api/agent/designer/workflows/{file}/draft",
                    debug=debug)
    print(f"Discarded the draft of {data.get('file') or file}; live is untouched.")
    return 0


def workflows_draft_diff(api_url, file, debug=False):
    """Unified diff of a workflow's draft against live (the server builds it;
    the raw text goes to stdout so it pipes into `less` or `patch`)."""
    data = api.call(api_url, "GET",
                    f"/api/agent/designer/workflows/{file}/draft/diff", debug=debug)
    if data.get("same"):
        print("The draft is identical to live.")
        return 0
    diff = data.get("diff") or ""
    if diff:
        print(diff, end="" if diff.endswith("\n") else "\n")
    return 0


def workflows_delete(api_url, file, assume_yes=False, debug=False):
    """Delete a workflow on every surface at once (the agent DELETE route):
    the live published item is removed and one atomic git commit takes the
    YAML out of the repo; version history and past runs survive as history.
    Refused while runs of the workflow are parked on a delay, so a resume
    cannot dangle."""
    if not assume_yes:
        try:
            answer = input(f"Delete {file}? It stops immediately and the YAML is "
                           "deleted from the repo; past runs and version history "
                           "remain [y/N]: ")
        except EOFError:
            # No interactive stdin (scripts, CI): never guess on a destroy.
            print("No terminal to confirm on; pass --yes to delete without a prompt.")
            return 2
        if answer.strip().lower() not in ("y", "yes"):
            print("Cancelled.")
            return 1
    data = api.call(api_url, "DELETE", f"/api/agent/designer/workflows/{file}", debug=debug)
    print(f"Deleted {data.get('file') or file}. It is off now and no deploy will bring it back.")
    if data.get("commit"):
        print(f"Committed the removal ({str(data['commit'])[:7]}).")
    for warning in data.get("warnings") or []:
        print(f"Warning: {warning}")
    if data.get("git_sync_error"):
        print(f"Warning: the git commit failed ({data['git_sync_error']}); "
              "the next deploy may restore the file — retry `workflows delete`.")
    return 0


def _split_tags(spec, what):
    """Comma-separated tags as a clean list, or None when the flag is absent."""
    if spec is None:
        return None
    tags = [part.strip() for part in spec.split(",") if part.strip()]
    return tags or None


def workflows_tags(api_url, file, tags_spec=None, clear=False, add=None,
                   remove=None, debug=False):
    """Edit a workflow's tags (the agent PUT .../tags route): Zapier-style
    organization labels the list filters on. `--tags` replaces the whole set,
    `--add`/`--remove` edit it (removals win, case-insensitive), `--clear`
    empties it. The set lives in the workflow YAML, so it travels with saves,
    deploys, and rollbacks."""
    if clear:
        body = {"tags": []}
    elif tags_spec is not None:
        tags = _split_tags(tags_spec, "--tags")
        if not tags:
            print("Nothing to do: pass --tags 'billing,ops', --add/--remove, or --clear.")
            return 2
        body = {"tags": tags}
    elif add is not None or remove is not None:
        body = {}
        adds = _split_tags(add, "--add")
        removes = _split_tags(remove, "--remove")
        if adds:
            body["add"] = adds
        if removes:
            body["remove"] = removes
        if not body:
            print("Nothing to do: pass --tags 'billing,ops', --add/--remove, or --clear.")
            return 2
    else:
        print("Nothing to do: pass --tags 'billing,ops', --add/--remove, or --clear.")
        return 2
    data = api.call(api_url, "PUT", f"/api/agent/designer/workflows/{file}/tags",
                    body, debug=debug)
    shown = ", ".join(data.get("tags") or []) or "(none)"
    print(f"Tags for {data.get('file') or file}: {shown}"
          + (" — published live." if data.get("published") else "."))
    if data.get("git_sync_error"):
        print(f"Warning: the git commit failed ({data['git_sync_error']}); "
              "the tags are live but the next deploy may not carry them.")
    return 0


def workflows_folder(api_url, file, set_value=None, clear=False, debug=False):
    """Put a workflow in a Zapier-style folder (`--set "Name"`) or take it
    out (`--clear`) through the agent PUT .../folder route. Folders are flat —
    at most one per workflow, and a name, never a path — and the folder lives
    in the workflow YAML, so it travels with saves, deploys, and rollbacks."""
    if clear and set_value is not None:
        print("Pass --set <name> or --clear, not both.")
        return 2
    if not clear and set_value is None:
        print("Nothing to do: pass --set 'Name' or --clear.")
        return 2
    body = {"folder": "" if clear else set_value}
    data = api.call(api_url, "PUT", f"/api/agent/designer/workflows/{file}/folder",
                    body, debug=debug)
    shown = data.get("folder") or "(none)"
    print(f"Folder for {data.get('file') or file}: {shown}"
          + (" — published live." if data.get("published") else "."))
    if data.get("git_sync_error"):
        print(f"Warning: the git commit failed ({data['git_sync_error']}); "
              "the folder is live but the next deploy may not carry it.")
    return 0


def _read_workflow_yaml(path):
    """The workflow YAML from a path or stdin; (text, None) or (None, message)."""
    try:
        with (sys.stdin if path == "-" else open(path, encoding="utf-8")) as handle:
            return handle.read(), None
    except OSError as exc:
        return None, f"Cannot read {path}: {exc}"


def _load_json_arg(spec, what):
    """A JSON argument, inline or @file; (value, None) or (None, message)."""
    try:
        if spec.startswith("@"):
            with open(spec[1:], encoding="utf-8") as handle:
                return json.load(handle), None
        return json.loads(spec), None
    except OSError as exc:
        return None, f"Cannot read {spec[1:]}: {exc}"
    except ValueError as exc:
        return None, f"Invalid {what} JSON: {exc}"


def _print_test_steps(data):
    """The steps list of a test-run (or per-step test) report."""
    for index, step in enumerate(data.get("steps") or [], 1):
        head = f"  {index}. {step.get('action_id', '?')} ({step.get('action_type', '?')})"
        if step.get("ok"):
            print(f"{head}: ok")
        else:
            print(f"{head}: {step.get('error') or 'failed'}")
        for warning in step.get("warnings") or []:
            print(f"     warning: {warning}")
        if step.get("rendered_input") is not None:
            print(f"     input: {json.dumps(step['rendered_input'], sort_keys=True)}")
        if step.get("output") is not None:
            print(f"     output: {json.dumps(step['output'], sort_keys=True)}")


def workflows_test(api_url, path, event_spec, execute=False, strict=False, debug=False):
    """Test-run a workflow YAML against a sample event, via the agent API.

    Default is a dry-run: per-step rendered inputs with no side effects.
    --execute really runs the actions; --strict makes dry-run warnings
    (a required field the sample renders empty, a value implausible for its
    declared type) fail the step. Exits 0 when every step is ok and a
    trigger actually matched; 1 when the run found problems (a failed step,
    an unsupported action, or no trigger match), so it can gate scripts.
    """
    yaml_text, error = _read_workflow_yaml(path)
    if error:
        print(error)
        return 2
    sample, error = _load_json_arg(event_spec, "sample event")
    if error:
        print(error)
        return 2
    if not isinstance(sample, dict):
        print("The sample event must be a JSON object.")
        return 2
    data = api.call(api_url, "POST", "/api/agent/designer/workflows/test",
                    {"yaml": yaml_text, "event": sample, "execute": execute,
                     "strict": strict}, debug=debug)
    label = data.get("file") or path
    matched = bool(data.get("matched"))
    enabled = bool(data.get("enabled", True))
    print(f"{'Executed' if data.get('mode') == 'execute' else 'Dry run'}: {label} — "
          f"{'a trigger matches' if matched else 'NO trigger matches'} the sample event"
          + ("" if enabled else " (the workflow is disabled)"))
    _print_test_steps(data)
    if data.get("error"):
        print(f"Run error: {data['error']}")
    if not data.get("ok") or not matched:
        if data.get("ok") and not matched:
            print("Nothing would run: no trigger matches this event.")
        return 1
    return 0


def workflows_test_step(api_url, path, action_id, event_spec, steps_spec=None,
                        execute=False, debug=False):
    """Test one step of a workflow against a sample event, via the agent API.

    Zapier's per-step "Test step": the step's inputs render against the
    sample (plus prior steps' outputs from --steps, shaped like the run
    history, so {steps.<id>.output.*} templates fill in), and --execute
    really runs this one step through the real dispatch. Logic steps are
    evaluated, never executed: the branch a run would take, the wait a delay
    would take, what a loop would iterate. Exits 0 when the step passed,
    1 when it failed, 2 on input errors.
    """
    yaml_text, error = _read_workflow_yaml(path)
    if error:
        print(error)
        return 2
    sample, error = _load_json_arg(event_spec, "sample event")
    if error:
        print(error)
        return 2
    if not isinstance(sample, dict):
        print("The sample event must be a JSON object.")
        return 2
    steps_context = None
    if steps_spec:
        steps_context, error = _load_json_arg(steps_spec, "steps")
        if error:
            print(error)
            return 2
        if not isinstance(steps_context, dict):
            print("The steps context must be a JSON object shaped like run history "
                  "({action_id: {status, output}}).")
            return 2
    body = {"yaml": yaml_text, "action_id": action_id, "event": sample,
            "execute": execute}
    if steps_context:
        body["steps"] = steps_context
    data = api.call(api_url, "POST", "/api/agent/designer/workflows/test-step",
                    body, debug=debug)
    label = data.get("file") or path
    print(f"{'Executed' if execute else 'Tested'} step "
          f"{data.get('action_id') or action_id} of {label} — "
          f"{'ok' if data.get('ok') else 'FAILED'}")
    _print_test_steps(data)
    if data.get("error"):
        print(f"Run error: {data['error']}")
    return 0 if data.get("ok") else 1


def print_hook(item):
    for key in ("hook_id", "kind", "url", "connection_id", "list_id", "description",
                "dedupe_path", "flow", "enabled", "created_by", "created_at",
                "updated_at"):
        if item.get(key) not in (None, ""):
            print(f"{key}: {item[key]}")
    if item.get("events"):
        print(f"events: {', '.join(item['events'])}")
    if item.get("response"):
        print(f"response: {json.dumps(item['response'], sort_keys=True)}")
    if item.get("kind") == "telegram":
        print("secret header: x-telegram-bot-api-secret-token (managed by Telegram)")
    elif item.get("kind") == "mailchimp":
        print("auth: none — Mailchimp calls the unguessable URL directly")
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


def hooks_save(api_url, path, debug=False, sync_response=False):
    body, error = _read_json_file(path)
    if error:
        print(error)
        return 2
    if sync_response:
        # Force response.mode sync (a template in the file's own response
        # object is kept); the API's shared validator still bounds the shape,
        # so a telegram kind or an unknown mode is rejected server-side.
        response = body.get("response") if isinstance(body.get("response"), dict) else {}
        body["response"] = {**response, "mode": "sync"}
    data = api.call(api_url, "PUT", "/api/agent/hook-triggers", body, debug=debug)
    verb = "Created" if data.get("created") else "Updated"
    print(f"{verb} {data.get('kind', 'webhook')} hook '{data.get('hook_id')}'. "
          "It is live immediately; no deploy needed.")
    for warning in data.get("warnings") or []:
        print(f"  warning: {warning}")
    if data.get("kind") == "telegram":
        print(f"  Telegram delivery URL: {data.get('url')}")
        if data.get("connection_id"):
            print(f"  Bot connection: {data.get('connection_id')}")
    elif data.get("kind") == "mailchimp":
        # Mailchimp sends no auth headers — the unguessable URL is the
        # credential — and the webhook registration was just (re)drawn with
        # the stored Mailchimp API key, so no curl hint with a bearer token.
        print(f"  Mailchimp delivery URL: {data.get('url')}")
        if data.get("list_id"):
            print(f"  Audience: {data.get('list_id')} — webhook registered on the list")
    else:
        print(f"  URL: {data.get('url')}")
        if data.get("signed"):
            # Signature-locked: the bearer token alone is rejected, so the
            # hint signs the body the way the ingress verifies it.
            header = data.get("signature_header", "x-dapier-signature")
            print(f"  Callers send: {header}: sha256=<hex HMAC-SHA256(secret, raw body)>")
        else:
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
    for warning in data.get("warnings") or []:
        print(f"  warning: {warning}")
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


def _poll_target(item):
    """What a poll watches: the URL for http, the provider target otherwise."""
    if item.get("source") and item.get("source") != "http":
        target = (item.get("bucket") or item.get("spreadsheet_id")
                  or item.get("folder_id") or item.get("for_email") or "")
        return f"{item['source']} {target}".strip()
    return f"{item.get('method', 'GET')} {item.get('url', '')}".strip()


def polls_list(api_url, debug=False):
    data = api.call(api_url, "GET", "/api/agent/poll-triggers", debug=debug)
    items = data.get("polls", [])
    if not items:
        print("No poll triggers yet. Create one with `dapier polls save`.")
    for item in items:
        state = "enabled" if item.get("enabled", True) else "disabled"
        print(f"{item.get('poll_id', ''):20} {item.get('expression', ''):40} "
              f"{state:9} {_poll_target(item)}")
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
    print(f"  Watches {_poll_target(data)} — one event per new item, no deploy needed.")
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


CREDENTIAL_FIELDS = {"slack": ["token"], "mailchimp": ["api_key"],
                     "aws": ["access_key_id", "secret_access_key"]}


def credentials_set(api_url, provider, path, debug=False):
    """Store a provider credential; the value travels only in the request body.

    Single-field providers take the raw value; multi-field providers (aws)
    take a JSON object with exactly their fields.
    """
    fields = CREDENTIAL_FIELDS.get(provider)
    if not fields:
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
    if len(fields) == 1:
        body = {fields[0]: value}
    else:
        try:
            parsed = json.loads(value)
        except ValueError:
            print(f"The {provider} credential must be a JSON object with keys: "
                  f"{', '.join(fields)}.")
            return 2
        if not isinstance(parsed, dict) or sorted(parsed) != sorted(fields) or not all(
                isinstance(parsed.get(field), str) and parsed.get(field).strip()
                for field in fields):
            print(f"The {provider} credential must be a JSON object with keys: "
                  f"{', '.join(fields)}.")
            return 2
        body = parsed
    data = api.call(api_url, "PUT", f"/api/agent/credentials/{provider}",
                    body, debug=debug)
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


def grants_list(api_url, connection_id=None, debug=False, limit=None, next_token=None):
    params = {}
    if connection_id:
        params["connection_id"] = connection_id
    if limit:
        params["limit"] = int(limit)
    if next_token:
        params["next"] = next_token
    query = f"?{urlencode(params)}" if params else ""
    data = api.call(api_url, "GET", f"/api/agent/grants{query}", debug=debug)
    items = data.get("grants", [])
    if not items:
        print("No grants. Create one with `dapier grants save`.")
        return 0
    print_grants(items)
    next_page = (data.get("paging") or {}).get("next")
    if next_page:
        print(f"\nnext page: {next_page}  (pass it to --next)")
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


def tokens_create(api_url, name, agent, debug=False, output=None):
    if output:
        from pathlib import Path
        if Path(output).expanduser().exists():
            raise ValueError(f"Token file already exists: {output}")
    data = api.call(api_url, "PUT", "/api/agent/tokens",
                    {"token_id": name, "agent": agent}, debug=debug)
    print(f"Created API token {data.get('token_id')} "
          f"(subject {data.get('subject')}, agent {data.get('agent')}).")
    if agent != "host-worker":
        print(f"Grant it access with `dapier grants save` using subject {data.get('subject')}.")
    if output:
        from pathlib import Path
        import os
        path = Path(output).expanduser()
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data["token"] + "\n")
        print(f"Stored the one-time token in {path} (owner-only).")
    else:
        print("Store the value now; it is not retrievable again:")
        print(data.get("token", ""))
    return 0


def tokens_revoke(api_url, name, debug=False):
    query = urlencode({"token_id": name})
    api.call(api_url, "DELETE", f"/api/agent/tokens?{query}", debug=debug)
    print(f"Revoked API token {name}. Presented values stop authenticating immediately.")
    return 0


def tokens_delete(api_url, name, debug=False):
    query = urlencode({"token_id": name, "purge": "1"})
    data = api.call(api_url, "DELETE", f"/api/agent/tokens?{query}", debug=debug)
    grants = data.get("grants_removed") or 0
    suffix = f" and {grants} connection grant{'s' if grants != 1 else ''}" if grants else ""
    print(f"Removed revoked API token {name}{suffix}.")
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
        # The same what-actually-happened line the console row carries:
        # trigger type plus the API's event summary, indented under the run.
        trigger = " · ".join(str(part) for part in
                             (item.get("connector"), item.get("event_type")) if part)
        summary = str(item.get("event_summary") or "")
        detail = " — ".join(part for part in (trigger, summary) if part)
        if detail:
            print(f"    {detail}")


def runs_list(api_url, limit=25, workflow=None, status=None, since=None, before=None,
              next_token=None, query=None, debug=False):
    params = {"limit": int(limit)}
    if workflow:
        params["workflow_id"] = workflow
    if status:
        params["status"] = status
    if since:
        params["since"] = since
    if before:
        params["before"] = before
    if query:
        params["q"] = query
    if next_token:
        params["next"] = next_token
    data = api.call(api_url, "GET", f"/api/agent/runs?{urlencode(params)}", debug=debug)
    items = data.get("runs", [])
    if not items:
        filtered = workflow or status or since or before or next_token or query
        print("No runs match these filters." if filtered else
              "No runs recorded yet. Runs appear once a workflow handles a trigger event.")
        return 0
    print_runs(items)
    next_page = (data.get("paging") or {}).get("next")
    if next_page:
        print(f"\nnext page: {next_page}  (pass it to --next)")
    return 0


def runs_export(api_url, out=None, max_rows=None, workflow=None, status=None,
                since=None, before=None, query=None, debug=False):
    """Run history as CSV (thin client over the agent export route): the
    list's filters, one bounded export, written to --out or the server's
    suggested filename."""
    params = {}
    if workflow:
        params["workflow_id"] = workflow
    for key, value in (("status", status), ("since", since),
                       ("before", before), ("q", query)):
        if value:
            params[key] = value
    if max_rows:
        params["max_rows"] = int(max_rows)
    data = api.call(api_url, "GET", f"/api/agent/runs/export?{urlencode(params)}",
                    debug=debug)
    # The server suggests the filename; basename keeps a hostile suggestion
    # from writing outside the caller's directory.
    path = out or os.path.basename(data.get("filename") or "") or "dapier-runs.csv"
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(data.get("csv") or "")
    note = " (capped; narrow the filters for the rest)" if data.get("truncated") else ""
    print(f"Wrote {data.get('count', 0)} runs to {path}{note}")
    return 0


def _audit_params(connection=None, action=None, actor=None, agent=None,
                  outcome=None, since=None, before=None, query=None,
                  next_token=None, limit=None):
    params = {}
    if limit:
        params["limit"] = int(limit)
    for key, value in (("connection", connection), ("action", action),
                       ("actor", actor), ("agent", agent), ("outcome", outcome),
                       ("since", since), ("before", before), ("q", query),
                       ("next", next_token)):
        if value:
            params[key] = value
    return params


def audit_list(api_url, limit=50, connection=None, action=None, actor=None,
               agent=None, outcome=None, since=None, before=None, query=None,
               next_token=None, debug=False):
    params = _audit_params(connection=connection, action=action, actor=actor,
                           agent=agent, outcome=outcome, since=since,
                           before=before, query=query, next_token=next_token,
                           limit=limit)
    data = api.call(api_url, "GET", f"/api/agent/audit?{urlencode(params)}", debug=debug)
    events = data.get("events", [])
    if not events:
        filtered = any((connection, action, actor, agent, outcome, since,
                        before, query, next_token))
        print("No audit events match these filters." if filtered else
              "No audit events yet. Actions appear as operators change connections, workflows, and triggers.")
        return 0
    print_audit(events)
    next_page = (data.get("paging") or {}).get("next")
    if next_page:
        print(f"\nnext page: {next_page}  (pass it to --next)")
    return 0


def audit_export(api_url, out=None, max_rows=None, connection=None, action=None,
                 actor=None, agent=None, outcome=None, since=None, before=None,
                 query=None, debug=False):
    params = _audit_params(connection=connection, action=action, actor=actor,
                           agent=agent, outcome=outcome, since=since,
                           before=before, query=query)
    if max_rows:
        params["max_rows"] = int(max_rows)
    data = api.call(api_url, "GET", f"/api/agent/audit/export?{urlencode(params)}",
                    debug=debug)
    # The server suggests the filename; basename keeps a hostile suggestion
    # from writing outside the caller's directory.
    path = out or os.path.basename(data.get("filename") or "") or "dapier-audit.csv"
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(data.get("csv") or "")
    note = " (capped; narrow the filters for the rest)" if data.get("truncated") else ""
    print(f"Wrote {data.get('count', 0)} audit events to {path}{note}")
    return 0


def _print_quota(quota):
    """One line for the quota block the usage and quota payloads carry."""
    if not quota or not quota.get("enabled"):
        used = (quota or {}).get("used", 0)
        print(f"Task quota: none set ({used} tasks this month — uncapped)")
        return
    used, limit = quota.get("used", 0), quota.get("limit")
    remaining = quota.get("remaining")
    tail = f", {remaining} left" if remaining is not None else ""
    print(f"Task quota: {used}/{limit} tasks this month ({quota.get('month', '')}{tail})")


def usage(api_url, debug=False, months=12):
    data = api.call(api_url, "GET", f"/api/agent/usage?months={int(months)}", debug=debug)
    items = data.get("usage", [])
    if not items:
        print("No task usage recorded yet. Usage appears once workflow actions complete.")
    else:
        print(f"{'MONTH':8} {'WORKFLOW':40} TASKS")
        for item in items:
            print(f"{item.get('month', ''):8} {item.get('workflow_id', ''):40} {item.get('tasks', 0)}")
    _print_quota(data.get("quota"))
    return 0


def quota(api_url, debug=False, command="show", limit=None):
    """Show the monthly task quota, or set it: `dapier quota set 1000` /
    `dapier quota set off` (the worker fails action steps once the month's
    budget is spent)."""
    if command == "set":
        data = api.call(api_url, "PUT", "/api/agent/quota",
                        body={"limit": limit}, debug=debug)
        _print_quota((data or {}).get("quota"))
        return 0
    data = api.call(api_url, "GET", "/api/agent/quota", debug=debug)
    _print_quota((data or {}).get("quota"))
    return 0


def errors_summary(api_url, debug=False, days=7):
    data = api.call(api_url, "GET", f"/api/agent/errors/summary?days={int(days)}", debug=debug)
    workflows = data.get("workflows") or []
    if not workflows:
        print(f"No failed runs in the last {data.get('window_days', int(days))} days.")
        return 0
    print(f"Failed runs by workflow (last {data.get('window_days', int(days))} days, "
          f"{data.get('total_failed_runs', 0)} total)")
    print(f"{'WORKFLOW':40} {'FAILED RUNS':11} LAST FAILURE")
    for item in workflows:
        last = (item.get('last_failed_at') or '-')[:19]
        print(f"{item.get('workflow_id', ''):40} {item.get('failed_runs', 0):<11} {last}")
        if item.get("last_error"):
            print(f"{'':40} {'':11} {item['last_error']}")
    return 0


def errors_send_digest(api_url, debug=False):
    """Render and email the operator error digest now (thin client over the
    agent route — the same function the daily schedule runs)."""
    data = api.call(api_url, "POST", "/api/agent/errors/digest", body={}, debug=debug)
    if data.get("skipped"):
        print(f"Nothing failed in the last {data.get('window_days', 1)} day(s); "
              "digest skipped (no email sent).")
        return 0
    print(f"Digest sent to {data.get('to', '')} "
          f"({data.get('total_failed_runs', 0)} failed runs in the last "
          f"{data.get('window_days', 1)} day(s)).")
    if data.get("subject"):
        print(data["subject"])
    return 0


def print_audit(items):
    print(f"{'TIMESTAMP':20} {'ACTION':12} {'ACTOR':34} {'AGENT':20} "
          f"{'OUTCOME':22} CONNECTION")
    for item in items:
        timestamp = (item.get("timestamp") or "-")[:19]
        error = item.get("error")
        suffix = f"  ({error})" if error else ""
        print(f"{timestamp:20} {item.get('action', ''):12} "
              f"{item.get('actor_subject', ''):34} {item.get('agent') or '-':20} "
              f"{item.get('outcome', ''):22} {item.get('connection_id', '')}{suffix}")


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


def runs_replay(api_url, run_id, from_step=None, debug=False):
    body = {"from_step": from_step} if from_step else {}
    data = api.call(api_url, "POST", f"/api/agent/runs/{quote(run_id, safe='')}/replay",
                    body=body, debug=debug)
    if from_step:
        print(f"Replay from step '{from_step}' accepted for {data.get('replayed_from') or run_id}.")
        print("Earlier steps do not run again; their recorded outputs seed the rerun.")
    else:
        print(f"Replay accepted for {data.get('replayed_from') or run_id}.")
    print(f"The re-injected run ({data.get('run_id') or 'pending'}) appears in `dapier runs list` "
          "once the worker picks it up.")
    return 0


def runs_cancel(api_url, run_id, debug=False):
    data = api.call(api_url, "POST", f"/api/agent/runs/{quote(run_id, safe='')}/cancel", body={}, debug=debug)
    run = data.get("run") or {}
    print(f"Cancelled {data.get('cancelled', 0)} parked step(s) of "
          f"{data.get('run_id') or run_id}; the run reads `{run.get('status', 'cancelled')}`.")
    print("The parked continuation is dropped: the run's remaining actions will not fire.")
    return 0


def runs_replay_failed(api_url, workflow_id, debug=False):
    data = api.call(api_url, "POST", "/api/agent/runs/replay-failed",
                    {"workflow_id": workflow_id}, debug=debug)
    print(f"Replay accepted for {data.get('replayed', 0)} failed run(s) of {workflow_id}"
          + (f"; {data.get('skipped', 0)} skipped." if data.get("skipped") else "."))
    for item in data.get("runs") or []:
        if item.get("replayed"):
            print(f"  {item.get('run_id')}: replayed as {item.get('new_run_id')}")
        else:
            print(f"  {item.get('run_id')}: skipped ({item.get('reason')})")
    print("The re-injected runs appear in `dapier runs list` once the worker picks them up.")
    return 0


def print_inbox(items):
    print(f"{'INBOX ID':44} {'CONNECTOR':10} {'STATUS':10} RECEIVED")
    for item in items:
        received = (item.get("received_at") or "-")[:19]
        print(f"{item.get('inbox_id', ''):44} {item.get('connector') or '-':10} "
              f"{item.get('status', ''):10} {received}")


def inbox_list(api_url, connector=None, limit=25, next_token=None, debug=False):
    params = {"limit": int(limit)}
    if connector:
        params["connector"] = connector
    if next_token:
        params["next"] = next_token
    data = api.call(api_url, "GET",
                    f"/api/agent/triggers/inbox?{urlencode(params)}", debug=debug)
    items = data.get("events", [])
    if not items:
        print("Inbox is empty. Every trigger event lands here once the worker picks it up — "
              "matched or not.")
        return 0
    print_inbox(items)
    next_page = (data.get("paging") or {}).get("next")
    if next_page:
        print(f"\nnext page: {next_page}  (pass it to --next)")
    return 0


def inbox_show(api_url, inbox_id, debug=False):
    data = api.call(api_url, "GET", f"/api/agent/triggers/inbox/{quote(inbox_id, safe='')}", debug=debug)
    event = data.get("event") or {}
    print(f"inbox event: {event.get('inbox_id') or inbox_id}")
    for key in ("connector", "event", "source", "status", "occurred_at", "received_at",
                "processed_at", "matched", "error"):
        if event.get(key) not in (None, "", []):
            print(f"  {key}: {event[key]}")
    if event.get("data") not in (None, {}, []):
        print(f"  data: {json.dumps(event['data'], sort_keys=True, default=str)}")
    return 0


def inbox_replay(api_url, inbox_id, debug=False):
    data = api.call(api_url, "POST", f"/api/agent/triggers/inbox/{quote(inbox_id, safe='')}/replay", body={}, debug=debug)
    print(f"Replay accepted for {data.get('replayed_from') or inbox_id}.")
    print(f"The re-injected event ({data.get('run_id') or 'pending'}) appears in `dapier runs list` "
          "once the worker picks it up.")
    return 0


def storage_get(api_url, workflow, key, debug=False):
    data = api.call(api_url, "GET",
                    f"/api/agent/storage/{quote(workflow, safe='')}?key={quote(key, safe='')}",
                    debug=debug)
    print(f"{data.get('key') or key} = {data.get('value', '')}")
    for stamp in ("updated_at", "expires"):
        if data.get(stamp):
            print(f"  {stamp}: {data[stamp]}")
    return 0


def storage_set(api_url, workflow, key, value, ttl_seconds=None, debug=False):
    body = {"key": key, "value": value}
    if ttl_seconds is not None:
        body["ttl_seconds"] = int(ttl_seconds)
    data = api.call(api_url, "POST", f"/api/agent/storage/{quote(workflow, safe='')}",
                    body=body, debug=debug)
    suffix = f" (expires {data['expires']})" if data.get("expires") else ""
    print(f"Stored {data.get('key') or key} for {workflow}{suffix}.")
    print("Workflow runs read it back with the storage_get action (`{steps.<id>.output.value}`).")
    return 0


def storage_find(api_url, workflow, prefix="", limit=None, debug=False):
    query = f"prefix={quote(prefix, safe='')}"
    if limit:
        query += f"&limit={int(limit)}"
    data = api.call(api_url, "GET",
                    f"/api/agent/storage/{quote(workflow, safe='')}?{query}", debug=debug)
    items = data.get("items", [])
    if not items:
        print(f"No stored keys under '{prefix}' for {workflow}.")
        return 0
    print(f"{'KEY':40} VALUE")
    for item in items:
        print(f"{item.get('key', ''):40} {item.get('value', '')}")
    return 0


def storage_delete(api_url, workflow, key, debug=False):
    data = api.call(api_url, "DELETE",
                    f"/api/agent/storage/{quote(workflow, safe='')}?key={quote(key, safe='')}",
                    debug=debug)
    if data.get("deleted"):
        print(f"Deleted {key} from {workflow}'s storage.")
    else:
        print(f"{key} was not stored for {workflow} (nothing to delete).")
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


def agent_tasks_list(api_url, debug=False, limit=None, status=None):
    """Host jobs the agent action enqueued, as `dapier worker` leaves them."""
    params = []
    if limit:
        params.append(f"limit={quote(str(limit), safe='')}")
    if status:
        params.append(f"status={quote(status, safe='')}")
    path = "/api/agent/agent-tasks" + ("?" + "&".join(params) if params else "")
    data = api.call(api_url, "GET", path, debug=debug)
    items = data.get("tasks") or []
    if not items:
        print("No host tasks yet. An agent action enqueues one when its workflow runs.")
        return 0
    print(f"{'STATUS':10} {'WORKFLOW':30} TASK ID")
    for item in items:
        print(f"{item.get('status', ''):10} {str(item.get('workflow') or ''):30} "
              f"{item.get('task_id') or ''}")
        if item.get("email_subject"):
            print(f"           task {item['email_subject']}")
        if item.get("summary"):
            print(f"           result {item['summary']}")
        if item.get("error"):
            print(f"           error {item['error']}")
    return 0


def workers_list(api_url, debug=False):
    """Host workers (`dapier worker` processes), most recently seen first."""
    data = api.call(api_url, "GET", "/api/agent/workers", debug=debug)
    workers = data.get("workers") or []
    if not workers:
        print("No worker has checked in yet. Start one with `dapier worker` — "
              "agent tasks stay queued until a worker picks them up.")
        return 0
    print(f"{'STATUS':10} {'WORKER':44} CURRENT TASK")
    for worker in workers:
        state = "active" if worker.get("active") else "offline"
        current = worker.get("current_task_id") or "—"
        print(f"{state:10} {str(worker.get('worker_id') or ''):44} {current}")
        where = " @ ".join(part for part in (worker.get("hostname"),
                                             worker.get("workspace_root")) if part)
        if where:
            print(f"           {where}")
        if worker.get("last_task_id"):
            print(f"           last {worker['last_task_id']}"
                  f" -> {worker.get('last_status') or 'unknown'}")
    return 0


def agent_tasks_show(api_url, task_id, debug=False):
    data = api.call(api_url, "GET", "/api/agent/agent-tasks?task_id=" + quote(task_id, safe=""),
                    debug=debug)
    print(json.dumps(data["task"], indent=2, ensure_ascii=False))
    return 0


def worker_run(api_url=None, *, token_file=None, workspace_root=None,
               max_runtime=3600, once=False):
    """Run headless jobs through the authenticated HTTPS host API."""
    from src.dapier.headless_worker import DEFAULT_ROOT, DEFAULT_TOKEN_FILE, serve

    serve(api_url=api_url or config.api_url(),
          token_file=token_file or DEFAULT_TOKEN_FILE,
          workspace_root=workspace_root or DEFAULT_ROOT,
          max_runtime=max_runtime, once=once)
    return 0
