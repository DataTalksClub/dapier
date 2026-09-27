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


def workflows_list(api_url, debug=False):
    data = api.call(api_url, "GET", "/api/agent/designer/workflows", debug=debug)
    items = data.get("workflows", [])
    if not items:
        print("No workflows yet. Create one in the console or run `dapier workflows save`.")
    for item in items:
        state = "On" if item.get("enabled", True) else "Off"
        trigger = f"{item.get('connector', '?')}.{item.get('event', '?')}"
        extra = (item.get("triggerCount") or 1) - 1
        if extra > 0:
            trigger = f"{trigger} +{extra}"
        print(f"{item.get('source', ''):36} {trigger:34} "
              f"{item.get('actionCount', 0)} action(s) {state}")
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
    state = "On" if enabled else "Off"
    print(f"{data.get('file') or file} is {state} — live now.")
    if data.get("commit"):
        print(f"Committed {str(data['commit'])[:7]}.")
    if data.get("git_sync_error"):
        print(f"Warning: the git commit failed ({data['git_sync_error']}); "
              "the next deploy may revert this toggle.")
    return 0


def workflows_duplicate(api_url, file, name=None, debug=False):
    """Copy a saved workflow under a new id (the server slugifies `--name`,
    default `<id>-copy`) through the same commit-and-publish path as a save."""
    body = {"name": name} if name else {}
    data = api.call(api_url, "POST", f"/api/agent/designer/workflows/{file}/duplicate",
                    body, debug=debug)
    new_file = data.get("file") or file
    if data.get("published"):
        print(f"Duplicated {file} as {new_file} ({str(data.get('commit', ''))[:7]}) and published it live.")
    else:
        print(f"Duplicated {file} as {new_file} ({str(data.get('commit', ''))[:7]}). "
              "The deploy pipeline publishes it in a few minutes.")
    return 0


def workflows_versions(api_url, file, debug=False):
    """Version history for one workflow: who published what, when, and why."""
    data = api.call(api_url, "GET", f"/api/agent/designer/workflows/{file}/versions", debug=debug)
    print(f"{data.get('workflow') or file} is at revision {data.get('revision', '?')}.")
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
    """Restore an old version: re-committed through the save path so git and
    the live engine agree, and recorded in the history as a rollback."""
    body = {} if revision is None else {"revision": int(revision)}
    label = f"v{revision}" if revision is not None else "the previous version"
    data = api.call(api_url, "POST", f"/api/agent/designer/workflows/{file}/rollback",
                    body, debug=debug)
    if data.get("published"):
        print(f"Rolled {file} back to {label} "
              f"({str(data.get('commit', ''))[:7]}) and published it live.")
    else:
        print(f"Rolled {file} back to {label} "
              f"({str(data.get('commit', ''))[:7]}). The deploy pipeline publishes it in a few minutes.")
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


def runs_list(api_url, limit=25, workflow=None, status=None, since=None, before=None,
              next_token=None, debug=False):
    params = {"limit": int(limit)}
    if workflow:
        params["workflow_id"] = workflow
    if status:
        params["status"] = status
    if since:
        params["since"] = since
    if before:
        params["before"] = before
    if next_token:
        params["next"] = next_token
    data = api.call(api_url, "GET", f"/api/agent/runs?{urlencode(params)}", debug=debug)
    items = data.get("runs", [])
    if not items:
        filtered = workflow or status or since or before or next_token
        print("No runs match these filters." if filtered else
              "No runs recorded yet. Runs appear once a workflow handles a trigger event.")
        return 0
    print_runs(items)
    next_page = (data.get("paging") or {}).get("next")
    if next_page:
        print(f"\nnext page: {next_page}  (pass it to --next)")
    return 0


def usage(api_url, debug=False, months=12):
    data = api.call(api_url, "GET", f"/api/agent/usage?months={int(months)}", debug=debug)
    items = data.get("usage", [])
    if not items:
        print("No task usage recorded yet. Usage appears once workflow actions complete.")
        return 0
    print(f"{'MONTH':8} {'WORKFLOW':40} TASKS")
    for item in items:
        print(f"{item.get('month', ''):8} {item.get('workflow_id', ''):40} {item.get('tasks', 0)}")
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


def inbox_list(api_url, connector=None, limit=25, debug=False):
    query = f"limit={int(limit)}"
    if connector:
        query += f"&connector={quote(connector, safe='')}"
    data = api.call(api_url, "GET", f"/api/agent/triggers/inbox?{query}", debug=debug)
    items = data.get("events", [])
    if not items:
        print("Inbox is empty. Every trigger event lands here once the worker picks it up — "
              "matched or not.")
        return 0
    print_inbox(items)
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
