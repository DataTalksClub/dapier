"""Parser registration and dispatch for the `dapier` CLI, one module per noun.

Each module registers its nouns with `register(sub)` and dispatches through
the dapier_cli.commands package namespace at call time, so tests can patch
commands.<handler>.
"""

import argparse

from . import access, audit, auth, connections, emails, hooks, ops, \
    polls, runs, schedules, storage, typefully, slack, workflows, gdocs

MODULES = (auth, connections, access, ops, runs, audit, storage,
           workflows, hooks, schedules, polls, emails, typefully, slack, gdocs)

EPILOG = """common tasks:
  dapier slack find-user "Mikhail"            # Slack user lookup (name, handle, email, or id)
  dapier gdocs get <doc-url-or-id>             # read a Google Doc (stored Google connection)
  dapier emails received --limit 25            # dtcdev inbox receipts (NOT Gmail — see below)
  dapier connections list                      # provider connections granted to you

Gmail, Calendar, and Drive live outside the CLI (there is no
`gmail search` command). From the repo root, via the google-data-pull
skill (.agents/skills/google-data-pull/SKILL.md):
  python3 .agents/skills/google-data-pull/gpull.py --as "gmail datatalks" --agent admin-os gmail list --q "from:nebius.com newer_than:30d"
  # accounts: "gmail datatalks" (alexey@datatalks.club), "gmail alexey.s.grigoriev" (personal)"""


def build_parser():
    parser = argparse.ArgumentParser(
        prog="dapier",
        description="Dapier OAuth token factory CLI",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--api-url", default=None, help="Dapier API base URL")
    parser.add_argument("--debug", action="store_true", help="Print requests (never credentials)")
    sub = parser.add_subparsers(dest="group", required=True)
    for module in MODULES:
        module.register(sub)
    return parser


def dispatch(args, api_url, debug, child=None):
    for module in MODULES:
        if getattr(args, "group", None) in module.GROUPS:
            return module.run(args, api_url, debug, child=child)
    return 2
