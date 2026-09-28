"""Amazon S3 connector: upload files to and find objects in a bucket with
stored AWS keys, plus bucket/object discovery, the stored-keys health
check, the "new file in bucket" poll source, and the trigger chip's sample
pull."""
from ..engine.actions.s3 import (
    DEFAULT_CREDENTIAL_ID,
    run_s3_delete_object,
    run_s3_find,
    run_s3_list_objects,
    run_s3_presign_url,
    run_s3_read_object,
    run_s3_upload,
)
from ..triggers.poll_sources import PollSource, register_source
from .registry import (
    Action,
    ConnectionTest,
    Discovery,
    register,
    register_connection_test,
    register_discovery,
)

register(Action(
    type="s3_upload",
    label="Amazon S3",
    icon="s3",
    description="Upload a file to an S3 bucket (Upload File)",
    run=lambda action, event, workflow_id, steps=None: run_s3_upload(action, event, steps=steps),
    required=frozenset({"bucket", "key"}),
    optional=frozenset({
        "credential_id", "connection_id",
        "source_url", "source_connection_id", "source_s3", "content_type",
    }),
    fields=(
        {"key": "credential_id", "label": "Credential ID", "placeholder": "aws (default)"},
        {"key": "bucket", "label": "Bucket", "placeholder": "datatalks-mailchimp-backup", "required": True,
         "discover": {"resource": "s3.buckets"}},
        {"key": "key", "label": "Object key", "placeholder": "mailchimp/{name}", "required": True,
         "discover": {"resource": "s3.objects", "params": {"bucket": "bucket"}}},
        {"key": "source_url", "label": "Source URL", "type": "url",
         "placeholder": "https://www.googleapis.com/drive/v3/files/{id}?alt=media",
         "discover": {"resource": "google-drive.files"},
         "help": "Browse the source connection's Drive files; picking one fills the download URL"},
        {"key": "source_connection_id", "label": "Source connection ID", "placeholder": "google — authorizes the source URL"},
        {"key": "content_type", "label": "Content type", "placeholder": "defaults to the trigger's mimeType"},
    ),
))

register(Action(
    type="s3_find",
    label="S3: find object",
    icon="s3",
    description=("Find the objects matching a name pattern in a bucket "
                 "(Find Object). Output: {found, key, size, last_modified, "
                 "bucket, matches (up to 25), next_token} — key stays the "
                 "first match; feed next_token back in as this field to "
                 "walk a big bucket."),
    run=lambda action, event, workflow_id, steps=None: run_s3_find(action, event, steps=steps),
    required=frozenset({"bucket", "pattern"}),
    optional=frozenset({"prefix", "match", "next_token", "credential_id", "connection_id"}),
    fields=(
        {"key": "credential_id", "label": "Credential ID", "placeholder": "aws (default)"},
        {"key": "bucket", "label": "Bucket", "placeholder": "datatalks-mailchimp-backup", "required": True,
         "discover": {"resource": "s3.buckets"}},
        {"key": "pattern", "label": "Name pattern", "placeholder": "{name}.pdf", "required": True,
         "help": "Compared against the object key, falling back to its file name"},
        {"key": "prefix", "label": "Key prefix", "placeholder": "reports/2026/",
         "discover": {"resource": "s3.objects",
                      "params": {"bucket": "bucket", "prefix": "prefix"}}},
        {"key": "match", "label": "Match", "type": "select",
         "options": ["exact", "prefix", "suffix", "contains"], "default": "exact"},
        {"key": "next_token", "label": "Continuation token",
         "help": "continuation token from a previous find's output — chain "
                 "through for_each or repeated runs to walk a big bucket"},
    ),
))

register(Action(
    type="s3_list_objects",
    label="S3: list objects",
    icon="s3",
    description=("List a bucket's objects under a prefix (ListObjectsV2; "
                 "List Files). Output: {bucket, prefix, items, count, "
                 "truncated, next_token} — each item is {key, size, "
                 "last_modified}; keys arrive alphabetically, capped at Max "
                 "items (default 20, up to 100). Feed next_token back in to "
                 "keep walking a truncated listing."),
    run=lambda action, event, workflow_id, steps=None: run_s3_list_objects(
        action, event, steps=steps),
    required=frozenset({"bucket"}),
    optional=frozenset({"prefix", "max_items", "next_token",
                        "credential_id", "connection_id"}),
    fields=(
        {"key": "credential_id", "label": "Credential ID", "placeholder": "aws (default)"},
        {"key": "bucket", "label": "Bucket", "placeholder": "datatalks-mailchimp-backup", "required": True,
         "discover": {"resource": "s3.buckets"}},
        {"key": "prefix", "label": "Key prefix", "placeholder": "reports/2026/",
         "discover": {"resource": "s3.objects",
                      "params": {"bucket": "bucket", "prefix": "prefix"}},
         "help": "Only keys starting with this prefix; empty lists the whole bucket"},
        {"key": "max_items", "label": "Max items", "type": "number", "default": "20",
         "help": "Listing bound — capped at 100"},
        {"key": "next_token", "label": "Continuation token",
         "help": "token from a previous listing's output — feed it back in "
                 "to walk past the cap"},
    ),
))

register(Action(
    type="s3_read_object",
    label="S3: read object",
    icon="s3",
    description=("Download one object and stage the bytes for the steps that "
                 "follow (GetObject). Output: {filename, size, content_type, "
                 "bucket, key, source_bucket, source_key}; the key defaults to "
                 "the event's object key. Pair with a step whose source_s3 "
                 "takes templates to move the bytes elsewhere."),
    run=lambda action, event, workflow_id, steps=None: run_s3_read_object(
        action, event, steps=steps),
    required=frozenset({"bucket"}),
    optional=frozenset({"key", "credential_id", "connection_id"}),
    fields=(
        {"key": "credential_id", "label": "Credential ID", "placeholder": "aws (default)"},
        {"key": "bucket", "label": "Bucket", "placeholder": "datatalks-mailchimp-backup", "required": True,
         "discover": {"resource": "s3.buckets"}},
        {"key": "key", "label": "Object key", "placeholder": "defaults to the event's object key",
         "discover": {"resource": "s3.objects", "params": {"bucket": "bucket"}}},
    ),
))

register(Action(
    type="s3_presign_url",
    label="S3: presigned URL",
    icon="s3",
    description=("Mint a short-lived presigned download URL for one object "
                 "(presigned GET; one hour by default, up to seven days). "
                 "Output: {link, bucket, key, expires_in} — chain it before a "
                 "step whose source_url takes templates to hand a private "
                 "object to another pipeline. Computed, never sent: no "
                 "network call in this step."),
    run=lambda action, event, workflow_id, steps=None: run_s3_presign_url(
        action, event, steps=steps),
    required=frozenset({"bucket", "key"}),
    optional=frozenset({"expires_in", "credential_id", "connection_id"}),
    fields=(
        {"key": "credential_id", "label": "Credential ID", "placeholder": "aws (default)"},
        {"key": "bucket", "label": "Bucket", "placeholder": "datatalks-mailchimp-backup", "required": True,
         "discover": {"resource": "s3.buckets"}},
        {"key": "key", "label": "Object key", "placeholder": "reports/2026/report.pdf", "required": True,
         "discover": {"resource": "s3.objects", "params": {"bucket": "bucket"}}},
        {"key": "expires_in", "label": "Expires in (s)", "type": "number", "default": "3600",
         "help": "Seconds the link stays valid (capped at 604800 — SigV4's seven days)"},
    ),
))

register(Action(
    type="s3_delete_object",
    label="S3: delete object",
    icon="s3",
    description=("Delete one object from a bucket (DeleteObject). S3 deletes "
                 "are idempotent — removing an absent key succeeds — so the "
                 "output just names what was asked to be forgotten: "
                 "{deleted: true, bucket, key}."),
    run=lambda action, event, workflow_id, steps=None: run_s3_delete_object(
        action, event, steps=steps),
    required=frozenset({"bucket", "key"}),
    optional=frozenset({"credential_id", "connection_id"}),
    fields=(
        {"key": "credential_id", "label": "Credential ID", "placeholder": "aws (default)"},
        {"key": "bucket", "label": "Bucket", "placeholder": "datatalks-mailchimp-backup", "required": True,
         "discover": {"resource": "s3.buckets"}},
        {"key": "key", "label": "Object key", "placeholder": "tmp/{name}.pdf", "required": True,
         "discover": {"resource": "s3.objects", "params": {"bucket": "bucket"}}},
    ),
))


def _stored_keys(connection):
    """The (access_key_id, secret_access_key) pair behind the connection.

    Prefers the connection's own credential record and falls back to the
    shared ``aws`` credential, mirroring the s3_upload resolution.
    """
    from ..connections import credentials

    for credential_id in (connection.get("credential_id"), DEFAULT_CREDENTIAL_ID):
        if not credential_id:
            continue
        try:
            secret = credentials.get_credential(credential_id)
        except KeyError:
            continue
        if secret.get("access_key_id") and secret.get("secret_access_key"):
            return secret["access_key_id"], secret["secret_access_key"]
    raise RuntimeError("no stored AWS keys for this connection")


def _run_buckets(connection, params, *, transport=None):
    import boto3

    access_key, secret_key = _stored_keys(connection)
    client = boto3.client(
        "s3", aws_access_key_id=access_key, aws_secret_access_key=secret_key,
    )
    return [
        {"id": bucket["Name"], "name": bucket["Name"]}
        for bucket in client.list_buckets().get("Buckets", [])
        if bucket.get("Name")
    ]


register_discovery(Discovery(
    name="buckets",
    connector="s3",
    label="Buckets",
    description="S3 buckets reachable with the stored AWS keys",
    run=_run_buckets,
))


def _iso(value):
    """JSON-safe timestamp: boto3 datetimes render as ISO strings."""
    return value.isoformat() if hasattr(value, "isoformat") else str(value or "")


def _run_objects(connection, params, *, transport=None):
    """The keys in one bucket (under ``prefix`` when given), basenames as
    display names."""
    import boto3

    access_key, secret_key = _stored_keys(connection)
    client = boto3.client(
        "s3", aws_access_key_id=access_key, aws_secret_access_key=secret_key,
    )
    request = {"Bucket": params["bucket"], "MaxKeys": 100}
    prefix = str(params.get("prefix") or "").strip()
    if prefix:
        request["Prefix"] = prefix
    return [
        {"id": item["Key"], "name": item["Key"].rsplit("/", 1)[-1],
         "size": item.get("Size"), "modified": _iso(item.get("LastModified"))}
        for item in client.list_objects_v2(**request).get("Contents") or []
        if item.get("Key")
    ]


register_discovery(Discovery(
    name="objects",
    connector="s3",
    label="Bucket objects",
    description="Objects in an S3 bucket, up to 100 keys",
    params=(
        {"key": "bucket", "label": "Bucket", "type": "text", "required": True},
        {"key": "prefix", "label": "Key prefix", "type": "text",
         "help": "Only keys starting with this prefix"},
    ),
    run=_run_objects,
))


def _run_test(connection):
    """STS get-caller-identity against the stored AWS key pair."""
    import boto3

    try:
        access_key, secret_key = _stored_keys(connection)
        identity = boto3.client(
            "sts", aws_access_key_id=access_key, aws_secret_access_key=secret_key,
        ).get_caller_identity()
    except Exception as exc:
        return {"ok": False, "detail": f"AWS key check failed: {exc}"}
    arn = identity.get("Arn") or ""
    return {
        "ok": True,
        "detail": f"AWS keys verified ({arn})",
        "identity": {
            "account": identity.get("Account"),
            "arn": arn,
            "user_id": identity.get("UserId"),
        },
    }


register_connection_test(ConnectionTest(connector="aws", run=_run_test))


# --- trigger discovery: bucket options for the upload action's bucket field ----

from . import trigger_discovery
from .trigger_discovery import (
    DEFAULT_LIMIT,
    DiscoveryNotFound,
    TriggerDiscovery,
    register_trigger_discovery,
)


def _fetch_bucket_options(event=None, connection_id=None, limit=DEFAULT_LIMIT):
    """Bucket options via the registry listing (falls back to the shared
    ``aws`` credential when no connection is named)."""
    return trigger_discovery.options_from_registry(
        "s3.buckets", connection_id, limit,
        option_of=lambda item: {"value": item.get("name") or item.get("id"),
                                "label": item.get("name") or item.get("id")})


register_trigger_discovery(TriggerDiscovery(
    connector="s3", label="Amazon S3", kind="options", resource="s3.buckets",
    fetch=_fetch_bucket_options))


def _fetch_object_options(event=None, connection_id=None, limit=DEFAULT_LIMIT):
    """Object-key options for one bucket via the registry listing.

    The TriggerDiscovery fetch signature has no params field, so the bucket
    rides in ``event`` — the same slot the CLI's ``--event`` and the API
    body's ``event`` already fill. A missing bucket is 404, the same
    convention as poll's missing trigger name.
    """
    bucket = str(event or "").strip()
    if not bucket:
        raise DiscoveryNotFound(
            "bucket is required: pass the bucket name as 'event'")
    return trigger_discovery.options_from_registry(
        "s3.objects", connection_id, limit, params={"bucket": bucket},
        option_of=lambda item: {"value": item.get("id"),
                                "label": item.get("id") or item.get("name")})


register_trigger_discovery(TriggerDiscovery(
    connector="s3", label="Amazon S3", kind="options", resource="s3.objects",
    fetch=_fetch_object_options))


# --- poll source: "New File in S3 bucket" on the poll-trigger schedule ------
#
# The generic poll trigger fetches JSON over HTTP; ListObjectsV2 answers in
# XML. The ``s3`` source swaps the fetcher (``triggers.poll_sources``) and
# keeps the schedule, the cursor and the seen-set dedupe: each fire lists
# the bucket, objects strictly newer than the stored ``last_modified``
# watermark become ``s3``/``file.created`` events, and the first fire seeds
# the watermark without emitting — a new trigger must not fire on the
# bucket's whole history.

S3_POLL_MAX_KEYS = 1000


def _s3_poll_validate(body):
    """Save-time fetch spec: ``bucket`` (required), optional ``prefix`` and
    ``credential_id`` (the shared ``aws`` keys by default), with the fetch
    defaults a stored s3 poll carries."""
    from ..triggers.email_triggers import TriggerError

    body = body if isinstance(body, dict) else {}
    bucket = str(body.get("bucket") or "").strip()
    if not bucket:
        raise TriggerError("bucket is required: name the S3 bucket to watch")
    return {
        "bucket": bucket,
        "prefix": str(body.get("prefix") or "").strip(),
        "credential_id": str(body.get("credential_id") or "").strip()
                         or DEFAULT_CREDENTIAL_ID,
        "cursor_mode": "next_cursor",
        "id_path": "id",
        "url": "",
    }


def _s3_poll_client(item):
    """The boto3 client on the stored credential's key pair."""
    import boto3

    from ..connections import credentials

    credential_id = str(item.get("credential_id") or DEFAULT_CREDENTIAL_ID).strip()
    try:
        secret = credentials.get_credential(credential_id)
    except KeyError:
        raise RuntimeError(f"credential '{credential_id}' is not configured") from None
    access_key = secret.get("access_key_id")
    secret_key = secret.get("secret_access_key")
    if not access_key or not secret_key:
        raise RuntimeError(f"credential '{credential_id}' does not contain AWS keys")
    return boto3.client("s3", aws_access_key_id=access_key, aws_secret_access_key=secret_key)


def _s3_poll_objects(client, bucket, prefix):
    """The bucket's objects under ``prefix``, following continuation pages
    up to S3_POLL_MAX_KEYS — one page is enough for v1-sized buckets."""
    objects = []
    token = None
    while len(objects) < S3_POLL_MAX_KEYS:
        request = {"Bucket": bucket, "MaxKeys": S3_POLL_MAX_KEYS - len(objects)}
        if prefix:
            request["Prefix"] = prefix
        if token:
            request["ContinuationToken"] = token
        response = client.list_objects_v2(**request)
        for entry in response.get("Contents") or []:
            key = str(entry.get("Key") or "")
            if not key:
                continue
            modified = entry.get("LastModified")
            last_modified = modified.isoformat() if hasattr(modified, "isoformat") \
                else str(modified or "")
            objects.append({
                # The composite id keeps same-second uploads distinct; ISO
                # timestamps sort lexically = chronologically.
                "id": f"{last_modified}|{key}",
                "key": key,
                "size": entry.get("Size"),
                "last_modified": last_modified,
                "etag": entry.get("ETag"),
            })
        token = response.get("NextContinuationToken") if response.get("IsTruncated") else None
        if not token:
            break
    return objects


def _s3_poll_fetch(item, cursor=None):
    """One poll page as ``(items, next_cursor)`` for ``triggers.poll_sources``.

    With no stored cursor (first fire) the bucket's newest ``last_modified``
    seeds the watermark and nothing is emitted. With a cursor, only objects
    strictly newer fire — oldest first, so the parked cursor ends at the
    newest — and the cursor is the newest emitted ``last_modified`` (the
    incoming one when nothing qualifies; ``fire`` parks it only after the
    page drains). Raises ``RuntimeError`` on a failed fetch, like every
    poll source.
    """
    bucket = str(item.get("bucket") or "").strip()
    if not bucket:
        raise RuntimeError("poll source 's3' needs a stored bucket")
    prefix = str(item.get("prefix") or "").strip()
    try:
        objects = _s3_poll_objects(_s3_poll_client(item), bucket, prefix)
    except Exception as exc:
        raise RuntimeError(f"s3 list failed for bucket '{bucket}': "
                           f"{str(exc) or type(exc).__name__}") from exc
    newest = max((obj["last_modified"] for obj in objects), default=None)
    if cursor is None:
        return [], newest
    newer = sorted(
        (obj for obj in objects if obj["last_modified"] > str(cursor)),
        key=lambda obj: (obj["last_modified"], obj["key"]))
    return newer, (newer[-1]["last_modified"] if newer else str(cursor))


def _s3_poll_view(item):
    """The provider params ``public_view`` shows beside ``source``."""
    return {"bucket": item.get("bucket"), "prefix": item.get("prefix")}


register_source(PollSource(
    name="s3", connector="s3", event="file.created", label="S3",
    validate=_s3_poll_validate, fetch=_s3_poll_fetch, view=_s3_poll_view))


def _stored_s3_poll(name):
    """The stored poll trigger named by ``event`` when it watches S3, or
    None. A missing selector, unconfigured poll triggers, an unknown name
    and a non-s3 source (the generic poll connector owns those) all fold
    together: the caller only distinguishes live-vs-fallback."""
    from ..triggers import poll_triggers

    if not name:
        return None
    try:
        item = poll_triggers.get_item(name)
    except poll_triggers.TriggerError:
        return None
    if not item or str(item.get("source") or "") != "s3":
        return None
    return item


_S3_SYNTHETIC_OBJECT = {
    "bucket": "dapier-renders",
    "key": "invoices/4137.pdf",
    "size": 52310,
    "last_modified": "2026-09-28T09:14:03+00:00",
    "etag": "\"9b2cf535f27731c974343645a3985328\"",
}


def _fetch_s3_sample(event=None, connection_id=None, limit=DEFAULT_LIMIT):
    """The S3 chip's sample pull: the newest object a stored s3 poll would
    fire next (``source: "live"``), else the newest recorded s3 run
    (``"history"``), else a documented example (``"synthetic"``).

    ``event`` names the stored poll trigger; only ``source: "s3"`` polls
    qualify. The live pull runs the poll's own fetch once against the
    "everything" cursor ``""`` — every object's ``last_modified`` compares
    strictly newer, so the bucket's newest object comes back; no stored
    cursor is read or advanced (the sheets sample's ``"0"``, s3-shaped). A
    live fetch that cannot run — no stored AWS credential, the bucket
    unreachable, or the bucket empty — falls through to the
    recorded/documented sample instead of failing: a sample pull shows the
    payload shape, it never raises (see the audit note in
    docs/connector-coverage-audit.md about bare deploys).
    """
    from ..triggers import poll_triggers

    name = str(event or "").strip().lower()
    item = _stored_s3_poll(name)
    if item is not None:
        try:
            items = poll_triggers.fetch_page(item, cursor="")
            raw = next((entry for entry in items if entry is not None), None)
            envelope = poll_triggers.event_for(item, raw) if raw is not None else None
        except Exception:
            envelope = None
        if envelope is not None:
            return {
                "sample": trigger_discovery.as_sample(envelope),
                "source": "live",
                "connection_id": item.get("connection_id") or None,
            }
    found = trigger_discovery.history_sample("s3")
    if found is not None:
        return {"sample": found, "source": "history", "connection_id": connection_id}
    return {
        "sample": trigger_discovery.synthetic_sample(
            "s3", "file.created", dict(_S3_SYNTHETIC_OBJECT)),
        "source": "synthetic",
        "connection_id": connection_id,
    }


register_trigger_discovery(TriggerDiscovery(
    connector="s3", label="S3", kind="sample", resource="",
    fetch=_fetch_s3_sample))
