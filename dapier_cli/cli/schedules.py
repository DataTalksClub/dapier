"""The `dapier schedules` noun."""

from .. import commands

GROUPS = ("schedules",)


def register(sub):
    sched_p = sub.add_parser("schedules", help="Cron and rate schedule triggers (EventBridge rules)")
    sched_sub = sched_p.add_subparsers(dest="command", required=True)
    sched_sub.add_parser("list", help="List schedule triggers")
    sched_save_p = sched_sub.add_parser("save", help="Create or update a schedule trigger from a JSON file")
    sched_save_p.add_argument("file", help="Path to the schedule JSON, or - for stdin")
    sched_del_p = sched_sub.add_parser("delete", help="Delete a schedule trigger and its rule")
    sched_del_p.add_argument("name")


def run(args, api_url, debug, child=None):
    if args.command == "list":
        return commands.schedules_list(api_url, debug)
    if args.command == "save":
        return commands.schedules_save(api_url, args.file, debug)
    if args.command == "delete":
        return commands.schedules_delete(api_url, args.name, debug)
    return 2
