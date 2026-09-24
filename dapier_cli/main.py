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
    login_p = auth_sub.add_parser("login", help="Sign in with the DTC identity in a browser")
    login_p.add_argument("--timeout", type=int, default=180)
    auth_sub.add_parser("status", help="Show the stored session (no secrets)")
    auth_sub.add_parser("logout", help="Forget the stored session")

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
    import_p.add_argument("--authorized-user-file", required=True)
    import_p.add_argument("--expected-account", default=None)
    import_p.add_argument("--scopes", nargs="*", default=[])

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
        )
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


if __name__ == "__main__":
    sys.exit(main())
