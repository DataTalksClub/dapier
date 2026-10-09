"""The `dapier connections` and `dapier token` nouns."""

from .. import commands

GROUPS = ("connections", "token",)

REF_HELP = ("the connection: '<service> <account>' such as 'drive alexey@example.com' "
            "or 'drive datatalks' (any unique part of the account), a bare service "
            "when only one account has it, or the internal connection id")




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
    show_p.add_argument("connection", nargs="+", metavar="CONNECTION", help=REF_HELP)
    show_p.add_argument("--agent", default=None)
    connect_p = conn_sub.add_parser("connect", help="Run provider consent for a connection")
    connect_p.add_argument("connection", nargs="+", metavar="CONNECTION", help=REF_HELP)
    connect_p.add_argument("--agent", required=True)
    connect_p.add_argument("--timeout", type=int, default=300)
    create_p = conn_sub.add_parser("create", help="Provision a new OAuth connection before consent")
    create_p.add_argument("connection_id", nargs="?", default=None,
                          help="optional internal id; generated when omitted")
    create_p.add_argument("--provider", required=True,
                          choices=("google", "youtube", "dropbox", "zoom"))
    create_p.add_argument("--scopes", nargs="+", default=None, metavar="SCOPE",
                          help="Requested scopes (required except for Zoom, which "
                               "defaults to the read scopes its actions use)")
    create_p.add_argument("--kind", choices=("api", "webhook"), default=None,
                          help="Zoom only: connection kind (default api; a webhook "
                               "is created with `connections import --token-file`)")
    create_p.add_argument("--root-path", default=None, help="Dropbox only: listing root")
    edit_p = conn_sub.add_parser("edit", help="Edit connection scopes or Dropbox path")
    edit_p.add_argument("connection", nargs="+", metavar="CONNECTION", help=REF_HELP)
    edit_p.add_argument("--scopes", nargs="+", default=None, metavar="SCOPE")
    root_group = edit_p.add_mutually_exclusive_group()
    root_group.add_argument("--root-path", default=None, help="Dropbox only: listing root")
    root_group.add_argument("--clear-root-path", action="store_true", help="Dropbox only: list from the Dropbox root")
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
    import_p.add_argument("--expected-account", default=None)
    import_p.add_argument("--scopes", nargs="*", default=[])
    import_p.add_argument("--root-path", default=None,
                          help="Dropbox only: folder webhook resolution lists (empty lists everything)")
    revoke_p = conn_sub.add_parser("revoke", help="Revoke a connection's stored tokens")
    revoke_p.add_argument("connection", nargs="+", metavar="CONNECTION", help=REF_HELP)
    delete_p = conn_sub.add_parser("delete", help="Delete a connection outright (operator)")
    delete_p.add_argument("connection", nargs="+", metavar="CONNECTION", help=REF_HELP)
    delete_p.add_argument("--force", action="store_true",
                          help="Delete even when workflows or hook triggers still reference it")
    discover_p = conn_sub.add_parser(
        "discover", help="List a connection's discovery resources, or one resource's items")
    discover_p.add_argument("connection", metavar="CONNECTION",
                            help="the connection id, or service:account such as drive:datatalks")
    discover_p.add_argument("resource", nargs="?", default=None,
                            help="Resource to list, e.g. spreadsheets or channels (omit to list resources)")
    discover_p.add_argument("--param", action="append", nargs="*", default=[],
                            metavar="KEY=VALUE",
                            help="Discovery param, repeatable (e.g. --param folder=/invoices)")
    test_p = conn_sub.add_parser("test", help="Test a connection's stored tokens against its provider")
    test_p.add_argument("connection", nargs="+", metavar="CONNECTION", help=REF_HELP)
    conn_sub.add_parser("send-expiry-digest",
                        help="Render and email the connection expiry digest now (operator)")
    token_p = sub.add_parser("token", help="Short-lived provider access tokens")
    token_sub = token_p.add_subparsers(dest="command", required=True)
    exec_p = token_sub.add_parser(
        "exec", help="Run a command with a fresh token in its environment",
        description="Example: dapier token exec drive datatalks --agent A -- <command>")
    exec_p.add_argument("connection", nargs="+", metavar="CONNECTION", help=REF_HELP)
    exec_p.add_argument("--agent", required=True)
    write_p = token_sub.add_parser("write", help="Write a fresh token to a private file")
    write_p.add_argument("connection", nargs="+", metavar="CONNECTION", help=REF_HELP)
    write_p.add_argument("--agent", required=True)
    write_p.add_argument("--output", required=True)
    write_p.add_argument("--force", action="store_true")


def _connection_id(args, api_url, debug, agent=None):
    """The internal id the human reference on the command line names."""
    words = args.connection if isinstance(args.connection, list) else [args.connection]
    return commands.resolve_connection_id(api_url, words, agent=agent, debug=debug)


def run(args, api_url, debug, child=None):
    if args.group == "token":
            connection_id = _connection_id(args, api_url, debug, agent=args.agent)
            if args.command == "exec":
                return commands.token_exec(api_url, connection_id, args.agent,
                                           list(child or []), debug)
            if args.command == "write":
                return commands.token_write(api_url, connection_id, args.agent,
                                            args.output, args.force, debug)
            return 2
    if args.command == "list":
        return commands.connections_list(api_url, debug, limit=args.limit,
                                         next_token=args.next_token,
                                         list_all=args.list_all)
    if args.command == "create":
        return commands.connections_create(
            api_url, args.connection_id, args.provider, args.scopes,
            root_path=args.root_path, kind=args.kind, debug=debug,
        )
    if args.command == "import":
        return commands.connections_import(
            api_url, args.connection_id, args.provider, args.client_id,
            args.client_secret_file, args.authorized_user_file,
            expected_account_id=args.expected_account, scopes=args.scopes, debug=debug,
            token_path=args.token_file, root_path=args.root_path,
            signing_secret_path=args.signing_secret_file,
        )
    if args.command == "send-expiry-digest":
        return commands.connections_send_expiry_digest(api_url, debug)
    agent = getattr(args, "agent", None)
    connection_id = _connection_id(args, api_url, debug, agent=agent)
    if args.command == "show":
        return commands.connections_show(api_url, connection_id, args.agent, debug)
    if args.command == "connect":
        return commands.connections_connect(api_url, connection_id, args.agent, args.timeout, debug)
    if args.command == "edit":
        root_path = "" if args.clear_root_path else args.root_path
        return commands.connections_edit(
            api_url, connection_id,
            scopes=args.scopes, root_path=root_path, debug=debug,
        )
    if args.command == "revoke":
        return commands.connections_revoke(api_url, connection_id, debug)
    if args.command == "delete":
        return commands.connections_delete(api_url, connection_id,
                                           force=args.force, debug=debug)
    if args.command == "discover":
        return commands.connections_discover(
            api_url, connection_id, args.resource,
            params=[pair for group in args.param for pair in group], debug=debug)
    if args.command == "test":
        return commands.connections_test(api_url, connection_id, debug)
    return 2
