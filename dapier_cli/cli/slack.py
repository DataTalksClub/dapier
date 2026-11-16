"""The `dapier slack` noun: find users, open DMs, export history."""

from .. import commands

GROUPS = ("slack",)
DEFAULT_CONNECTION_ID = "slack-alexey"


def register(sub):
    p = sub.add_parser(
        "slack",
        help="Slack user lookup and DM/history export (uses stored connection server-side)")
    sub_p = p.add_subparsers(dest="command", required=True)

    find_user = sub_p.add_parser("find-user", help="Find workspace users by name, handle, email, or id")
    find_user.add_argument("query")
    find_user.add_argument("--connection", default=DEFAULT_CONNECTION_ID,
                           help="Connection id (default slack-alexey)")
    find_user.add_argument("--limit", type=int, default=50)

    open_dm = sub_p.add_parser("open-dm", help="Open (or resolve) a DM channel with a user")
    open_dm.add_argument("user", help="User id (U…) or a find-user query")
    open_dm.add_argument("--connection", default=DEFAULT_CONNECTION_ID)

    history = sub_p.add_parser("history", help="Dump conversations.history for a channel/DM")
    history.add_argument("--channel", required=True)
    history.add_argument("--connection", default=DEFAULT_CONNECTION_ID)
    history.add_argument("--limit", type=int, default=100)
    history.add_argument("--cursor", default=None)
    history.add_argument("--oldest", default=None)
    history.add_argument("--latest", default=None)

    replies = sub_p.add_parser("replies", help="Read the replies of one thread (conversations.replies)")
    replies.add_argument("--channel", required=True)
    replies.add_argument("--thread-ts", required=True, help="ts of the thread root message")
    replies.add_argument("--connection", default=DEFAULT_CONNECTION_ID)
    replies.add_argument("--limit", type=int, default=200)
    replies.add_argument("--cursor", default=None)

    channels = sub_p.add_parser("channels", help="List channels, paged with --cursor")
    channels.add_argument("--connection", default=DEFAULT_CONNECTION_ID)
    channels.add_argument("--limit", type=int, default=200)
    channels.add_argument("--cursor", default=None)

    search = sub_p.add_parser("search", help="Search messages (needs search:read user scope)")
    search.add_argument("query")
    search.add_argument("--connection", default=DEFAULT_CONNECTION_ID)
    search.add_argument("--count", type=int, default=20)
    search.add_argument("--page", type=int, default=1)

    export = sub_p.add_parser(
        "dm-export",
        help="Find user, open DM, dump history (JSON)")
    export.add_argument("user", help="User id (U…) or a find-user query")
    export.add_argument("--connection", default=DEFAULT_CONNECTION_ID)
    export.add_argument("--limit", type=int, default=200)
    export.add_argument("--cursor", default=None)


def run(args, api_url, debug, child=None):
    if args.command == "find-user":
        return commands.slack_find_user(
            api_url, args.query, connection_id=args.connection,
            limit=args.limit, debug=debug)
    if args.command == "open-dm":
        return commands.slack_open_dm(
            api_url, args.user, connection_id=args.connection, debug=debug)
    if args.command == "history":
        return commands.slack_history(
            api_url, channel=args.channel, connection_id=args.connection,
            limit=args.limit, cursor=args.cursor, oldest=args.oldest,
            latest=args.latest, debug=debug)
    if args.command == "replies":
        return commands.slack_replies(
            api_url, args.channel, args.thread_ts, connection_id=args.connection,
            limit=args.limit, cursor=args.cursor, debug=debug)
    if args.command == "channels":
        return commands.slack_channels(
            api_url, connection_id=args.connection, limit=args.limit,
            cursor=args.cursor, debug=debug)
    if args.command == "search":
        return commands.slack_search(
            api_url, args.query, connection_id=args.connection, count=args.count,
            page=args.page, debug=debug)
    if args.command == "dm-export":
        return commands.slack_dm_export(
            api_url, args.user, connection_id=args.connection,
            limit=args.limit, cursor=args.cursor, debug=debug)
    return 2
