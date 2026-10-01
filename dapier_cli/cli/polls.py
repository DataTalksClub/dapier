"""The `dapier polls` noun."""

from .. import commands

GROUPS = ("polls",)


def register(sub):
    poll_p = sub.add_parser("polls", help="API poll triggers: fetch on a schedule, one event per new item")
    poll_sub = poll_p.add_subparsers(dest="command", required=True)
    poll_sub.add_parser("list", help="List poll triggers")
    poll_save_p = poll_sub.add_parser("save", help="Create or update a poll trigger from a JSON file")
    poll_save_p.add_argument("file", help="Path to the poll trigger JSON, or - for stdin")
    poll_del_p = poll_sub.add_parser("delete", help="Delete a poll trigger and its rule")
    poll_del_p.add_argument("name")


def run(args, api_url, debug, child=None):
    if args.command == "list":
        return commands.polls_list(api_url, debug)
    if args.command == "save":
        return commands.polls_save(api_url, args.file, debug)
    if args.command == "delete":
        return commands.polls_delete(api_url, args.name, debug)
    return 2
