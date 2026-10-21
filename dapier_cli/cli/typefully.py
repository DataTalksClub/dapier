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
        help="Create an unscheduled draft from JSON (no publish_at/plan_at unless --allow-schedule)")
    create.add_argument("--file", required=True, help="JSON file, or - for stdin")
    create.add_argument("--social-set-id", type=int, default=DEFAULT_SOCIAL_SET_ID)
    create.add_argument("--allow-schedule", action="store_true",
                        help="Allow publish_at/plan_at in the body (needs PUBLISH key permission)")
    patch = sub_p.add_parser(
        "patch-draft",
        help="PATCH an existing draft from JSON (attach media_ids, edit, schedule)")
    patch.add_argument("draft_id", type=int)
    patch.add_argument("--file", required=True, help="JSON patch body file, or - for stdin")
    patch.add_argument("--social-set-id", type=int, default=DEFAULT_SOCIAL_SET_ID)
    upload = sub_p.add_parser(
        "upload-media",
        help="Upload an image/video/GIF/PDF and poll until ready (prints media_id + status only)")
    upload.add_argument("--file", required=True, help="Media file to upload")
    upload.add_argument("--alt-text", default=None, help="Accessibility description")
    upload.add_argument("--file-name", default=None, help="Override the uploaded file name")
    upload.add_argument("--social-set-id", type=int, default=DEFAULT_SOCIAL_SET_ID)
    status = sub_p.add_parser("media-status", help="Processing status for an uploaded media id (JSON)")
    status.add_argument("media_id")
    status.add_argument("--social-set-id", type=int, default=DEFAULT_SOCIAL_SET_ID)


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
            api_url, args.file, social_set_id=args.social_set_id,
            allow_schedule=args.allow_schedule, debug=debug)
    if args.command == "patch-draft":
        return commands.typefully_patch_draft(
            api_url, args.draft_id, args.file, social_set_id=args.social_set_id, debug=debug)
    if args.command == "upload-media":
        return commands.typefully_upload_media(
            api_url, args.file, alt_text=args.alt_text, file_name=args.file_name,
            social_set_id=args.social_set_id, debug=debug)
    if args.command == "media-status":
        return commands.typefully_media_status(
            api_url, args.media_id, social_set_id=args.social_set_id, debug=debug)
    return 2
