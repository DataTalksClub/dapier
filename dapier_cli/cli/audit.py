"""The `dapier audit` noun."""

from .. import commands

GROUPS = ("audit",)


def register(sub):
    def add_audit_filters(parser):
        parser.add_argument("--connection", help="Only events for this connection id")
        parser.add_argument("--action",
                            help="Exact action, or an action family like 'workflow.'")
        parser.add_argument("--actor", help="Only events recorded for this actor subject")
        parser.add_argument("--agent", help="Only events recorded for this agent")
        parser.add_argument("--outcome",
                            help="Only events with this outcome (ok, error, denied-no-grant, ...)")
        parser.add_argument("--since", help="Only events at or after this ISO date/datetime")
        parser.add_argument("--before",
                            help="Only events before this ISO date/datetime (exclusive)")
        parser.add_argument("--query",
                            help="Substring search over action, connection, actor, agent, and error text")

    audit_p = sub.add_parser("audit",
                             help="Operator audit trail: who changed what, and when")
    audit_sub = audit_p.add_subparsers(dest="command", required=True)
    audit_list_p = audit_sub.add_parser("list", help="Recent audit events, newest first")
    audit_list_p.add_argument("--limit", type=int, default=50)
    add_audit_filters(audit_list_p)
    audit_list_p.add_argument("--next", dest="next_token",
                              help="Page token from the previous call's `next page:` footer")
    audit_export_p = audit_sub.add_parser("export", help="Export the audit trail as CSV")
    add_audit_filters(audit_export_p)
    audit_export_p.add_argument("--max-rows", type=int,
                                help="Cap on exported rows (default 1000, max 5000)")
    audit_export_p.add_argument("--out", help="Write the CSV here (default: the suggested filename)")


def run(args, api_url, debug, child=None):
    if args.command == "list":
        return commands.audit_list(api_url, args.limit, connection=args.connection,
                                   action=args.action, actor=args.actor,
                                   agent=args.agent, outcome=args.outcome,
                                   since=args.since, before=args.before,
                                   query=args.query, next_token=args.next_token,
                                   debug=debug)
    if args.command == "export":
        return commands.audit_export(api_url, out=args.out, max_rows=args.max_rows,
                                     connection=args.connection, action=args.action,
                                     actor=args.actor, agent=args.agent,
                                     outcome=args.outcome, since=args.since,
                                     before=args.before, query=args.query, debug=debug)
    return 2
