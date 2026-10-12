"""The `dapier emails` noun: inbound addresses and per-address sender policy."""

from .. import commands
from .runs import OUTCOME_HELP

GROUPS = ("emails",)


def register(sub):
    emails_p = sub.add_parser("emails", help="Email addresses that start a flow")
    emails_sub = emails_p.add_subparsers(dest="command", required=True)
    emails_sub.add_parser("list", help="List email addresses")
    emails_show = emails_sub.add_parser("show", help="Show one email address")
    emails_show.add_argument("name")
    emails_received = emails_sub.add_parser(
        "received", help="Recent received emails and what happened to each (the Emails page list)")
    emails_received.add_argument("--outcome", help=OUTCOME_HELP)
    emails_received.add_argument("--limit", type=int, default=25)
    emails_received.add_argument("--next", dest="next_token",
                                 help="Page token from the previous call's `next page:` footer")
    emails_from = emails_sub.add_parser(
        "from", help="Per-address sender policy, with a default for addresses that have none")
    emails_from_sub = emails_from.add_subparsers(dest="from_command", required=True)
    emails_from_list = emails_from_sub.add_parser(
        "list", help="Show the default senders and per-address overrides")
    emails_from_list.add_argument(
        "--route", help="One inbound address (local-part, or newsletter@…)")
    emails_from_add = emails_from_sub.add_parser("add", help="Allow one sender address")
    emails_from_add.add_argument("address")
    emails_from_add.add_argument(
        "--route", help="Apply to this inbound address instead of the default")
    emails_from_rm = emails_from_sub.add_parser("remove", help="Drop one sender address")
    emails_from_rm.add_argument("address")
    emails_from_rm.add_argument(
        "--route", help="Apply to this inbound address instead of the default")
    emails_from_any = emails_from_sub.add_parser(
        "anyone", help="Accept mail from anyone to this address")
    emails_from_any.add_argument("name", help="Inbound address local-part")
    emails_from_allow = emails_from_sub.add_parser(
        "allowlist", help="Accept mail only from listed senders to this address")
    emails_from_allow.add_argument("name", help="Inbound address local-part")


def run(args, api_url, debug, child=None):
    if args.command == "list":
        return commands.triggers_list(api_url, debug)
    if args.command == "show":
        return commands.triggers_show(api_url, args.name, debug)
    if args.command == "received":
        return commands.inbox_list(api_url, connector="email", limit=args.limit,
                                   next_token=args.next_token, outcome=args.outcome,
                                   debug=debug)
    if args.command == "from":
        if args.from_command == "list":
            return commands.emails_from_list(api_url, debug, route=args.route)
        if args.from_command == "add":
            return commands.emails_from_add(api_url, args.address, debug, route=args.route)
        if args.from_command == "remove":
            return commands.emails_from_remove(api_url, args.address, debug, route=args.route)
        if args.from_command == "anyone":
            return commands.emails_from_mode(api_url, args.name, "anyone", debug)
        if args.from_command == "allowlist":
            return commands.emails_from_mode(api_url, args.name, "allowlist", debug)
    return 2
