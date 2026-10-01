"""The `dapier connections` and `dapier token` nouns."""

from .. import commands

GROUPS = ("connections", "token",)


def register(sub):
    conn_p = sub.add_parser("connections", help="Named provider connections")
    conn_sub = conn_p.add_subparsers(dest="command", required=True)
    conn_list_p = conn_sub.add_parser("list", help="List connections granted to this identity")
    conn_list_p.add_argument("--limit", type=int, default=None,
                             help="Page size (default: the API's)")
    conn_list_p.add_argument("--next", dest="next_token",
                             help="Page token from the previous call's `next page:` footer")
    conn_list_p.add_argument("--all", action="store_true", dest="list_all",
                             help="Operator: list every connection, not just your grants")
    show_p = conn_sub.add_parser("show", help="Show one connection (no secrets)")
    show_p.add_argument("connection_id")
    show_p.add_argument("--agent", default=None)
    connect_p = conn_sub.add_parser("connect", help="Run provider consent for a connection")
    connect_p.add_argument("connection_id")
    connect_p.add_argument("--agent", required=True)
    connect_p.add_argument("--timeout", type=int, default=300)
    create_p = conn_sub.add_parser("create", help="Provision a new OAuth connection before consent")
    create_p.add_argument("connection_id")
    create_p.add_argument("--provider", required=True,
                          choices=("google", "youtube", "dropbox", "zoom"))
    create_p.add_argument("--display-name", default=None)
    create_p.add_argument("--scopes", nargs="+", required=True, metavar="SCOPE")
    create_p.add_argument("--root-path", default=None, help="Dropbox only: listing root")
    edit_p = conn_sub.add_parser("edit", help="Edit connection display name, scopes, or Dropbox path")
    edit_p.add_argument("connection_id")
    edit_p.add_argument("--display-name", default=None)
    edit_p.add_argument("--scopes", nargs="+", default=None, metavar="SCOPE")
    root_group = edit_p.add_mutually_exclusive_group()
    root_group.add_argument("--root-path", default=None, help="Dropbox only: listing root")
    root_group.add_argument("--clear-root-path", action="store_true", help="Dropbox only: list from the Dropbox root")
    scopes_p = conn_sub.add_parser("scopes", help="Replace a connection's requested OAuth scopes")
    scopes_p.add_argument("connection_id")
    scopes_p.add_argument("--scopes", nargs="+", required=True, metavar="SCOPE")
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
                          help="Provider token or Zoom webhook Secret Token (read from file)")
    import_p.add_argument("--signing-secret-file", default=None,
                          help="Slack only: Events API signing secret (read from file)")
    import_p.add_argument("--display-name", default=None)
    import_p.add_argument("--expected-account", default=None)
    import_p.add_argument("--scopes", nargs="*", default=[])
    import_p.add_argument("--root-path", default=None,
                          help="Dropbox only: folder webhook resolution lists (empty lists everything)")
    revoke_p = conn_sub.add_parser("revoke", help="Revoke a connection's stored tokens")
    revoke_p.add_argument("connection_id")
    delete_p = conn_sub.add_parser("delete", help="Delete a connection outright (operator)")
    delete_p.add_argument("connection_id")
    delete_p.add_argument("--force", action="store_true",
                          help="Delete even when workflows or hook triggers still reference it")
    discover_p = conn_sub.add_parser(
        "discover", help="List a connection's discovery resources, or one resource's items")
    discover_p.add_argument("connection_id")
    discover_p.add_argument("resource", nargs="?", default=None,
                            help="Resource to list, e.g. spreadsheets or channels (omit to list resources)")
    discover_p.add_argument("--param", action="append", nargs="*", default=[],
                            metavar="KEY=VALUE",
                            help="Discovery param, repeatable (e.g. --param folder=/invoices)")
    test_p = conn_sub.add_parser("test", help="Test a connection's stored tokens against its provider")
    test_p.add_argument("connection_id")
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


def run(args, api_url, debug, child=None):
    if args.group == "token":
            if args.command == "exec":
                argv = list(child) if child is not None else list(args.argv)
                return commands.token_exec(api_url, args.connection_id, args.agent, argv, debug)
            if args.command == "write":
                return commands.token_write(api_url, args.connection_id, args.agent,
                                            args.output, args.force, debug)
            return 2
    if args.command == "list":
        return commands.connections_list(api_url, debug, limit=args.limit,
                                         next_token=args.next_token,
                                         list_all=args.list_all)
    if args.command == "show":
        return commands.connections_show(api_url, args.connection_id, args.agent, debug)
    if args.command == "connect":
        return commands.connections_connect(api_url, args.connection_id, args.agent, args.timeout, debug)
    if args.command == "create":
        return commands.connections_create(
            api_url, args.connection_id, args.provider, args.scopes,
            display_name=args.display_name, root_path=args.root_path, debug=debug,
        )
    if args.command == "edit":
        root_path = "" if args.clear_root_path else args.root_path
        return commands.connections_edit(
            api_url, args.connection_id, display_name=args.display_name,
            scopes=args.scopes, root_path=root_path, debug=debug,
        )
    if args.command == "scopes":
        return commands.connections_scopes(api_url, args.connection_id, args.scopes, debug)
    if args.command == "import":
        return commands.connections_import(
            api_url, args.connection_id, args.provider, args.client_id,
            args.client_secret_file, args.authorized_user_file,
            expected_account_id=args.expected_account, scopes=args.scopes, debug=debug,
            token_path=args.token_file, root_path=args.root_path,
            display_name=args.display_name,
            signing_secret_path=args.signing_secret_file,
        )
    if args.command == "revoke":
        return commands.connections_revoke(api_url, args.connection_id, debug)
    if args.command == "delete":
        return commands.connections_delete(api_url, args.connection_id,
                                           force=args.force, debug=debug)
    if args.command == "discover":
        return commands.connections_discover(
            api_url, args.connection_id, args.resource,
            params=[pair for group in args.param for pair in group], debug=debug)
    if args.command == "test":
        return commands.connections_test(api_url, args.connection_id, debug)
    return 2
