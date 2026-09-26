import os
import re
from datetime import datetime, timezone

import boto3

CREDENTIAL_SPECS = {
    "slack": {"credential_id": "slack", "fields": ("token",)},
    "mailchimp": {"credential_id": "mailchimp", "fields": ("api_key",)},
    "aws": {"credential_id": "aws", "fields": ("access_key_id", "secret_access_key")},
}


class VersionConflict(Exception):
    """A conditional credential write lost a race with a newer version."""


def _table():
    return boto3.resource("dynamodb").Table(os.environ["CREDENTIALS_TABLE"])


def _bump(record):
    try:
        return int(record.get("version", 0)) + 1
    except (TypeError, ValueError):
        return 1


def put_credential(credential_id, value, *, provider):
    now = datetime.now(timezone.utc).isoformat()
    record = get_credential_record(credential_id)
    _table().put_item(Item={
        "credential_id": credential_id,
        "provider": provider,
        "value": value,
        "version": _bump(record),
        "updated_at": now,
    })
    return now


def get_credential_record(credential_id):
    return _table().get_item(Key={"credential_id": credential_id}).get("Item") or {}


def get_credential(credential_id):
    item = get_credential_record(credential_id)
    if not item or not isinstance(item.get("value"), dict):
        raise KeyError(credential_id)
    return item["value"]


def put_credential_if_version(credential_id, value, *, provider, expected_version):
    """Write only when the stored version still equals ``expected_version``.

    Legacy items without a version attribute count as version 0. Raises
    VersionConflict instead of overwriting a newer secret with stale state.
    """
    from botocore.exceptions import ClientError

    now = datetime.now(timezone.utc).isoformat()
    try:
        expected = int(expected_version)
    except (TypeError, ValueError):
        expected = 0
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
                "attribute_not_exists(credential_id) OR version = :expected "
                "OR (attribute_not_exists(version) AND :expected = :zero)"
            ),
            ExpressionAttributeValues={":expected": expected, ":zero": 0},
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise VersionConflict(credential_id)
        raise
    return now


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
    if provider == "slack":
        token = str(body.get("token", "")).strip()
        if not token.startswith(("xoxb-", "xapp-")) or len(token) < 20:
            return 400, {"error": "Enter a valid Slack bot token"}
        secret_value = {"token": token}
    elif provider == "aws":
        access_key_id = str(body.get("access_key_id", "")).strip()
        secret_access_key = str(body.get("secret_access_key", "")).strip()
        # ASIA covers temporary STS keys; AKIA/other long-lived prefixes are
        # 20 characters of uppercase letters and digits. Secrets are 40 chars
        # of base64 characters.
        if not re.fullmatch(r"[A-Za-z0-9]{16,128}", access_key_id):
            return 400, {"error": "Enter a valid AWS access key ID"}
        if not re.fullmatch(r"[A-Za-z0-9/+=]{40}", secret_access_key):
            return 400, {"error": "Enter a valid AWS secret access key"}
        secret_value = {"access_key_id": access_key_id, "secret_access_key": secret_access_key}
    else:
        api_key = str(body.get("api_key", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{20,}-us\d{1,3}", api_key):
            return 400, {"error": "Enter a valid Mailchimp API key"}
        secret_value = {"apiKey": api_key, "server": api_key.rsplit("-", 1)[1]}
    put_credential(spec["credential_id"], secret_value, provider=provider)
    return 200, {"provider": provider, "configured": True}
