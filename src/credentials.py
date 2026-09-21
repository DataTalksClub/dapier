import os
from datetime import datetime, timezone

import boto3


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
