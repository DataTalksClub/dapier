"""S3 actions: put a fetched file into a bucket, find objects by name, and
read, delete, or mint download links for single objects.

``s3_upload`` sources the file from exactly one place: ``source_url``
(optionally authorized by a connection's bearer token) or ``source_s3`` (a
staged ``{bucket, key}`` object, like an email attachment, a render output,
or an earlier dropbox_read_file's output — bucket/key take templates). The
target bucket is written with a credential's access key pair —
the key/secret approach, not the stack's own identity — so backups can land
in any bucket.

``s3_find`` lists the bucket with the same stored keys and returns the
objects whose key (or basename) match a pattern — the first match keeps the
single-object output, the full list (capped at 25) rides in ``matches`` with
``next_token`` chaining a truncated listing; a miss is a ``found: False``
output, not an error. ``s3_read_object`` stages an object's bytes
for the steps that follow, ``s3_presign_url`` mints a short-lived download
link (symmetry with dropbox_get_temp_link) and ``s3_delete_object`` removes
one object — S3 deletes are idempotent, so deleting an absent key is
success, not an error.
"""
import mimetypes
import os

from ...connections import credentials, tokens
from . import base
from .templating import render

DEFAULT_CREDENTIAL_ID = "aws"
DOWNLOAD_TIMEOUT = 30
FIND_MATCH_MODES = ("exact", "prefix", "suffix", "contains")
FIND_PAGE_SIZE = 1000
FIND_MAX_PAGES = 3
# ``matches`` stops here so a bucket-sized listing cannot blow the run's
# memory; the truncated listing's token rides out in ``next_token``.
FIND_MATCH_CAP = 25
# s3_list_objects' bounds: 20 items by default, 100 hard-capped.
LIST_DEFAULT_ITEMS = 20
LIST_MAX_ITEMS = 100
# SigV4 presigned URLs cap at seven days; the default is Zapier-ish one hour.
PRESIGN_DEFAULT_EXPIRY = 3600
PRESIGN_MAX_EXPIRY = 7 * 24 * 60 * 60


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
    """The file bytes: one HTTP download or one staged S3 object.

    ``source_s3`` bucket/key take templates, so an earlier step's staged
    file (dropbox_read_file's output, say) feeds the upload without a
    literal bucket/key in the workflow.
    """
    transport = transport or base._default_transport
    url = render(str(action.get("source_url") or ""), event, steps).strip()
    source = action.get("source_s3") if isinstance(action.get("source_s3"), dict) else {}
    staged = {
        "bucket": render(str(source.get("bucket") or ""), event, steps).strip(),
        "key": render(str(source.get("key") or ""), event, steps).strip(),
    }
    if url and staged["bucket"]:
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
    if staged["bucket"] and staged["key"]:
        return base._s3_body(staged)
    raise ValueError("s3_upload needs source_url or source_s3 with bucket and key")


def _object_key(action, event, steps):
    rendered = render(str(action.get("key") or ""), event, steps)
    mode = action.get("key_mode", "safe")
    if mode not in ("safe", "exact"):
        raise ValueError("s3_upload key_mode must be safe or exact")
    if mode == "exact":
        rendered = rendered.lstrip("/")
        if not rendered:
            raise ValueError("s3_upload requires a key")
        return rendered
    rendered = rendered.strip().strip("/")
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
    kwargs = {"Bucket": bucket, "Key": key, "Body": body}
    omit = action.get("omit_content_type", False)
    if isinstance(omit, str):
        omit = omit.strip().lower() == "true"
    if not omit:
        kwargs["ContentType"] = content_type
    client.put_object(**kwargs)
    return {"bucket": bucket, "key": key, "bytes": len(body), "content_type": None if omit else content_type}


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


def _client(action, s3_client):
    """The injected test client, or a real one on the credential's key pair."""
    if s3_client is not None:
        return s3_client
    access_key, secret_key = _aws_keys(action)
    import boto3

    return boto3.client(
        "s3", aws_access_key_id=access_key, aws_secret_access_key=secret_key,
    )


def _find_client(action, s3_client):
    """The injected test client, or a real one on the credential's key pair."""
    return _client(action, s3_client)


def run_s3_find(action, event, *, transport=None, steps=None, s3_client=None):
    """Find the objects matching a pattern in a bucket (no exception on a
    miss — the output's ``found`` flag carries the answer).

    The first match keeps the single-object output (``key``, ``size``,
    ``last_modified``, ``bucket``) exactly as it was; every match up to
    FIND_MATCH_CAP rides in ``matches`` with the same per-object projection.
    A listing that stops — the cap reached, or the walk ran out of pages —
    hands the truncated listing's continuation token back in ``next_token``:
    feed it to a repeated run's ``next_token`` input (a for_each chain, say)
    to keep walking a big bucket. It is empty once the listing is exhausted.
    """
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
    token = render(str(action.get("next_token") or ""), event, steps).strip() or None
    matches = []
    for _page in range(FIND_MAX_PAGES):
        kwargs = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": FIND_PAGE_SIZE}
        if token:
            kwargs["ContinuationToken"] = token
        response = client.list_objects_v2(**kwargs)
        token = response.get("NextContinuationToken") if response.get("IsTruncated") else None
        for item in response.get("Contents") or []:
            key = str(item.get("Key") or "")
            if not _find_hit(mode, pattern, key):
                continue
            last_modified = item.get("LastModified")
            if hasattr(last_modified, "isoformat"):
                last_modified = last_modified.isoformat()
            matches.append({"key": key, "size": item.get("Size"),
                            "last_modified": last_modified})
            if len(matches) >= FIND_MATCH_CAP:
                break
        if len(matches) >= FIND_MATCH_CAP or not token:
            break
    if not matches:
        # A miss with the listing still truncated hands the pending token
        # back too — the match may live past the walk's page budget.
        return {"found": False, "key": None, "matches": [],
                "next_token": token or ""}
    first = matches[0]
    return {
        "found": True,
        "key": first["key"],
        "size": first["size"],
        "last_modified": first["last_modified"],
        "bucket": bucket,
        "matches": matches,
        "next_token": token or "",
    }


# --- single-object staples: read, presign, delete -------------------------------


def _required(action, key, event, steps, action_type):
    """One rendered, non-empty, whitespace-stripped string field.

    The value is addressed exactly as rendered — unlike ``s3_upload``'s
    target key there is no filename normalization, because read/delete/
    presign must hit the object the field (or the event) actually names.
    """
    rendered = render(str(action.get(key) or ""), event, steps).strip()
    if not rendered:
        raise ValueError(f"{action_type} requires a {key}")
    return rendered


def run_s3_read_object(action, event, *, transport=None, steps=None, s3_client=None):
    """Download one object and stage it for the steps that follow (GetObject).

    ``key`` falls back to the triggering event's object key, so an s3
    file.created → read_object chain needs nothing but the bucket. The bytes
    land in the render-artifacts bucket under
    ``s3/<event id>/<step id>/<filename>`` — staged like
    dropbox_read_file stages its downloads, with the stack's own identity
    writing the artifacts bucket — and the output names both the staged ref
    (``{filename, size, content_type, bucket, key}``) and the source object
    (``source_bucket``, ``source_key``). Pair with a step whose ``source_s3``
    takes templates to move the bytes elsewhere; the object stages in
    memory, so keep sources modest.
    """
    bucket = _required(action, "bucket", event, steps, "s3_read_object")
    key = render(str(action.get("key") or ""), event, steps).strip()
    if not key:
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        key = str(data.get("key") or "").strip()
    if not key:
        raise ValueError("s3_read_object requires a key (or an event carrying one)")
    client = _client(action, s3_client)
    response = client.get_object(Bucket=bucket, Key=key)
    body = response["Body"].read()
    filename = base._safe_filename(key.rsplit("/", 1)[-1])
    content_type = (response.get("ContentType")
                    or mimetypes.guess_type(filename)[0]
                    or "application/octet-stream")
    artifacts_bucket = os.environ["RENDER_ARTIFACTS_BUCKET"]
    staged_key = "s3/{}/{}/{}".format(
        str(event.get("id") or "unknown").replace("/", "_"),
        str(action.get("id") or "read").replace("/", "_"),
        filename,
    )
    stager = s3_client
    if stager is None:
        import boto3

        stager = boto3.client("s3")
    stager.put_object(
        Bucket=artifacts_bucket, Key=staged_key, Body=body, ContentType=content_type,
        ServerSideEncryption="AES256",
    )
    return {
        "filename": filename,
        "size": len(body),
        "content_type": content_type,
        "bucket": artifacts_bucket,
        "key": staged_key,
        "source_bucket": bucket,
        "source_key": key,
    }


def run_s3_presign_url(action, event, *, transport=None, steps=None, s3_client=None):
    """Mint a presigned download URL for one object (presigned GET,
    ``generate_presigned_url``).

    Symmetry with dropbox_get_temp_link: the output's ``link`` feeds
    straight into any step's ``source_url``, and hands a private bucket's
    bytes to someone without credentials. ``expires_in`` is seconds — one
    hour by default, capped at SigV4's seven-day ceiling (604800). The URL
    is computed, never sent: no network call happens in this step. Output:
    ``{link, bucket, key, expires_in}``.
    """
    bucket = _required(action, "bucket", event, steps, "s3_presign_url")
    key = _required(action, "key", event, steps, "s3_presign_url")
    value = action.get("expires_in")
    raw = str(value).strip() if value is not None else ""
    if raw:
        try:
            expires = int(float(raw))
        except ValueError:
            raise ValueError("s3_presign_url expires_in must be a number of "
                             "seconds") from None
    else:
        expires = PRESIGN_DEFAULT_EXPIRY
    if expires <= 0:
        raise ValueError("s3_presign_url expires_in must be a positive number "
                         "of seconds")
    if expires > PRESIGN_MAX_EXPIRY:
        expires = PRESIGN_MAX_EXPIRY
    client = _client(action, s3_client)
    link = client.generate_presigned_url(
        "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=expires)
    return {"link": link, "bucket": bucket, "key": key, "expires_in": expires}


def run_s3_delete_object(action, event, *, transport=None, steps=None, s3_client=None):
    """Delete one object from a bucket (DeleteObject).

    S3 deletes are idempotent — removing an absent key succeeds — so there
    is no ``found`` flag to branch on; the output names what the step asked
    the bucket to forget: ``{deleted: true, bucket, key}``.
    """
    bucket = _required(action, "bucket", event, steps, "s3_delete_object")
    key = _required(action, "key", event, steps, "s3_delete_object")
    client = _client(action, s3_client)
    client.delete_object(Bucket=bucket, Key=key)
    return {"deleted": True, "bucket": bucket, "key": key}


# --- s3_list_objects: one bounded listing of a bucket (or prefix) ---------------


def run_s3_list_objects(action, event, *, transport=None, steps=None, s3_client=None):
    """List a bucket's objects under ``prefix`` (ListObjectsV2; Zapier's
    List Files semantics — a bounded, ordered listing rather than a find).

    The listing rides the same credential resolution as
    :func:`run_s3_find`/:func:`run_s3_upload` (``_client`` — the injected
    test client, else a real one on the stored ``credential_id``/connection
    key pair). ``max_items`` bounds the output: 20 by default, capped at
    LIST_MAX_ITEMS (100) — a bucket-sized listing cannot blow the run's
    memory. Each item carries ``key``, ``size`` and ``last_modified`` (ISO,
    like every s3 listing projection); keys arrive in S3's own order — UTF-8
    binary, i.e. alphabetical. When the listing was cut short (the cap, or
    S3 said the page was truncated before ``max_items`` filled), ``truncated``
    is true and ``next_token`` carries the continuation token — feed it back
    in as this step's ``next_token`` to keep walking. An empty bucket or
    prefix is an empty ``items`` list, not an error. Output: ``{bucket,
    prefix, items, count, truncated, next_token}``.
    """
    bucket = _required(action, "bucket", event, steps, "s3_list_objects")
    prefix = render(str(action.get("prefix") or ""), event, steps).strip()
    raw_max = str(action.get("max_items") or "").strip()
    if raw_max:
        try:
            max_items = int(float(raw_max))
        except ValueError:
            raise ValueError("s3_list_objects max_items must be a whole "
                             "number") from None
        if max_items < 1:
            raise ValueError("s3_list_objects max_items must be at least 1")
    else:
        max_items = LIST_DEFAULT_ITEMS
    max_items = min(max_items, LIST_MAX_ITEMS)
    client = _client(action, s3_client)
    token = render(str(action.get("next_token") or ""), event, steps).strip() or None
    items = []
    truncated = False
    while True:
        request = {"Bucket": bucket, "MaxKeys": max_items - len(items)}
        if prefix:
            request["Prefix"] = prefix
        if token:
            request["ContinuationToken"] = token
        response = client.list_objects_v2(**request)
        for entry in response.get("Contents") or []:
            key = str(entry.get("Key") or "")
            if not key:
                continue
            last_modified = entry.get("LastModified")
            if hasattr(last_modified, "isoformat"):
                last_modified = last_modified.isoformat()
            items.append({"key": key, "size": entry.get("Size"),
                          "last_modified": last_modified})
            if len(items) >= max_items:
                break
        token = response.get("NextContinuationToken") \
            if response.get("IsTruncated") else None
        if len(items) >= max_items or not token:
            # A token at break time means S3 has more than we kept.
            truncated = bool(token)
            break
    return {
        "bucket": bucket,
        "prefix": prefix or None,
        "items": items,
        "count": len(items),
        "truncated": truncated,
        "next_token": token or "",
    }
