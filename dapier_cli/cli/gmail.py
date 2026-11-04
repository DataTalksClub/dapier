"""The `dapier gmail` noun: account pick, search/read/label, drafts,
send, and replies — server-side through /api/agent/gmail/*."""

from .. import commands

GROUPS = ("gmail",)

EPILOG = """examples:
  dapier gmail accounts                          # list Google accounts (email + connection id)
  dapier gmail search "from:boss is:unread" --account me@gmail.com
  dapier gmail read MSG_ID --save-attachments ./att --account me@gmail.com
  dapier gmail send --to a@b.c --subject "Hi" --file body.txt --attach report.pdf --account me@gmail.com
  dapier gmail reply MSG_ID --text "On it" --account me@gmail.com  # direct send in the original thread
  dapier gmail reply MSG_ID --draft --account me@gmail.com  # park it as an editable reply draft
  dapier gmail draft create --to a@b.c --subject "Invoice" --text "..." --attach inv.pdf --account me@gmail.com
  dapier gmail draft send DRAFT_ID --account me@gmail.com
--account is required on every operation except accounts; use the verified
email or connection id. Ambiguous fragments are rejected."""

COMMANDS = ("accounts", "search", "list", "read", "attachment", "threads",
            "thread", "labels", "mark", "send", "reply", "draft")


def _body_args(p):
    group = p.add_mutually_exclusive_group()
    group.add_argument("--text", default=None, help="Body text (or --file)")
    group.add_argument("--file", default=None,
                       help="Read the body from a file ('-' = stdin)")


def _attach_arg(p):
    p.add_argument("--attach", action="append", default=[],
                   metavar="PATH[;MIME]",
                   help="Attach a file; repeat for several. Optional type: report.pdf;application/pdf")


def _common_message(p):
    _body_args(p)
    _attach_arg(p)
    p.add_argument("--json", action="store_true", help="Print raw JSON")
    p.add_argument("--preview", action="store_true",
                   help="Print the exact request body and exit — nothing is sent")


def register(sub):
    import argparse

    p = sub.add_parser(
        "gmail", help="Gmail over a stored Google connection: search, read, "
        "drafts, send, reply (server-side token)",
        epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub_p = p.add_subparsers(dest="command", required=True)

    acc = sub_p.add_parser("accounts", help="List Google accounts serving Gmail")
    acc.add_argument("--json", action="store_true")

    search = sub_p.add_parser("search", help="Search messages (Gmail query syntax)")
    search.add_argument("query")
    _list_args(search)

    listing = sub_p.add_parser("list", help="List the newest messages")
    _list_args(listing)

    read = sub_p.add_parser("read", help="Read one message (headers, body, attachments)")
    read.add_argument("message_id")
    read.add_argument("--account", required=True)
    read.add_argument("--html", action="store_true", help="Include the HTML part")
    read.add_argument("--json", action="store_true")
    read.add_argument("--save-attachments", default=None, metavar="DIR",
                      help="Decode every attachment into this directory")

    att = sub_p.add_parser("attachment", help="Save one attachment by id")
    att.add_argument("message_id")
    att.add_argument("attachment_id")
    att.add_argument("--account", required=True)
    att.add_argument("--out", default=None, help="Output path (default: the filename)")

    threads = sub_p.add_parser("threads", help="List threads (optionally searched)")
    threads.add_argument("query", nargs="?", default=None)
    _list_args(threads)

    thread = sub_p.add_parser("thread", help="Read one thread with all messages")
    thread.add_argument("thread_id")
    thread.add_argument("--account", required=True)
    thread.add_argument("--json", action="store_true")

    labels = sub_p.add_parser("labels", help="List the account's labels")
    labels.add_argument("--account", required=True)
    labels.add_argument("--json", action="store_true")

    mark = sub_p.add_parser("mark", help="Mark read/unread, archive, trash, label")
    mark.add_argument("message_id")
    mark.add_argument("--account", required=True)
    mode = mark.add_mutually_exclusive_group()
    mode.add_argument("--read", action="store_true")
    mode.add_argument("--unread", action="store_true")
    mark.add_argument("--archive", action="store_true")
    mode.add_argument("--trash", action="store_true")
    mode.add_argument("--untrash", action="store_true")
    mark.add_argument("--add-label", action="append", default=[])
    mark.add_argument("--remove-label", action="append", default=[])
    mark.add_argument("--json", action="store_true")

    send = sub_p.add_parser("send", help="Send a new email (attachments supported)")
    _recipient_args(send)
    _common_message(send)

    reply = sub_p.add_parser("reply", help="Reply to a message in its thread (send or --draft)")
    reply.add_argument("message_id")
    reply.add_argument("--account", required=True)
    reply.add_argument("--to", help="Explicit recipients instead of the inferred reply recipients")
    reply.add_argument("--cc", help="Explicit Cc recipients")
    reply.add_argument("--bcc", help="Explicit Bcc recipients")
    reply.add_argument("--all", dest="reply_all", action="store_true",
                       help="Reply to all original recipients")
    reply.add_argument("--draft", action="store_true",
                       help="Create an editable reply draft instead of sending")
    _common_message(reply)

    draft = sub_p.add_parser("draft", help="Drafts: create/update/list/get/delete/send")
    draft_sp = draft.add_subparsers(dest="draft_command", required=True)

    dcreate = draft_sp.add_parser("create", help="Create a draft (attachments supported)")
    _recipient_args(dcreate)
    _common_message(dcreate)
    dcreate.add_argument("--html-file", help="Read HTML body from a UTF-8 file (or stdin with -); may accompany --file/--text")

    dupdate = draft_sp.add_parser("update", help="Replace a draft's content")
    dupdate.add_argument("draft_id")
    _recipient_args(dupdate, required=False)
    _common_message(dupdate)
    dupdate.add_argument("--html-file", help="Read HTML body from a UTF-8 file (or stdin with -); replaces the draft body")

    dlist = draft_sp.add_parser("list", help="List drafts")
    dlist.add_argument("--account", required=True)
    dlist.add_argument("--json", action="store_true")
    dlist.add_argument("--max", type=int, default=25)
    dlist.add_argument("--page-token")

    dget = draft_sp.add_parser("get", help="Show one draft")
    dget.add_argument("draft_id")
    dget.add_argument("--account", required=True)
    dget.add_argument("--json", action="store_true")
    dget.add_argument("--html", action="store_true", help="Print the HTML body instead of plain text")

    ddelete = draft_sp.add_parser("delete", help="Delete a draft")
    ddelete.add_argument("draft_id")
    ddelete.add_argument("--account", required=True)

    dsend = draft_sp.add_parser("send", help="Send an existing draft")
    dsend.add_argument("draft_id")
    dsend.add_argument("--account", required=True)
    dsend.add_argument("--json", action="store_true")


def _list_args(p):
    p.add_argument("--account", required=True)
    p.add_argument("--max", type=int, default=25)
    p.add_argument("--json", action="store_true")
    p.add_argument("--label", default=None, help="Filter by label id, e.g. INBOX")
    p.add_argument("--page-token", default=None)


def _recipient_args(p, required=True):
    p.add_argument("--account", required=True)
    p.add_argument("--to", required=required)
    p.add_argument("--cc", default=None)
    p.add_argument("--bcc", default=None)
    p.add_argument("--subject", default=None)


def run(args, api_url, debug, child=None):
    c = commands
    account = getattr(args, "account", None)
    if args.command == "accounts":
        return c.gmail_accounts(api_url, as_json=args.json, debug=debug)
    if args.command == "search":
        return c.gmail_search(api_url, args.query, account=account, max_results=args.max,
                              label=args.label, page_token=args.page_token, as_json=args.json, debug=debug)
    if args.command == "list":
        return c.gmail_list(api_url, account=account, max_results=args.max,
                            label=args.label, page_token=args.page_token, as_json=args.json, debug=debug)
    if args.command == "read":
        return c.gmail_read(api_url, args.message_id, account=account, include_html=args.html,
                            as_json=args.json, save_attachments=args.save_attachments, debug=debug)
    if args.command == "attachment":
        return c.gmail_attachment(api_url, args.message_id, args.attachment_id, account=account,
                                  out=args.out, debug=debug)
    if args.command == "threads":
        return c.gmail_threads(api_url, query=args.query, account=account,
                               max_results=args.max, page_token=args.page_token, debug=debug)
    if args.command == "thread":
        return c.gmail_thread(api_url, args.thread_id, account=account, as_json=args.json, debug=debug)
    if args.command == "labels":
        return c.gmail_labels(api_url, account=account, as_json=args.json, debug=debug)
    if args.command == "mark":
        if not any((args.read, args.unread, args.archive, args.trash, args.untrash,
                    args.add_label, args.remove_label)):
            print("mark needs a change: --read/--unread/--archive/--trash/--untrash "
                  "or --add-label/--remove-label")
            return 2
        return c.gmail_mark(api_url, args.message_id, account=account, read=args.read,
                            unread=args.unread, archive=args.archive, trash=args.trash,
                            untrash=args.untrash, add_label=args.add_label,
                            remove_label=args.remove_label, as_json=args.json, debug=debug)
    if args.command == "send":
        return c.gmail_send(api_url, account=account, to=args.to, cc=args.cc, bcc=args.bcc,
                            subject=args.subject, text=args.text, file=args.file,
                            attach=args.attach, as_json=args.json,
                            preview=getattr(args, "preview", False), debug=debug)
    if args.command == "reply":
        return c.gmail_reply(api_url, args.message_id, account=account, reply_all=args.reply_all,
                             text=args.text, file=args.file, attach=args.attach,
                             draft=args.draft, to=args.to, cc=args.cc, bcc=args.bcc, as_json=args.json,
                             preview=getattr(args, "preview", False), debug=debug)
    if args.command == "draft":
        return _run_draft(args, api_url, account, debug)
    return 2


def _run_draft(args, api_url, account, debug):
    c = commands
    name = args.draft_command
    if name == "list":
        return c.gmail_draft_list(api_url, account=account, as_json=args.json, debug=debug,
                                  max_results=args.max, page_token=args.page_token)
    if name == "get":
        return c.gmail_draft_get(api_url, args.draft_id, account=account, as_json=args.json,
                                  include_html=args.html, debug=debug)
    if name == "delete":
        return c.gmail_draft_delete(api_url, args.draft_id, account=account, debug=debug)
    if name == "send":
        return c.gmail_draft_send(api_url, args.draft_id, account=account, as_json=args.json, debug=debug)
    if name == "create":
        return c.gmail_draft_create(api_url, account=account, to=args.to, cc=args.cc,
                                    bcc=args.bcc, subject=args.subject, text=args.text,
                                    file=args.file, attach=args.attach, html_file=args.html_file, as_json=args.json,
                                    preview=getattr(args, "preview", False), debug=debug)
    if name == "update":
        return c.gmail_draft_update(api_url, args.draft_id, account=account, to=args.to,
                                    cc=args.cc, bcc=args.bcc, subject=args.subject,
                                    text=args.text, file=args.file, attach=args.attach, html_file=args.html_file,
                                    as_json=args.json,
                                    preview=getattr(args, "preview", False), debug=debug)
    return 2
