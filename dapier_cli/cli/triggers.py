"""The `dapier triggers` noun (email inventory and trigger samples)."""

from .. import commands

GROUPS = ("triggers",)


def register(sub):
    trig_p = sub.add_parser(
        "triggers",
        help="Moved to dapier emails (list, show) and dapier workflows sample",
        description="Moved to dapier emails (list, show) and dapier workflows sample.")
    trig_sub = trig_p.add_subparsers(dest="command", required=True)
    trig_sub.add_parser("list", help="List the email addresses workflows answer")
    trig_sample_p = trig_sub.add_parser(
        "sample", help="Pull a sample event for a trigger connector, or a workflow's last trigger input")
    trig_sample_p.add_argument("connector", nargs="?", default=None,
                               help="e.g. custom, dataops, dropbox, email, mailchimp, poll, "
                                    "renderer, schedule, telegram, webhook, youtube, zoom")
    trig_sample_p.add_argument("--workflow", default=None,
                               help="Workflow id: print its newest run's recorded "
                                    "trigger input (else the connector's sample) "
                                    "with the {trigger.*} fields it offers")
    trig_sample_p.add_argument("--event", default=None,
                               help="Event name override; for poll, the trigger's name")
    trig_sample_p.add_argument("--connection-id", default=None,
                               help="Prefer this connected account for live pulls")
    trig_sample_p.add_argument("--limit", type=int, default=None,
                               help="Max options when kind is options (1-25)")
    trig_sample_p.add_argument("--resource", default=None,
                               help="Options listing instead of a sample (e.g. slack.channels)")
    trig_show_p = trig_sub.add_parser("show", help="Show one email address")
    trig_show_p.add_argument("name")


def run(args, api_url, debug, child=None):
    if args.command == "list":
        return commands.triggers_list(api_url, debug)
    if args.command == "show":
        return commands.triggers_show(api_url, args.name, debug)
    if args.command == "sample":
        if args.workflow:
            return commands.triggers_workflow_sample(api_url, args.workflow, debug=debug)
        if not args.connector:
            print("Error: give a connector (e.g. email) or --workflow <workflow_id>")
            return 2
        return commands.triggers_sample(api_url, args.connector, event=args.event,
                                        connection_id=args.connection_id,
                                        limit=args.limit, resource=args.resource, debug=debug)
    return 2
