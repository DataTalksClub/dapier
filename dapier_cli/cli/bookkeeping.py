"""The `dapier bookkeeping` noun: the invoice review queue."""

from .. import commands

GROUPS = ("bookkeeping",)


def register(sub):
    bookkeeping_p = sub.add_parser(
        "bookkeeping",
        help="Bookkeeping review queue: parsed invoices awaiting confirmation")
    bookkeeping_sub = bookkeeping_p.add_subparsers(dest="command", required=True)

    list_p = bookkeeping_sub.add_parser(
        "list", help="The review queue, newest first")
    list_p.add_argument("--status", choices=["pending", "confirmed", "rejected"],
                        default=None, help="Only entries in this status")
    list_p.add_argument("--limit", type=int, default=None,
                        help="Max entries to list (default 50, max 200)")

    get_p = bookkeeping_sub.add_parser("get", help="One entry, every field")
    get_p.add_argument("entry_id", help="Entry id from `bookkeeping list`")

    confirm_p = bookkeeping_sub.add_parser(
        "confirm", help="Confirm a pending entry (optionally correcting fields)")
    confirm_p.add_argument("entry_id", help="Entry id from `bookkeeping list`")
    confirm_p.add_argument("--edit", action="append", default=[], metavar="KEY=VALUE",
                           help="Correct a field before confirming (repeatable; "
                                "e.g. --edit amount=691.13 --edit what='Cloud services')")

    reject_p = bookkeeping_sub.add_parser(
        "reject", help="Reject a pending entry (forwarding the invoice again stages a fresh one)")
    reject_p.add_argument("entry_id", help="Entry id from `bookkeeping list`")
    reject_p.add_argument("--note", default=None, help="Why it was rejected")


def run(args, api_url, debug, child=None):
    if args.command == "list":
        return commands.bookkeeping_list(api_url, status=args.status,
                                         limit=args.limit, debug=debug)
    if args.command == "get":
        return commands.bookkeeping_get(api_url, args.entry_id, debug)
    if args.command == "confirm":
        return commands.bookkeeping_confirm(api_url, args.entry_id,
                                            edits=args.edit, debug=debug)
    if args.command == "reject":
        return commands.bookkeeping_reject(api_url, args.entry_id,
                                           note=args.note, debug=debug)
    return 2
