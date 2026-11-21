"""The `dapier gdocs` noun: read, append to, or find/replace in a Google Doc."""

from .. import commands

GROUPS = ("gdocs",)

EPILOG = """examples:
  dapier gdocs get https://docs.google.com/document/d/DOC_ID/edit
  dapier gdocs append DOC_ID --file rec.txt          # or: ... --text "..." / stdin
  dapier gdocs replace DOC_ID --find "{{PLACEHOLDER}}" --text "new"
  --as "drive datatalks" (default, alexey@datatalks.club) | "drive alexey.s.grigoriev"
The doc must be editable by that account (Drive capabilities.canEdit)."""


def register(sub):
    import argparse
    p = sub.add_parser("gdocs", help="Google Docs: read text, append, find/replace (stored Google connection)",
                       epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = p.add_subparsers(dest="command", required=True)

    def common(x):
        x.add_argument("doc", help="Doc URL or id")
        x.add_argument("--as", dest="ref", default="drive datatalks", help="Connection reference")
        x.add_argument("--agent", default="admin-os")

    g = sp.add_parser("get", help="Print title, revision and plain text")
    common(g)
    g.add_argument("--json", action="store_true")
    g.add_argument("--account", help="Operator connection email/id; returns full structure as JSON")
    b = sp.add_parser("batch-update", help="Apply native Docs requests with a revision guard")
    b.add_argument("doc", help="Doc URL or id")
    b.add_argument("--account", required=True)
    b.add_argument("--file", required=True, help="Requests JSON file, or - for stdin")
    b.add_argument("--revision", required=True, help="revisionId from gdocs get --account")
    for name, hlp in (("append", "Append text at end of doc"), ("replace", "Replace all occurrences of --find")):
        x = sp.add_parser(name, help=hlp)
        common(x)
        x.add_argument("--text", default=None)
        x.add_argument("--file", default=None, help="Read text from file (default stdin)")
        if name == "replace":
            x.add_argument("--find", required=True)


def run(args, api_url, debug, child=None):
    if args.command == "batch-update":
        return commands.gdocs_batch_update(api_url, args.doc, args.file, args.account, args.revision, debug)
    if args.command == "get" and args.account:
        return commands.gdocs_structure(api_url, args.doc, args.account, debug)
    if args.command == "get":
        return commands.gdocs_get(api_url, args.doc, ref=args.ref, agent=args.agent,
                                  as_json=args.json, debug=debug)
    if args.command == "append":
        return commands.gdocs_append(api_url, args.doc, text=args.text, file=args.file,
                                     ref=args.ref, agent=args.agent, debug=debug)
    if args.command == "replace":
        return commands.gdocs_replace(api_url, args.doc, args.find, text=args.text, file=args.file,
                                      ref=args.ref, agent=args.agent, debug=debug)
    return 2
