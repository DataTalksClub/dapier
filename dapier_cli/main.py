"""`dapier` command line interface: argument handling and error mapping.

Parser registration and per-noun dispatch live in dapier_cli.cli; command
implementations live in dapier_cli.commands.
"""

import sys

from . import auth, config
from .api import ApiError
from .cli import build_parser, dispatch


def main(argv=None):
    raw = list(sys.argv[1:] if argv is None else argv)
    # Split the child command on the first `--` before argparse sees it, so
    # child flags never confuse option parsing (nargs=REMAINDER would swallow
    # our own options).
    child = None
    if "--" in raw:
        index = raw.index("--")
        raw, child = raw[:index], raw[index + 1:]
    args = build_parser().parse_args(raw)
    api_url = config.api_url(args.api_url)
    debug = args.debug
    try:
        return dispatch(args, api_url, debug, child=child)
    except ApiError as exc:
        print(f"Error: {exc}")
        if exc.status == 401:
            return 3
        if exc.status in (403, 404):
            return 4
        return 5
    except auth.LoginError as exc:
        print(f"Error: {exc}")
        return 3
    return 2


if __name__ == "__main__":
    sys.exit(main())
