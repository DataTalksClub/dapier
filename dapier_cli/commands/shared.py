"""Helpers shared by several `dapier` command implementations."""

import json
import sys


def owner_query(owner=None, as_id=None, assignee=None):
    """Query params for the read-only owner filter. Writes are not affected."""
    params = {}
    if owner:
        params["owner"] = owner
    if as_id:
        params["as"] = as_id
    if assignee:
        params["assignee"] = assignee
    return params


def read_json_file(path):
    """Return the parsed JSON body, or ``(None, error_message)``."""
    try:
        with (sys.stdin if path == "-" else open(path, encoding="utf-8")) as handle:
            return json.loads(handle.read()), None
    except OSError as exc:
        return None, f"Cannot read {path}: {exc}"
    except ValueError as exc:
        return None, f"{path} is not valid JSON: {exc}"
