"""Helpers shared by several `dapier` command implementations."""

import json
import sys


def entry_label(item):
    """What a trigger runs: its inline action types."""
    return ",".join(action.get("type", "?") for action in item.get("actions") or []) or "-"


def read_json_file(path):
    """Return the parsed JSON body, or ``(None, error_message)``."""
    try:
        with (sys.stdin if path == "-" else open(path, encoding="utf-8")) as handle:
            return json.loads(handle.read()), None
    except OSError as exc:
        return None, f"Cannot read {path}: {exc}"
    except ValueError as exc:
        return None, f"{path} is not valid JSON: {exc}"
