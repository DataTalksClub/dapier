"""Stateless opaque page tokens: a JSON payload, base64url-encoded.

Listings (inbox items, runs, audit entries, connection records) carry the
last row's sort key in the token, so paging holds no server state and a
token keeps decoding across deploys.
"""
import base64
import json


def encode_token(payload):
    """The base64url token carrying ``payload`` (sorted keys, no padding)."""
    raw = json.dumps(payload, sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def decode_token(token):
    """The payload dict behind a token, or None when malformed."""
    text = str(token or "")
    try:
        data = json.loads(base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)))
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None
