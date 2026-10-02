"""The `dapier emails` noun: inbound addresses and the sender allow-list."""

from .. import commands

GROUPS = ("emails",)


def register(sub):
    emails_p = sub.add_parser("emails", help="Email addresses that start a flow")
    emails_sub = emails_p.add_subparsers(dest="command", required=True)
    emails_sub.add_parser("list", help="List email addresses")
    emails_show = emails_sub.add_parser("show", help="Show one email address")
    emails_show.add_argument("name")
    emails_from = emails_sub.add_parser("from", help="The shared sender allow-list for every inbound email")
    emails_from_sub = emails_from.add_subparsers(dest="from_command", required=True)
    emails_from_sub.add_parser("list", help="List allowed senders")
    emails_from_add = emails_from_sub.add_parser("add", help="Allow one sender address")
    emails_from_add.add_argument("address")
    emails_from_rm = emails_from_sub.add_parser("remove", help="Drop one sender address")
    emails_from_rm.add_argument("address")


def run(args, api_url, debug, child=None):
    if args.command == "list":
        return commands.triggers_list(api_url, debug)
    if args.command == "show":
        return commands.triggers_show(api_url, args.name, debug)
    if args.command == "from":
        if args.from_command == "list":
            return commands.emails_from_list(api_url, debug)
        if args.from_command == "add":
            return commands.emails_from_add(api_url, args.address, debug)
        if args.from_command == "remove":
            return commands.emails_from_remove(api_url, args.address, debug)
    return 2
