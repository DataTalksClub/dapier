"""The `dapier slack` noun: find users, open DMs, export history, post (with images)."""

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

    post = sub_p.add_parser(
        "post",
        help="Post a message to a Slack channel or thread (default connection: slack = Au-Tomator bot)",
    )
    post.add_argument("--channel", required=True,
                      help="Channel name (e.g. announcements or #announcements) or id (e.g. C01BQC114P2)")
    text_group = post.add_mutually_exclusive_group(required=True)
    text_group.add_argument("--text-file",
                            help="Path to UTF-8 text file (or '-' for stdin)")
    text_group.add_argument("--text", help="Inline message text")
    post.add_argument("--thread-ts", "--thread", dest="thread_ts", default=None,
                      help="Thread timestamp to reply in")
    post.add_argument("--connection", default="slack",
                      help="Connection id (default slack = Au-Tomator bot)")
    post.add_argument("--no-unfurl", action="store_true",
                      help="Disable link and media previews")
    post.add_argument("--dry-run", action="store_true",
                      help="Resolve channel and verify bot/channel access (and token scopes) "
                           "without posting")
    post.add_argument("--image", default=None, metavar="PATH",
                      help="Attach a local image (files.uploadV2); the text becomes its "
                           "comment. Needs the files:write scope")

    attach = sub_p.add_parser(
        "attach-image",
        help="Attach an image to an existing message: thread reply, or --replace "
             "(repost text+image, delete the old bot message)",
        description=(
            "Slack cannot add a file to a message that is already posted. By default "
            "the image is posted as a reply in the message's thread. With --replace "
            "the message (it must be this bot's own, without files or thread replies) "
            "is reposted as text+image in the same place and the old message is "
            "deleted; the new ts is printed. The repost happens before the delete, so "
            "a failure never loses the text."),
    )
    attach.add_argument("--channel", required=True, help="Channel id or name")
    attach.add_argument("--ts", required=True, help="ts of the existing message")
    attach.add_argument("--image", required=True, metavar="PATH", help="Local image file")
    attach.add_argument("--replace", action="store_true",
                        help="Repost text+image and delete the old message (returns new ts)")
    attach_text = attach.add_mutually_exclusive_group()
    attach_text.add_argument("--text", default=None,
                             help="Comment for the reply, or replacement text with --replace "
                                  "(default: the original text)")
    attach_text.add_argument("--text-file", default=None,
                             help="Path to UTF-8 text file (or '-' for stdin)")
    attach.add_argument("--connection", default="slack",
                        help="Connection id (default slack = Au-Tomator bot)")


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
    if args.command == "post":
        return commands.slack_post(
            api_url,
            channel=args.channel,
            text_file=args.text_file,
            text=args.text,
            connection_id=args.connection,
            thread_ts=args.thread_ts,
            unfurl_links=not args.no_unfurl,
            unfurl_media=not args.no_unfurl,
            dry_run=args.dry_run,
            image=args.image,
            debug=debug,
        )
    if args.command == "attach-image":
        return commands.slack_attach_image(
            api_url,
            channel=args.channel,
            ts=args.ts,
            image=args.image,
            replace=args.replace,
            text_file=args.text_file,
            text=args.text,
            connection_id=args.connection,
            debug=debug,
        )
    return 2
