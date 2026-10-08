"""The `dapier polls` noun."""

from .. import commands

GROUPS = ("polls",)


def register(sub):
    poll_p = sub.add_parser("polls", help="API poll triggers: fetch on a schedule, one event per new item")
    poll_sub = poll_p.add_subparsers(dest="command", required=True)
    poll_sub.add_parser("list", help="List poll triggers with their health")
    poll_show_p = poll_sub.add_parser("show", help="One poll: health, last checks, workflows it starts")
    poll_show_p.add_argument("name")
    poll_act_p = poll_sub.add_parser("activity", help="Items polls picked up and the runs they started")
    poll_act_p.add_argument("name", nargs="?", help="Only this poll")
    poll_act_p.add_argument("--limit", type=int, default=25)
    poll_check_p = poll_sub.add_parser("check", help="Poll now: queue one check outside the schedule")
    poll_check_p.add_argument("name")
    poll_pause_p = poll_sub.add_parser("pause", help="Stop checking until resumed (position kept)")
    poll_pause_p.add_argument("name")
    poll_resume_p = poll_sub.add_parser("resume", help="Check on the schedule again")
    poll_resume_p.add_argument("name")
    poll_reset_p = poll_sub.add_parser(
        "reset", help="Move the poll's position: skip what is waiting, or re-read from a date")
    poll_reset_p.add_argument("name")
    reset_to = poll_reset_p.add_mutually_exclusive_group(required=True)
    reset_to.add_argument("--now", action="store_true", help="Skip every item waiting now")
    reset_to.add_argument("--from", dest="date", metavar="DATE",
                          help="Pick up items newer than this ISO date (timestamp-positioned polls)")
    poll_save_p = poll_sub.add_parser("save", help="Create or update a poll trigger from a JSON file")
    poll_save_p.add_argument("file", help="Path to the poll trigger JSON, or - for stdin")
    poll_del_p = poll_sub.add_parser("delete", help="Delete a poll trigger and its rule")
    poll_del_p.add_argument("name")


def run(args, api_url, debug, child=None):
    if args.command == "list":
        return commands.polls_list(api_url, debug)
    if args.command == "show":
        return commands.polls_show(api_url, args.name, debug)
    if args.command == "activity":
        return commands.polls_activity(api_url, args.name, args.limit, debug)
    if args.command == "check":
        return commands.polls_check(api_url, args.name, debug)
    if args.command in ("pause", "resume"):
        return commands.polls_set_enabled(api_url, args.name, args.command == "resume", debug)
    if args.command == "reset":
        return commands.polls_reset(api_url, args.name, None if args.now else args.date, debug)
    if args.command == "save":
        return commands.polls_save(api_url, args.file, debug)
    if args.command == "delete":
        return commands.polls_delete(api_url, args.name, debug)
    return 2
