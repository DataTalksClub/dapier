"""The `dapier schedules` noun."""

from .. import commands

GROUPS = ("schedules",)


def register(sub):
    sched_p = sub.add_parser("schedules", help="Cron and rate schedule triggers (EventBridge rules)")
    sched_sub = sched_p.add_subparsers(dest="command", required=True)
    sched_sub.add_parser("list", help="List schedule triggers with their health and next fire")
    show_p = sched_sub.add_parser(
        "show", help="One schedule: plain-language timing, next fires, recent fires and their runs")
    show_p.add_argument("name")
    upcoming_p = sched_sub.add_parser("upcoming", help="Fires coming up across all enabled schedules")
    window = upcoming_p.add_mutually_exclusive_group()
    window.add_argument("--hours", type=int, help="Window in hours (default 24, max 168)")
    window.add_argument("--days", type=int, help="Window in days (max 7)")
    run_p = sched_sub.add_parser("run", help="Fire a schedule once now (Run now)")
    run_p.add_argument("name")
    pause_p = sched_sub.add_parser("pause", help="Pause a schedule (disables its rule)")
    pause_p.add_argument("name")
    resume_p = sched_sub.add_parser("resume", help="Resume a paused schedule")
    resume_p.add_argument("name")
    sched_save_p = sched_sub.add_parser("save", help="Create or update a schedule trigger from a JSON file")
    sched_save_p.add_argument("file", help="Path to the schedule JSON, or - for stdin")
    sched_del_p = sched_sub.add_parser("delete", help="Delete a schedule trigger and its rule")
    sched_del_p.add_argument("name")


def run(args, api_url, debug, child=None):
    if args.command == "list":
        return commands.schedules_list(api_url, debug)
    if args.command == "show":
        return commands.schedules_show(api_url, args.name, debug)
    if args.command == "upcoming":
        hours = args.days * 24 if args.days else (args.hours or 24)
        return commands.schedules_upcoming(api_url, hours, debug)
    if args.command == "run":
        return commands.schedules_run(api_url, args.name, debug)
    if args.command == "pause":
        return commands.schedules_pause(api_url, args.name, debug)
    if args.command == "resume":
        return commands.schedules_resume(api_url, args.name, debug)
    if args.command == "save":
        return commands.schedules_save(api_url, args.file, debug)
    if args.command == "delete":
        return commands.schedules_delete(api_url, args.name, debug)
    return 2
