"""The `dapier hooks` and `dapier webhooks` nouns (same commands)."""

from .. import commands

GROUPS = ("hooks", "webhooks",)


def _activity_commands(group_sub):
    """The delivery-log commands both nouns carry (the console Hooks tab's
    activity view and Send test request)."""
    deliveries = group_sub.add_parser(
        "deliveries", help="Recent webhook deliveries with their outcome, newest first")
    deliveries.add_argument("name", nargs="?", default=None, help="Only this hook's deliveries")
    deliveries.add_argument("--limit", type=int, default=25)
    deliveries.add_argument("--next", dest="next_token", default=None,
                            help="Page token printed by the previous page")
    delivery = group_sub.add_parser(
        "delivery", help="One delivery: request headers, payload, run outcome")
    delivery.add_argument("delivery_id")
    test = group_sub.add_parser(
        "test", help="Send a test request to a webhook through its real intake "
                     "(matched workflows run)")
    test.add_argument("name")
    test.add_argument("--data", default=None,
                      help="JSON payload file to send (- for stdin); default: a small sample")
    replay = group_sub.add_parser(
        "replay", help="Re-send a recorded delivery through the engine (fresh event id)")
    replay.add_argument("delivery_id")


def register(sub):
    hook_p = sub.add_parser(
        "hooks", help="Moved to dapier webhooks",
        description="Moved to dapier webhooks.")
    hook_sub = hook_p.add_subparsers(dest="command", required=True)
    hook_list_p = hook_sub.add_parser("list", help="List hook triggers")
    hook_list_p.add_argument("--kind", default=None,
                             choices=["webhook", "telegram", "mailchimp", "youtube"])
    hook_show_p = hook_sub.add_parser("show", help="Show one hook trigger, including its token")
    hook_show_p.add_argument("name")
    hook_save_p = hook_sub.add_parser("save", help="Create or update a hook trigger from a JSON file")
    hook_save_p.add_argument("file", help="Path to the hook JSON, or - for stdin")
    hook_save_p.add_argument("--sync-response", action="store_true",
                             help="Webhook only: answer each delivery by running the flow "
                                  "inline and replying with the outcome (response.mode sync) "
                                  "instead of the 202 ack; must finish inside ~30s")
    hook_del_p = hook_sub.add_parser("delete", help="Delete a hook trigger")
    hook_del_p.add_argument("name")
    hook_del_p.add_argument("--kind", default=None,
                            choices=["webhook", "telegram", "mailchimp", "youtube"])
    _activity_commands(hook_sub)
    webhooks_p = sub.add_parser(
        "webhooks", help="Webhook, Telegram, Mailchimp, and YouTube callbacks that start a flow")
    webhooks_sub = webhooks_p.add_subparsers(dest="command", required=True)
    webhooks_list = webhooks_sub.add_parser("list", help="List webhooks")
    webhooks_list.add_argument("--kind", default=None,
                               choices=["webhook", "telegram", "mailchimp", "youtube"])
    webhooks_show = webhooks_sub.add_parser("show", help="Show one webhook, including its token")
    webhooks_show.add_argument("name")
    webhooks_save = webhooks_sub.add_parser("save", help="Create or update a webhook from a JSON file")
    webhooks_save.add_argument("file", help="Path to the webhook JSON, or - for stdin")
    webhooks_save.add_argument("--sync-response", action="store_true",
                               help="Webhook only: answer each delivery by running the flow "
                                    "inline and replying with the outcome (response.mode sync) "
                                    "instead of the 202 ack; must finish inside ~30s")
    webhooks_del = webhooks_sub.add_parser("delete", help="Delete a webhook")
    webhooks_del.add_argument("name")
    webhooks_del.add_argument("--kind", default=None,
                              choices=["webhook", "telegram", "mailchimp", "youtube"])
    _activity_commands(webhooks_sub)


def run(args, api_url, debug, child=None):
    if args.command == "list":
        return commands.hooks_list(api_url, kind=args.kind, debug=debug)
    if args.command == "show":
        return commands.hooks_show(api_url, args.name, debug)
    if args.command == "save":
        return commands.hooks_save(api_url, args.file, debug=debug,
                                   sync_response=getattr(args, "sync_response", False))
    if args.command == "delete":
        return commands.hooks_delete(api_url, args.name, kind=args.kind, debug=debug)
    if args.command == "deliveries":
        return commands.hooks_deliveries(api_url, args.name, limit=args.limit,
                                         next_token=args.next_token, debug=debug)
    if args.command == "delivery":
        return commands.hooks_delivery(api_url, args.delivery_id, debug=debug)
    if args.command == "test":
        return commands.hooks_test(api_url, args.name, data_path=args.data, debug=debug)
    if args.command == "replay":
        return commands.inbox_replay(api_url, args.delivery_id, debug=debug)
    return 2
