"""`dapier` command line interface."""

import argparse
import sys

from . import auth, commands, config
from .api import ApiError


def build_parser():
    parser = argparse.ArgumentParser(prog="dapier", description="Dapier OAuth token factory CLI")
    parser.add_argument("--api-url", default=None, help="Dapier API base URL")
    parser.add_argument("--debug", action="store_true", help="Print requests (never credentials)")
    sub = parser.add_subparsers(dest="group", required=True)

    auth_p = sub.add_parser("auth", help="DTC shared-auth session")
    auth_sub = auth_p.add_subparsers(dest="command", required=True)
    login_p = auth_sub.add_parser(
        "login", help="Sign in by pairing this device (or --browser for the localhost flow)")
    login_p.add_argument("--timeout", type=int, default=600)
    login_p.add_argument("--browser", action="store_true",
                         help="Use the browser loopback flow instead of device pairing")
    auth_sub.add_parser("status", help="Show the stored session (no secrets)")
    auth_sub.add_parser("logout", help="Forget the stored session (revokes device sessions)")

    conn_p = sub.add_parser("connections", help="Named provider connections")
    conn_sub = conn_p.add_subparsers(dest="command", required=True)
    conn_sub.add_parser("list", help="List connections granted to this identity")
    show_p = conn_sub.add_parser("show", help="Show one connection (no secrets)")
    show_p.add_argument("connection_id")
    show_p.add_argument("--agent", default=None)
    connect_p = conn_sub.add_parser("connect", help="Run provider consent for a connection")
    connect_p.add_argument("connection_id")
    connect_p.add_argument("--agent", required=True)
    connect_p.add_argument("--timeout", type=int, default=300)
    import_p = conn_sub.add_parser("import", help="One-time operator import of an existing credential")
    import_p.add_argument("connection_id")
    import_p.add_argument("--provider", required=True)
    import_p.add_argument("--client-id", default=None,
                          help="Optional; defaults to the shared deploy-time OAuth client")
    import_p.add_argument("--client-secret-file", default=None,
                          help="Optional; needed only for refresh tokens issued by another client")
    import_p.add_argument("--authorized-user-file", default=None,
                          help="Refresh-token JSON for OAuth providers")
    import_p.add_argument("--token-file", default=None,
                          help="Pasted provider token for slack/telegram connections")
    import_p.add_argument("--expected-account", default=None)
    import_p.add_argument("--scopes", nargs="*", default=[])
    import_p.add_argument("--root-path", default=None,
                          help="Dropbox only: folder webhook resolution lists (empty lists everything)")
    revoke_p = conn_sub.add_parser("revoke", help="Revoke a connection's stored tokens")
    revoke_p.add_argument("connection_id")

    cred_p = sub.add_parser("credentials", help="Provider credentials used by workflows")
    cred_sub = cred_p.add_subparsers(dest="command", required=True)
    cred_set_p = cred_sub.add_parser("set", help="Store a credential (Slack bot token or Mailchimp API key)")
    cred_set_p.add_argument("provider")
    cred_set_p.add_argument("--file", required=True,
                            help="File with the secret value, or - for stdin")

    grants_p = sub.add_parser("grants", help="Agent access grants per connection")
    grants_sub = grants_p.add_subparsers(dest="command", required=True)
    grants_list_p = grants_sub.add_parser("list", help="List grants")
    grants_list_p.add_argument("--connection", default=None)
    grants_save_p = grants_sub.add_parser("save", help="Create or update a grant from a JSON file")
    grants_save_p.add_argument("file", help="Path to the grant JSON, or - for stdin")
    grants_del_p = grants_sub.add_parser("delete", help="Revoke one grant")
    grants_del_p.add_argument("connection_id")
    grants_del_p.add_argument("grantee", help="The grant's grantee ID (subject#agent)")

    tokens_p = sub.add_parser("tokens", help="Operator-issued API tokens for headless consumers")
    tokens_sub = tokens_p.add_subparsers(dest="command", required=True)
    tokens_sub.add_parser("list", help="List API tokens (no secrets)")
    tokens_create_p = tokens_sub.add_parser("create", help="Issue an API token; the value prints once")
    tokens_create_p.add_argument("--name", required=True,
                                 help="Token ID; the machine subject becomes token:<name>")
    tokens_create_p.add_argument("--agent", required=True,
                                 help="The one agent name this token may act as")
    tokens_del_p = tokens_sub.add_parser("revoke", help="Revoke an API token")
    tokens_del_p.add_argument("name")

    sub.add_parser("overview", help="Operator overview: workflows, connections, credentials")

    runs_p = sub.add_parser("runs", help="Workflow run history (one run per trigger event)")
    runs_sub = runs_p.add_subparsers(dest="command", required=True)
    runs_list_p = runs_sub.add_parser("list", help="Recent runs, newest first")
    runs_list_p.add_argument("--limit", type=int, default=25)
    runs_show_p = runs_sub.add_parser("show", help="Show one run's step-by-step flow")
    runs_show_p.add_argument("run_id", help="Run ID from `dapier runs list` (or the console)")
    runs_replay_p = runs_sub.add_parser("replay", help="Re-run a past run by re-injecting its original trigger event")
    runs_replay_p.add_argument("run_id", help="Run ID from `dapier runs list` (or the console)")

    oac_p = sub.add_parser("oauth-clients", help="Shared OAuth clients per provider (same as the console's Credentials view)")
    oac_sub = oac_p.add_subparsers(dest="command", required=True)
    oac_sub.add_parser("list", help="Show the configured shared OAuth clients (no secrets)")
    oac_set_p = oac_sub.add_parser("set", help="Store the shared OAuth client for a provider")
    oac_set_p.add_argument("provider", help="dropbox, google, or youtube")
    oac_set_p.add_argument("--client-id", required=True)
    oac_set_p.add_argument("--client-secret-file", required=True,
                           help="File with the client secret, or - for stdin")

    token_p = sub.add_parser("token", help="Short-lived provider access tokens")
    token_sub = token_p.add_subparsers(dest="command", required=True)
    exec_p = token_sub.add_parser("exec", help="Run a command with a fresh token in its environment")
    exec_p.add_argument("connection_id")
    exec_p.add_argument("--agent", required=True)
    exec_p.add_argument("argv", nargs="*")
    write_p = token_sub.add_parser("write", help="Write a fresh token to a private file")
    write_p.add_argument("connection_id")
    write_p.add_argument("--agent", required=True)
    write_p.add_argument("--output", required=True)
    write_p.add_argument("--force", action="store_true")

    trig_p = sub.add_parser("triggers", help="Email triggers (name@dtcdev.click)")
    trig_sub = trig_p.add_subparsers(dest="command", required=True)
    trig_sub.add_parser("list", help="List email triggers and YAML-claimed routes")
    trig_show_p = trig_sub.add_parser("show", help="Show one email trigger")
    trig_show_p.add_argument("name")
    trig_save_p = trig_sub.add_parser("save", help="Create or update a trigger from a JSON file")
    trig_save_p.add_argument("file", help="Path to the trigger JSON, or - for stdin")
    trig_del_p = trig_sub.add_parser("delete", help="Delete an email trigger")
    trig_del_p.add_argument("name")

    wf_p = sub.add_parser("workflows", help="Workflow YAML committed by the console designer")
    wf_sub = wf_p.add_subparsers(dest="command", required=True)
    wf_sub.add_parser("list", help="List workflows with their live published state")
    wf_show_p = wf_sub.add_parser("show", help="Show one workflow (JSON)")
    wf_show_p.add_argument("file", help="Workflow file name, e.g. my-flow.yaml")
    wf_save_p = wf_sub.add_parser("save", help="Commit a workflow YAML to the repo and publish it live")
    wf_save_p.add_argument("file", help="Path to the workflow YAML, or - for stdin")
    wf_save_p.add_argument("--rename-from", default=None,
                           help="Previous file name when the workflow was renamed")
    wf_draft_p = wf_sub.add_parser("draft",
                                   help="Generate a draft workflow YAML from a natural-language prompt")
    wf_draft_p.add_argument("prompt",
                            help='What the workflow should do, e.g. "when someone emails todo@, push it to slack"')
    wf_draft_p.add_argument("--save", action="store_true",
                            help="Also save the draft through the `workflows save` path (only when it validates)")
    wf_enable_p = wf_sub.add_parser("enable", help="Publish a workflow as enabled (live immediately)")
    wf_enable_p.add_argument("file", help="Workflow file name, e.g. my-flow.yaml")
    wf_disable_p = wf_sub.add_parser("disable", help="Publish a workflow as disabled (live immediately)")
    wf_disable_p.add_argument("file", help="Workflow file name, e.g. my-flow.yaml")
    wf_test_p = wf_sub.add_parser("test", help="Test-run a workflow YAML against a sample event (dry-run)")
    wf_test_p.add_argument("file", help="Path to the workflow YAML, or - for stdin")
    wf_test_p.add_argument("--event", required=True,
                           help="Sample event JSON, inline or @file (e.g. --event @event.json)")
    wf_test_p.add_argument("--execute", action="store_true",
                           help="Actually run the actions (real side effects); default is a dry-run")
    hook_p = sub.add_parser("hooks", help="Webhook and Telegram triggers")
    hook_sub = hook_p.add_subparsers(dest="command", required=True)
    hook_list_p = hook_sub.add_parser("list", help="List hook triggers")
    hook_list_p.add_argument("--kind", default=None, choices=["webhook", "telegram"])
    hook_show_p = hook_sub.add_parser("show", help="Show one hook trigger, including its token")
    hook_show_p.add_argument("name")
    hook_save_p = hook_sub.add_parser("save", help="Create or update a hook trigger from a JSON file")
    hook_save_p.add_argument("file", help="Path to the hook JSON, or - for stdin")
    hook_del_p = hook_sub.add_parser("delete", help="Delete a hook trigger")
    hook_del_p.add_argument("name")
    hook_del_p.add_argument("--kind", default=None, choices=["webhook", "telegram"])
    sched_p = sub.add_parser("schedules", help="Cron and rate schedule triggers (EventBridge rules)")
    sched_sub = sched_p.add_subparsers(dest="command", required=True)
    sched_sub.add_parser("list", help="List schedule triggers")
    sched_save_p = sched_sub.add_parser("save", help="Create or update a schedule trigger from a JSON file")
    sched_save_p.add_argument("file", help="Path to the schedule JSON, or - for stdin")
    sched_del_p = sched_sub.add_parser("delete", help="Delete a schedule trigger and its rule")
    sched_del_p.add_argument("name")
    return parser


def main(argv=None):
    raw = list(sys.argv[1:] if argv is None else argv)
    # Split the child command on the first `--` before argparse sees it, so
    # child flags never confuse option parsing (nargs=REMAINDER would swallow
    # our own options).
    child = None
    if "--" in raw:
        index = raw.index("--")
        raw, child = raw[:index], raw[index + 1:]
    args = build_parser().parse_args(raw)
    api_url = config.api_url(args.api_url)
    debug = args.debug
    try:
        if args.group == "auth":
            return cmd_auth(args, api_url)
        if args.group == "connections":
            return cmd_connections(args, api_url, debug)
        if args.group == "token":
            return cmd_token(args, api_url, debug, child)
        if args.group == "triggers":
            return cmd_triggers(args, api_url, debug)
        if args.group == "workflows":
            return cmd_workflows(args, api_url, debug)
        if args.group == "hooks":
            return cmd_hooks(args, api_url, debug)
        if args.group == "schedules":
            return cmd_schedules(args, api_url, debug)
        if args.group == "credentials":
            return cmd_credentials(args, api_url, debug)
        if args.group == "grants":
            return cmd_grants(args, api_url, debug)
        if args.group == "tokens":
            return cmd_tokens(args, api_url, debug)
        if args.group == "overview":
            return commands.overview(api_url, debug)
        if args.group == "runs":
            return cmd_runs(args, api_url, debug)
        if args.group == "oauth-clients":
            return cmd_oauth_clients(args, api_url, debug)
    except ApiError as exc:
        print(f"Error: {exc}")
        if exc.status == 401:
            return 3
        if exc.status in (403, 404):
            return 4
        return 5
    except auth.LoginError as exc:
        print(f"Error: {exc}")
        return 3
    return 2


def cmd_auth(args, api_url):
    if args.command == "login":
        if args.browser:
            session = auth.login(api_url, timeout=args.timeout)
        else:
            try:
                session = auth.login_device(api_url, timeout=args.timeout)
            except auth.DeviceFlowUnavailable:
                print("This API has no device pairing; falling back to the browser flow.")
                session = auth.login(api_url, timeout=args.timeout)
        print(f"Signed in as {session.get('email') or session.get('subject')}.")
        return 0
    if args.command == "status":
        info = auth.describe(config.load_session())
        if not info["signed_in"]:
            print("Not signed in. Run `dapier auth login`.")
            return 3
        state = "expired; run `dapier auth login`" if info["expired"] else "valid"
        print(f"Signed in as {info.get('email')} ({info.get('subject')}); session {state}.")
        return 0 if not info["expired"] else 3
    if args.command == "logout":
        stored = config.load_session()
        auth.revoke_session(api_url, stored)
        config.clear_session()
        print("Signed out.")
        return 0
    return 2


def cmd_connections(args, api_url, debug):
    if args.command == "list":
        return commands.connections_list(api_url, debug)
    if args.command == "show":
        return commands.connections_show(api_url, args.connection_id, args.agent, debug)
    if args.command == "connect":
        return commands.connections_connect(api_url, args.connection_id, args.agent, args.timeout, debug)
    if args.command == "import":
        return commands.connections_import(
            api_url, args.connection_id, args.provider, args.client_id,
            args.client_secret_file, args.authorized_user_file,
            expected_account_id=args.expected_account, scopes=args.scopes, debug=debug,
            token_path=args.token_file, root_path=args.root_path,
        )
    if args.command == "revoke":
        return commands.connections_revoke(api_url, args.connection_id, debug)
    return 2


def cmd_token(args, api_url, debug, child=None):
    if args.command == "exec":
        argv = list(child) if child is not None else list(args.argv)
        return commands.token_exec(api_url, args.connection_id, args.agent, argv, debug)
    if args.command == "write":
        return commands.token_write(api_url, args.connection_id, args.agent,
                                    args.output, args.force, debug)
    return 2


def cmd_triggers(args, api_url, debug):
    if args.command == "list":
        return commands.triggers_list(api_url, debug)
    if args.command == "show":
        return commands.triggers_show(api_url, args.name, debug)
    if args.command == "save":
        return commands.triggers_save(api_url, args.file, debug)
    if args.command == "delete":
        return commands.triggers_delete(api_url, args.name, debug)
    return 2


def cmd_workflows(args, api_url, debug):
    if args.command == "list":
        return commands.workflows_list(api_url, debug)
    if args.command == "show":
        return commands.workflows_show(api_url, args.file, debug)
    if args.command == "save":
        return commands.workflows_save(api_url, args.file, args.rename_from, debug)
    if args.command == "draft":
        return commands.workflows_draft(api_url, args.prompt, save=args.save, debug=debug)
    if args.command in ("enable", "disable"):
        return commands.workflows_set_enabled(api_url, args.file, args.command == "enable", debug)
    if args.command == "test":
        return commands.workflows_test(api_url, args.file, args.event, args.execute, debug)
    return 2


def cmd_hooks(args, api_url, debug):
    if args.command == "list":
        return commands.hooks_list(api_url, kind=args.kind, debug=debug)
    if args.command == "show":
        return commands.hooks_show(api_url, args.name, debug)
    if args.command == "save":
        return commands.hooks_save(api_url, args.file, debug)
    if args.command == "delete":
        return commands.hooks_delete(api_url, args.name, kind=args.kind, debug=debug)
    return 2


def cmd_schedules(args, api_url, debug):
    if args.command == "list":
        return commands.schedules_list(api_url, debug)
    if args.command == "save":
        return commands.schedules_save(api_url, args.file, debug)
    if args.command == "delete":
        return commands.schedules_delete(api_url, args.name, debug)
    return 2


def cmd_credentials(args, api_url, debug):
    if args.command == "set":
        return commands.credentials_set(api_url, args.provider, args.file, debug)
    return 2


def cmd_grants(args, api_url, debug):
    if args.command == "list":
        return commands.grants_list(api_url, args.connection, debug)
    if args.command == "save":
        return commands.grants_save(api_url, args.file, debug)
    if args.command == "delete":
        return commands.grants_delete(api_url, args.connection_id, args.grantee, debug)
    return 2


def cmd_tokens(args, api_url, debug):
    if args.command == "list":
        return commands.tokens_list(api_url, debug)
    if args.command == "create":
        return commands.tokens_create(api_url, args.name, args.agent, debug)
    if args.command == "revoke":
        return commands.tokens_revoke(api_url, args.name, debug)
    return 2


def cmd_oauth_clients(args, api_url, debug):
    if args.command == "list":
        return commands.oauth_clients_list(api_url, debug)
    if args.command == "set":
        return commands.oauth_clients_set(api_url, args.provider, args.client_id,
                                          args.client_secret_file, debug)
    return 2


def cmd_runs(args, api_url, debug):
    if args.command == "list":
        return commands.runs_list(api_url, args.limit, debug)
    if args.command == "show":
        return commands.runs_show(api_url, args.run_id, debug)
    if args.command == "replay":
        return commands.runs_replay(api_url, args.run_id, debug)
    return 2


if __name__ == "__main__":
    sys.exit(main())
