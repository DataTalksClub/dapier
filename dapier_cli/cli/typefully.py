"""The `dapier typefully` noun: social sets, drafts, queue, unscheduled create."""

from .. import commands

GROUPS = ("typefully",)
DEFAULT_SOCIAL_SET_ID = 182343


def register(sub):
    p = sub.add_parser("typefully", help="Typefully drafts and queue (uses stored credential server-side)")
    sub_p = p.add_subparsers(dest="command", required=True)
    sub_p.add_parser("social-sets", help="List social sets available to the saved API key")
    drafts = sub_p.add_parser("drafts", help="List drafts for a social set (JSON)")
    drafts.add_argument("--social-set-id", type=int, default=DEFAULT_SOCIAL_SET_ID)
    drafts.add_argument("--status", help="draft|scheduled|planned|published|error|publishing")
    draft = sub_p.add_parser("draft", help="Get one draft by id (JSON)")
    draft.add_argument("draft_id", type=int)
    draft.add_argument("--social-set-id", type=int, default=DEFAULT_SOCIAL_SET_ID)
    queue = sub_p.add_parser("queue", help="Queue window for a date range (JSON)")
    queue.add_argument("--start-date", required=True, help="YYYY-MM-DD")
    queue.add_argument("--end-date", required=True, help="YYYY-MM-DD")
    queue.add_argument("--social-set-id", type=int, default=DEFAULT_SOCIAL_SET_ID)
    create = sub_p.add_parser(
        "create-draft",
        help="Create an unscheduled draft from JSON (no publish_at/plan_at)")
    create.add_argument("--file", required=True, help="JSON file, or - for stdin")
    create.add_argument("--social-set-id", type=int, default=DEFAULT_SOCIAL_SET_ID)


def run(args, api_url, debug, child=None):
    if args.command == "social-sets":
        return commands.typefully_social_sets(api_url, debug=debug)
    if args.command == "drafts":
        return commands.typefully_drafts(
            api_url, social_set_id=args.social_set_id, status=args.status, debug=debug)
    if args.command == "draft":
        return commands.typefully_draft(
            api_url, args.draft_id, social_set_id=args.social_set_id, debug=debug)
    if args.command == "queue":
        return commands.typefully_queue(
            api_url, args.start_date, args.end_date,
            social_set_id=args.social_set_id, debug=debug)
    if args.command == "create-draft":
        return commands.typefully_create_draft(
            api_url, args.file, social_set_id=args.social_set_id, debug=debug)
    return 2
