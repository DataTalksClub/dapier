"""Shared helpers for workflow actions."""
import hashlib
import hmac
import json
import os
import urllib.request
from functools import lru_cache

from ...connections import records as connections


@lru_cache
def _signing_secret(secret_id):
    import boto3

    secrets = boto3.client("secretsmanager")
    value = secrets.get_secret_value(SecretId=secret_id)["SecretString"]
    try:
        return json.loads(value).get("signing_secret", value)
    except json.JSONDecodeError:
        return value

def _json_request(url, payload, headers=None, timeout=10):
    body = json.dumps(payload, separators=(",", ":")).encode()
    request = urllib.request.Request(
        url,
        data=body,
        headers={"content-type": "application/json", **(headers or {})},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        response_body = response.read()
        if response.status >= 300:
            raise RuntimeError(f"request returned HTTP {response.status}")
        return json.loads(response_body) if response_body else {}

class _SafeFormat(dict):
    def __missing__(self, key):
        return ""

def _connected_connection(connection_id):
    """Load a connected connection record for use by an action."""
    import boto3

    table = boto3.resource("dynamodb").Table(os.environ["CONNECTIONS_TABLE"])
    try:
        connection = connections.find_connection(table, connection_id)
    except LookupError as exc:
        raise ValueError(f"connection {connection_id} is not configured: {exc}") from None
    if connection.get("status") != connections.STATUS_CONNECTED:
        raise ValueError(f"connection {connection_id} is not connected")
    return connection

@lru_cache
def secrets_value(secret_id):
    import boto3

    return boto3.client("secretsmanager").get_secret_value(SecretId=secret_id)["SecretString"]

def _s3_body(ref):
    import boto3

    return boto3.client("s3").get_object(Bucket=ref["bucket"], Key=ref["key"])["Body"].read()

def _safe_filename(name):
    name = str(name or "").replace("\\", "/").split("/")[-1].strip()
    return (name or "file")[:255]

# Vendors staple Terms-of-Service and legal HTML onto invoice and receipt
# emails; those parts are never the document a document intake is for. A
# missing content type reads as a document.
BOILERPLATE_CONTENT_TYPES = ("text/html", "text/plain")

def is_document_attachment(attachment):
    content_type = str((attachment or {}).get("content_type") or "")
    return content_type.split(";")[0].strip().lower() not in BOILERPLATE_CONTENT_TYPES

def _default_transport(method, url, *, headers, body, timeout=15):
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, response.read()
