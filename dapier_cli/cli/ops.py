"""Single-command operator nouns: overview, usage, quota, errors, agent-tasks, workers, worker, catalog."""

import sys

from .. import commands

GROUPS = ("overview", "usage", "quota", "errors", "agent-tasks", "workers", "worker", "catalog",)


def register(sub):
    overview_p = sub.add_parser("overview", help="Operator overview: workflows, connections, credentials")
    overview_p.add_argument("--section", choices=("workflows", "activity", "usage", "connections", "credentials", "tokens", "emails"),
                            help="Fetch only this section for a faster response")
    usage_p = sub.add_parser("usage", help="Task usage rollup: tasks per workflow per month")
    usage_p.add_argument("--months", type=int, default=12,
                         help="How many months of rollup to show (default 12, max 24)")
    quota_p = sub.add_parser("quota", help="Monthly task quota: the budget action steps run against")
    quota_sub = quota_p.add_subparsers(dest="command")
    quota_sub.add_parser("show", help="Show the limit and this month's standing (default)")
    quota_set_p = quota_sub.add_parser("set", help="Set or clear the monthly task limit")
    quota_set_p.add_argument("limit",
                             help="A positive integer, or 'off' to remove the cap")
    errors_p = sub.add_parser("errors", help="Failed runs by workflow over the recent window")
    errors_p.add_argument("--days", type=int, default=7,
                          help="Window size in days (default 7, max 90)")
    errors_sub = errors_p.add_subparsers(dest="command")
    errors_sub.add_parser("send-digest",
                          help="Render and email the error digest now (operator)")
    agent_tasks_p = sub.add_parser(
        "agent-tasks", help="Host jobs the agent action enqueued (run by `dapier worker`)")
    agent_tasks_sub = agent_tasks_p.add_subparsers(dest="command", required=True)
    agent_tasks_list = agent_tasks_sub.add_parser("list", help="List host agent tasks, newest first")
    agent_tasks_list.add_argument("--limit", default=None,
                                  help="How many to show (default 50, max 200)")
    agent_tasks_list.add_argument("--status", default=None,
                                  help="Only rows with this status: queued, running, succeeded, failed, timed_out, interrupted")
    agent_tasks_show = agent_tasks_sub.add_parser("show", help="Show one agent task and its recorded logs")
    agent_tasks_show.add_argument("task_id")
    workers_p = sub.add_parser(
        "workers", help="Host workers (`dapier worker` processes) that run agent tasks")
    workers_sub = workers_p.add_subparsers(dest="command", required=True)
    workers_sub.add_parser("list", help="List host workers, most recently seen first")
    worker_p = sub.add_parser(
        "worker",
        help="Run headless host jobs over HTTPS until interrupted.")
    worker_p.add_argument("--token-file", default=None,
                          help="Owner-only Dapier host-worker token file")
    worker_p.add_argument("--workspace-root", default=None,
                          help="Host job root directory (default ~/dapier-ws)")
    worker_p.add_argument("--max-runtime", type=int, default=3600,
                          help="Maximum seconds per headless job")
    worker_p.add_argument("--once", action="store_true", help="Poll once and exit")
    catalog_p = sub.add_parser("catalog", help="Show the action and trigger catalog (GET /api/catalog)")
    catalog_p.add_argument("--json", action="store_true", help="Print the raw catalog JSON")


def run(args, api_url, debug, child=None):
    if args.group == "overview":
        return commands.overview(api_url, debug, section=args.section)
    if args.group == "usage":
        return commands.usage(api_url, debug, months=args.months)
    if args.group == "quota":
        command = getattr(args, "command", None) or "show"
        if command == "set" and not getattr(args, "limit", None):
            print("Error: quota set needs a limit: a positive integer or 'off'",
                  file=sys.stderr)
            return 2
        return commands.quota(api_url, debug, command=command,
                              limit=getattr(args, "limit", None))
    if args.group == "errors":
        if getattr(args, "command", None) == "send-digest":
            return commands.errors_send_digest(api_url, debug)
        return commands.errors_summary(api_url, debug, days=args.days)
    if args.group == "agent-tasks":
        if args.command == "show":
            return commands.agent_tasks_show(api_url, args.task_id, debug)
        return commands.agent_tasks_list(api_url, debug,
                                         limit=getattr(args, "limit", None),
                                         status=getattr(args, "status", None))
    if args.group == "workers":
        return commands.workers_list(api_url, debug)
    if args.group == "worker":
        return commands.worker_run(api_url, token_file=args.token_file,
                                   workspace_root=args.workspace_root,
                                   max_runtime=args.max_runtime, once=args.once)
    if args.group == "catalog":
        return commands.catalog_show(api_url, debug, as_json=args.json)
    return 2
