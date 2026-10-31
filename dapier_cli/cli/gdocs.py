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
    for name, hlp in (("append", "Append text at end of doc"), ("replace", "Replace all occurrences of --find")):
        x = sp.add_parser(name, help=hlp)
        common(x)
        x.add_argument("--text", default=None)
        x.add_argument("--file", default=None, help="Read text from file (default stdin)")
        if name == "replace":
            x.add_argument("--find", required=True)


def run(args, api_url, debug, child=None):
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
