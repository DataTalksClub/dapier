"""Parser registration and dispatch for the `dapier` CLI, one module per noun.

Each module registers its nouns with `register(sub)` and dispatches through
the dapier_cli.commands package namespace at call time, so tests can patch
commands.<handler>.
"""

import argparse

from . import access, audit, auth, bookkeeping, connections, emails, hooks, ops, \
    polls, runs, schedules, storage, triggers, workflows

MODULES = (auth, connections, access, ops, runs, audit, storage, triggers,
           workflows, hooks, schedules, polls, emails, bookkeeping)


def build_parser():
    parser = argparse.ArgumentParser(prog="dapier", description="Dapier OAuth token factory CLI")
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
