"""Helpers shared by several `dapier` command implementations."""

import base64
import json
import sys


def read_json_file(path):
    """Return the parsed JSON body, or ``(None, error_message)``."""
    try:
        with (sys.stdin if path == "-" else open(path, encoding="utf-8")) as handle:
            return json.loads(handle.read()), None
    except OSError as exc:
        return None, f"Cannot read {path}: {exc}"
    except ValueError as exc:
        return None, f"{path} is not valid JSON: {exc}"


def print_json(data):
    print(json.dumps(data, indent=2, ensure_ascii=False))


def preview_body(body):
    """Print the exact request body a write would send, for review before
    applying it (--preview: nothing is sent). Attachment/file payloads show
    as sizes, not bytes."""
    trimmed = {}
    for key, value in (body or {}).items():
        if key == "attachments" and isinstance(value, list):
            trimmed[key] = [
                {**item, "content_b64": f"<{len(base64.b64decode(item.get('content_b64') or ''))} bytes>"}
                if isinstance(item, dict) and item.get("content_b64") else item
                for item in value
            ]
        elif key == "content_b64" and isinstance(value, str):
            trimmed[key] = f"<{len(base64.b64decode(value))} bytes>"
        else:
            trimmed[key] = value
    print_json({"preview": True, "would_send": trimmed})
    return 0

