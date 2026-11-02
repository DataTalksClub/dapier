"""Single-command operator nouns: overview, usage, quota, errors, catalog."""

import sys

from .. import commands

GROUPS = ("costs", "overview", "usage", "quota", "errors", "catalog",
          "store-check",)


def register(sub):
    overview_p = sub.add_parser("overview", help="Operator overview: workflows, connections, credentials")
    overview_p.add_argument("--section", choices=("workflows", "activity", "usage", "connections", "credentials", "tokens", "emails"),
                            help="Fetch only this section for a faster response")
    costs_p = sub.add_parser("costs", help="Recorded paid API charges, estimates and unpriced usage")
    costs_p.add_argument("--months", type=int, default=1, help="Months to show (1-24, default 1)")
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
    catalog_p = sub.add_parser("catalog", help="Show the action and trigger catalog (GET /api/catalog)")
    catalog_p.add_argument("--json", action="store_true", help="Print the raw catalog JSON")
    store_check_p = sub.add_parser(
        "store-check",
        help="Count the legacy-shaped stored rows the compat fallbacks serve (operator)")


def run(args, api_url, debug, child=None):
    if args.group == "overview":
        return commands.overview(api_url, debug, section=args.section)
    if args.group == "costs":
        return commands.costs(api_url, debug, months=args.months)
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
    if args.group == "catalog":
        return commands.catalog_show(api_url, debug, as_json=args.json)
    if args.group == "store-check":
        return commands.store_check(api_url, debug)
    return 2
