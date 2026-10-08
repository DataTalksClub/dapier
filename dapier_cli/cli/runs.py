"""The `dapier runs` and `dapier inbox` nouns (and `runs events`)."""

from .. import commands

GROUPS = ("runs", "inbox")

OUTCOME_HELP = ("Only events with this outcome: handled, failed, refused, unmatched, "
                "pending (comma-separate several)")


def register(sub):
    runs_p = sub.add_parser("runs", help="Workflow executions and incoming trigger events")
    runs_sub = runs_p.add_subparsers(dest="command", required=True)
    runs_list_p = runs_sub.add_parser("list", help="Recent runs, newest first")
    runs_list_p.add_argument("--limit", type=int, default=25)
    runs_list_p.add_argument("--workflow", help="Only runs of this workflow id")
    runs_list_p.add_argument("--status",
                             help="success | failed | error | problems (failed or error that "
                                  "still needs action) | resolved (failures something "
                                  "settled), or an exact status (completed, processing, "
                                  "filtered)")
    runs_list_p.add_argument("--resolved", dest="resolved", action="store_true",
                             default=None,
                             help="Only failures already fixed (the operator marked them, or a "
                                  "later run of the workflow completed)")
    runs_list_p.add_argument("--unresolved", dest="resolved", action="store_false",
                             help="Only failures that still need action (the default set)")
    runs_list_p.add_argument("--since",
                             help="Only runs started at or after this ISO date/datetime")
    runs_list_p.add_argument("--before",
                             help="Only runs started before this ISO date/datetime (exclusive)")
    runs_list_p.add_argument("--search",
                             help="Substring search over each run's recorded step data "
                                  "(inputs, outputs, errors) and ids")
    runs_list_p.add_argument("--next", dest="next_token",
                             help="Page token from the previous call's `next page:` footer")
    runs_show_p = runs_sub.add_parser("show", help="Show one run's step-by-step flow")
    runs_show_p.add_argument("run_id", help="Run ID from `dapier runs list` (or the console)")
    runs_replay_p = runs_sub.add_parser("replay", help="Re-run a past run by re-injecting its original trigger event")
    runs_replay_p.add_argument("run_id", help="Run ID from `dapier runs list` (or the console)")
    runs_replay_p.add_argument("--from-step", dest="from_step",
                               help="Replay from this top-level step id onward: earlier "
                                    "steps do not run again; their recorded outputs are reused")
    runs_resolve_p = runs_sub.add_parser(
        "resolve", help="Mark a failed run fixed: it stops counting as a problem")
    runs_resolve_p.add_argument("run_id", help="Run ID from `dapier runs list` (or the console)")
    runs_resolve_p.add_argument("--note",
                                help="Why it is handled (kept on the resolve call's audit entry)")
    runs_replay_failed_p = runs_sub.add_parser(
        "replay-failed",
        help="Re-run the unresolved failed runs of one workflow (a completed rerun "
             "resolves them; already-fixed ones are left alone)")
    runs_replay_failed_p.add_argument("workflow_id", help="Workflow whose failed runs to replay")
    runs_cancel_p = runs_sub.add_parser(
        "cancel", help="Cancel a suspended run: it closes out cancelled and will not resume")
    runs_cancel_p.add_argument("run_id", help="Run ID from `dapier runs list` (or the console)")
    runs_export_p = runs_sub.add_parser("export", help="Export run history as CSV")
    runs_export_p.add_argument("--workflow", help="Only runs of this workflow id")
    runs_export_p.add_argument("--status",
                               help="success | failed | error | problems (failed or error that "
                                    "still needs action) | resolved (failures something "
                                    "settled), or an exact status (completed, processing, "
                                    "filtered)")
    runs_export_p.add_argument("--resolved", dest="resolved", action="store_true",
                               default=None,
                               help="Only failures already fixed (the operator marked them, or a "
                                    "later run of the workflow completed)")
    runs_export_p.add_argument("--unresolved", dest="resolved", action="store_false",
                               help="Only failures that still need action (the default set)")
    runs_export_p.add_argument("--since",
                               help="Only runs started at or after this ISO date/datetime")
    runs_export_p.add_argument("--before",
                               help="Only runs started before this ISO date/datetime (exclusive)")
    runs_export_p.add_argument("--search",
                               help="Substring search over each run's recorded step data "
                                    "(inputs, outputs, errors) and ids")
    runs_export_p.add_argument("--max-rows", type=int,
                               help="Cap the export (default 1000, max 5000)")
    runs_export_p.add_argument("--out",
                               help="Write the CSV here (default: the suggested filename)")
    inbox_p = sub.add_parser("inbox", help="Trigger inbox: every inbound trigger event, matched or not")
    inbox_sub = inbox_p.add_subparsers(dest="command", required=True)
    inbox_list_p = inbox_sub.add_parser("list", help="Recent inbox events, newest first")
    inbox_list_p.add_argument("--connector", help="Only events from this connector (e.g. webhook, telegram)")
    inbox_list_p.add_argument("--limit", type=int, default=25)
    inbox_list_p.add_argument("--outcome", help=OUTCOME_HELP)
    inbox_list_p.add_argument("--next", dest="next_token",
                              help="Page token from the previous call's `next page:` footer")
    inbox_show_p = inbox_sub.add_parser("show", help="Show one inbox event's stored envelope")
    inbox_show_p.add_argument("inbox_id", help="Inbox event ID from `dapier inbox list`")
    inbox_replay_p = inbox_sub.add_parser("replay", help="Send an inbox event through the engine again (fresh event id)")
    inbox_replay_p.add_argument("inbox_id", help="Inbox event ID from `dapier inbox list`")

    events_p = runs_sub.add_parser("events", help="Incoming events, including those no workflow handled")
    inbox_sub = events_p.add_subparsers(dest="event_command", required=True)
    inbox_list_p = inbox_sub.add_parser("list", help="Recent inbox events, newest first")
    inbox_list_p.add_argument("--connector", help="Only events from this connector (e.g. webhook, telegram)")
    inbox_list_p.add_argument("--limit", type=int, default=25)
    inbox_list_p.add_argument("--outcome", help=OUTCOME_HELP)
    inbox_list_p.add_argument("--next", dest="next_token",
                              help="Page token from the previous call's `next page:` footer")
    inbox_show_p = inbox_sub.add_parser("show", help="Show one inbox event's stored envelope")
    inbox_show_p.add_argument("inbox_id", help="Inbox event ID from `dapier runs events list`")
    inbox_replay_p = inbox_sub.add_parser("replay", help="Send an inbox event through the engine again (fresh event id)")
    inbox_replay_p.add_argument("inbox_id", help="Inbox event ID from `dapier runs events list`")


def run(args, api_url, debug, child=None):
    if args.group == "inbox":
        return _inbox(args, api_url, debug)
    if args.command == "events":
        args.command = args.event_command
        return _inbox(args, api_url, debug)
    if args.command == "list":
        return commands.runs_list(api_url, args.limit, workflow=args.workflow,
                                  status=args.status, since=args.since,
                                  before=args.before, query=args.search,
                                  next_token=args.next_token,
                                  resolved=args.resolved, debug=debug)
    if args.command == "show":
        return commands.runs_show(api_url, args.run_id, debug)
    if args.command == "replay":
        return commands.runs_replay(api_url, args.run_id, from_step=args.from_step, debug=debug)
    if args.command == "resolve":
        return commands.runs_resolve(api_url, args.run_id, note=args.note, debug=debug)
    if args.command == "replay-failed":
        return commands.runs_replay_failed(api_url, args.workflow_id, debug)
    if args.command == "cancel":
        return commands.runs_cancel(api_url, args.run_id, debug)
    if args.command == "export":
        return commands.runs_export(api_url, out=args.out, max_rows=args.max_rows,
                                    workflow=args.workflow, status=args.status,
                                    since=args.since, before=args.before,
                                    query=args.search, resolved=args.resolved,
                                    debug=debug)
    return 2


def _inbox(args, api_url, debug):
    if args.command == "list":
        return commands.inbox_list(api_url, connector=args.connector,
                                   limit=args.limit, next_token=args.next_token,
                                   outcome=args.outcome, debug=debug)
    if args.command == "show":
        return commands.inbox_show(api_url, args.inbox_id, debug)
    if args.command == "replay":
        return commands.inbox_replay(api_url, args.inbox_id, debug)
    return 2
