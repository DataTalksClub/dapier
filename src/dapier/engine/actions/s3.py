"""S3 actions: put a fetched file into a bucket and find objects by name.

``s3_upload`` sources the file from exactly one place: ``source_url``
(optionally authorized by a connection's bearer token) or ``source_s3`` (a
staged ``{bucket, key}`` object, like an email attachment or a render
output). The target bucket is written with a credential's access key pair —
the key/secret approach, not the stack's own identity — so backups can land
in any bucket.

``s3_find`` lists the bucket with the same stored keys and returns the first
object whose key (or basename) matches a pattern; a miss is a ``found:
False`` output, not an error.
"""
from ...connections import credentials, tokens
from . import base
from .templating import render

DEFAULT_CREDENTIAL_ID = "aws"
DOWNLOAD_TIMEOUT = 30
FIND_MATCH_MODES = ("exact", "prefix", "suffix", "contains")
FIND_PAGE_SIZE = 1000
FIND_MAX_PAGES = 3


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


def _find_hit(mode, pattern, key):
    """Whether ``key`` matches ``pattern`` under ``mode``.

    The comparison runs against the full object key and — when that fails —
    against the basename, so users can find ``report.pdf`` without spelling
    the folder path.
    """
    for candidate in (key, key.rsplit("/", 1)[-1]):
        if mode == "exact":
            hit = candidate == pattern
        elif mode == "prefix":
            hit = candidate.startswith(pattern)
        elif mode == "suffix":
            hit = candidate.endswith(pattern)
        else:  # contains
            hit = pattern in candidate
        if hit:
            return True
    return False


def _find_client(action, s3_client):
    """The injected test client, or a real one on the credential's key pair."""
    if s3_client is not None:
        return s3_client
    access_key, secret_key = _aws_keys(action)
    import boto3

    return boto3.client(
        "s3", aws_access_key_id=access_key, aws_secret_access_key=secret_key,
    )


def run_s3_find(action, event, *, transport=None, steps=None, s3_client=None):
    """Find the first object matching a pattern in a bucket (no exception on
    a miss — the output's ``found`` flag carries the answer)."""
    bucket = render(str(action.get("bucket") or ""), event, steps).strip()
    if not bucket:
        raise ValueError("s3_find requires a bucket")
    pattern = render(str(action.get("pattern") or ""), event, steps).strip()
    if not pattern:
        raise ValueError("s3_find requires a pattern")
    prefix = render(str(action.get("prefix") or ""), event, steps).strip()
    mode = str(action.get("match") or "exact").strip().lower()
    if mode not in FIND_MATCH_MODES:
        raise ValueError(f"s3_find match must be one of: {', '.join(FIND_MATCH_MODES)}")
    client = _find_client(action, s3_client)
    token = None
    for _page in range(FIND_MAX_PAGES):
        kwargs = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": FIND_PAGE_SIZE}
        if token:
            kwargs["ContinuationToken"] = token
        response = client.list_objects_v2(**kwargs)
        for item in response.get("Contents") or []:
            key = str(item.get("Key") or "")
            if not _find_hit(mode, pattern, key):
                continue
            last_modified = item.get("LastModified")
            if hasattr(last_modified, "isoformat"):
                last_modified = last_modified.isoformat()
            return {
                "found": True,
                "key": key,
                "size": item.get("Size"),
                "last_modified": last_modified,
                "bucket": bucket,
            }
        token = response.get("NextContinuationToken") if response.get("IsTruncated") else None
        if not token:
            break
    return {"found": False, "key": None}
