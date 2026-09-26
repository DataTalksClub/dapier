"""s3_upload action: put a fetched file into an S3 bucket with stored AWS keys.

The file content comes from exactly one source: ``source_url`` (optionally
authorized by a connection's bearer token) or ``source_s3`` (a staged
``{bucket, key}`` object, like an email attachment or a render output). The
target bucket is written with a credential's access key pair — the key/secret
approach, not the stack's own identity — so backups can land in any bucket.
"""
from ...connections import credentials, tokens
from . import base
from .templating import render

DEFAULT_CREDENTIAL_ID = "aws"
DOWNLOAD_TIMEOUT = 30


def _aws_keys(action):
    """The (access_key_id, secret_access_key) pair for the target bucket."""
    credential_id = str(action.get("credential_id") or DEFAULT_CREDENTIAL_ID).strip()
    if action.get("connection_id"):
        credential_id = base._connected_connection(action["connection_id"])["credential_id"]
    try:
        secret = credentials.get_credential(credential_id)
    except KeyError:
        raise ValueError(f"credential {credential_id} is not configured") from None
    access_key = secret.get("access_key_id")
    secret_key = secret.get("secret_access_key")
    if not access_key or not secret_key:
        raise ValueError(f"credential {credential_id} does not contain AWS keys")
    return access_key, secret_key


def _source_token(connection_id, *, transport=None):
    """Bearer token for the source fetch; refreshed for OAuth connections."""
    connection = base._connected_connection(connection_id)
    if connection.get("provider"):
        try:
            token, _info = tokens.get_access_token(connection, transport=transport)
        except tokens.TokenError as exc:
            raise ValueError(f"connection {connection_id} has no usable token: {exc}") from None
        return token
    secret = credentials.get_credential(connection["credential_id"])
    token = secret.get("token") or secret.get("access_token")
    if not token:
        raise ValueError(f"connection {connection_id} has no stored token")
    return token


def _source_body(action, event, steps, *, transport=None):
    """The file bytes: one HTTP download or one staged S3 object."""
    transport = transport or base._default_transport
    url = render(str(action.get("source_url") or ""), event, steps).strip()
    source = action.get("source_s3") if isinstance(action.get("source_s3"), dict) else {}
    if url and source.get("bucket"):
        raise ValueError("s3_upload takes either source_url or source_s3, not both")
    if url:
        headers = {}
        if action.get("source_connection_id"):
            headers["authorization"] = f"Bearer {_source_token(
                action["source_connection_id"], transport=transport)}"
        try:
            status, body = transport("GET", url, headers=headers, body=None,
                                     timeout=DOWNLOAD_TIMEOUT)
        except Exception as exc:
            raise RuntimeError(f"file download unreachable: {type(exc).__name__}") from None
        if status >= 300:
            raise RuntimeError(f"file download returned HTTP {status}")
        return body
    if source.get("bucket") and source.get("key"):
        return base._s3_body(source)
    raise ValueError("s3_upload needs source_url or source_s3 with bucket and key")


def _object_key(action, event, steps):
    rendered = render(str(action.get("key") or ""), event, steps).strip().strip("/")
    if not rendered:
        raise ValueError("s3_upload requires a key")
    return "/".join(base._safe_filename(segment) for segment in rendered.split("/"))


def _content_type(action, event, steps):
    rendered = render(str(action.get("content_type") or ""), event, steps).strip()
    if rendered:
        return rendered
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    mime = str(data.get("mimeType") or "").strip()
    return mime or "application/octet-stream"


def run_s3_upload(action, event, *, transport=None, steps=None, s3_client=None):
    bucket = render(str(action.get("bucket") or ""), event, steps).strip()
    if not bucket:
        raise ValueError("s3_upload requires a bucket")
    key = _object_key(action, event, steps)
    content_type = _content_type(action, event, steps)
    access_key, secret_key = _aws_keys(action)
    body = _source_body(action, event, steps, transport=transport)
    client = s3_client
    if client is None:
        import boto3

        client = boto3.client(
            "s3", aws_access_key_id=access_key, aws_secret_access_key=secret_key,
        )
    client.put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type)
    return {"bucket": bucket, "key": key, "bytes": len(body), "content_type": content_type}
