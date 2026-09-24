"""Shared HTTP plumbing for the API surfaces: JSON responses, redirects, bodies."""
import base64
import json
from datetime import datetime
from decimal import Decimal


def _json_default(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")

def _json_response(status, body, *, cookies=None, headers=None):
    response = {
        "statusCode": status,
        "headers": {"content-type": "application/json", **(headers or {})},
        "body": json.dumps(body, default=_json_default),
    }
    if cookies:
        response["cookies"] = cookies
    return response

def _redirect(location, *, cookies=None, status=302, headers=None):
    response = {
        "statusCode": status,
        "headers": {"location": location, **(headers or {})},
        "body": "",
    }
    if cookies:
        response["cookies"] = cookies
    return response

def _request_json(event):
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("request body must be an object")
    return value
