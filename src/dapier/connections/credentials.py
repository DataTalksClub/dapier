import os
import re
from datetime import datetime, timezone

import boto3

CREDENTIAL_SPECS = {
    "typefully": {"credential_id": "typefully", "fields": ("api_key",)},
    "taskdeck": {"credential_id": "taskdeck", "fields": ("token",)},
    "slack": {"credential_id": "slack", "fields": ("token",)},
    "aws": {"credential_id": "aws", "fields": ("role_arn",)},
    "openrouter": {"credential_id": "openrouter", "fields": ("api_key",)},
}


class VersionConflict(Exception):
    """A conditional credential write lost a race with a newer version."""


def _table():
    return boto3.resource("dynamodb").Table(os.environ["CREDENTIALS_TABLE"])


def put_credential(credential_id, value, *, provider):
    now = datetime.now(timezone.utc).isoformat()
    record = get_credential_record(credential_id)
    _table().put_item(Item={
        "credential_id": credential_id,
        "provider": provider,
        "value": value,
        "version": int(record["version"]) + 1 if record else 1,
        "updated_at": now,
    })
    return now


def get_credential_record(credential_id):
    return _table().get_item(Key={"credential_id": credential_id}).get("Item") or {}


def delete_credential(credential_id):
    """Remove the record outright, secret and all — the delete-a-connection
    path uses this so no orphaned credential outlives its connection."""
    _table().delete_item(Key={"credential_id": credential_id})


def get_credential(credential_id):
    item = get_credential_record(credential_id)
    if not item or not isinstance(item.get("value"), dict):
        raise KeyError(credential_id)
    return item["value"]


def put_credential_if_version(credential_id, value, *, provider, expected_version):
    """Write only when the stored version still equals ``expected_version``.

    Raises VersionConflict instead of overwriting a newer secret with stale
    state.
    """
    from botocore.exceptions import ClientError

    now = datetime.now(timezone.utc).isoformat()
    expected = int(expected_version)
    try:
        _table().put_item(
            Item={
                "credential_id": credential_id,
                "provider": provider,
                "value": value,
                "version": expected + 1,
                "updated_at": now,
            },
            ConditionExpression=(
                "attribute_not_exists(credential_id) OR version = :expected"
            ),
            ExpressionAttributeValues={":expected": expected},
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise VersionConflict(credential_id)
        raise
    return now


def credential_shapes():
    """Every stored credential's key names — shared and connection-owned.

    Values are read only to list their key names; the result never carries
    one. Operators use this to verify secret-shape migrations.
    """
    items = _table().scan(
        ProjectionExpression="credential_id, provider, #v, updated_at, #val",
        ExpressionAttributeNames={"#v": "version", "#val": "value"},
    ).get("Items", [])
    shapes = [
        {
            "credential_id": item.get("credential_id"),
            "provider": item.get("provider"),
            "keys": sorted(item["value"]) if isinstance(item.get("value"), dict) else [],
            "version": item.get("version"),
            "updated_at": item.get("updated_at"),
        }
        for item in items
    ]
    shapes.sort(key=lambda shape: shape["credential_id"] or "")
    return shapes


def credential_status(credential_id):
    item = _table().get_item(
        Key={"credential_id": credential_id},
        ProjectionExpression="credential_id, updated_at",
    ).get("Item")
    return {
        "configured": item is not None,
        "updated_at": item.get("updated_at") if item else None,
    }


def api_save_credential(provider, body):
    """Validate and store one provider credential. Returns ``(status, payload)``.

    Shared by the console (cookie) and CLI (bearer) API layers; the value is
    write-only — responses never echo it back.
    """
    spec = CREDENTIAL_SPECS.get(provider)
    if not spec:
        return 404, {"error": "Unknown credential provider"}
    if provider == "taskdeck":
        token = str(body.get("token", "")).strip()
        if len(token) < 32 or any(char.isspace() for char in token):
            return 400, {"error": "Use a Taskdeck bearer token of at least 32 characters with no whitespace"}
        secret_value = {"token": token}
    elif provider == "slack":
        token = str(body.get("token", "")).strip()
        if not token.startswith(("xoxb-", "xapp-")) or len(token) < 20:
            return 400, {"error": "Enter a valid Slack bot token"}
        secret_value = {"token": token}
    elif provider == "aws":
        if body.get("access_key_id") or body.get("secret_access_key"):
            return 400, {"error": "Configure AWS with an IAM role; access keys are no longer accepted"}
        from .aws import allowed_role_arns

        role_arn = str(body.get("role_arn", "")).strip()
        if not role_arn:
            return 400, {"error": "Enter a valid IAM role ARN"}
        if not re.fullmatch(r"arn:aws(?:-us-gov|-cn)?:iam::\d{12}:role/[A-Za-z0-9+=,.@_/-]+", role_arn):
            return 400, {"error": "Enter a valid IAM role ARN"}
        if role_arn not in allowed_role_arns():
            return 400, {"error": "AWS role is not in the deployment's allowed role list"}
        secret_value = {"role_arn": role_arn}
        for field in ("external_id", "region"):
            value = body.get(field)
            if value:
                if not isinstance(value, str):
                    return 400, {"error": f"{field} must be a string"}
                secret_value[field] = value.strip()
        if secret_value.get("region") and not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d+", secret_value["region"]):
            return 400, {"error": "Enter a valid AWS region"}
        if secret_value.get("external_id") and not re.fullmatch(r"[A-Za-z0-9_+=,.@:/-]{2,1224}", secret_value["external_id"]):
            return 400, {"error": "Enter a valid STS external ID"}
        buckets = body.get("buckets")
        if buckets is not None:
            if not isinstance(buckets, list) or not all(
                isinstance(bucket, str) and re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket)
                for bucket in buckets
            ):
                return 400, {"error": "buckets must be a list of S3 bucket names"}
            secret_value["buckets"] = buckets
    elif provider == "typefully":
        api_key = body.get("api_key")
        if not isinstance(api_key, str) or not re.fullmatch(r"[!-~]{20,2048}", api_key.strip()):
            return 400, {"error": "Enter a valid Typefully API key"}
        secret_value = {"api_key": api_key.strip()}
    else:
        api_key = str(body.get("api_key", "")).strip()
        if not re.fullmatch(r"sk-or-[A-Za-z0-9_-]{20,}", api_key):
            return 400, {"error": "Enter a valid OpenRouter API key (sk-or-...)"}
        secret_value = {"api_key": api_key}
    put_credential(spec["credential_id"], secret_value, provider=provider)
    return 200, {"provider": provider, "configured": True}
