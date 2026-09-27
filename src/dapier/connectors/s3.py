"""Amazon S3 connector: upload files to and find objects in a bucket with
stored AWS keys, plus bucket/object discovery and the stored-keys health
check."""
from ..engine.actions.s3 import DEFAULT_CREDENTIAL_ID, run_s3_find, run_s3_upload
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
    description="Find the first object matching a name pattern in a bucket (Find Object)",
    run=lambda action, event, workflow_id, steps=None: run_s3_find(action, event, steps=steps),
    required=frozenset({"bucket", "pattern"}),
    optional=frozenset({"prefix", "match", "credential_id", "connection_id"}),
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
