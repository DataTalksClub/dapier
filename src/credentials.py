import os
from datetime import datetime, timezone

import boto3


def _table():
    return boto3.resource("dynamodb").Table(os.environ["CREDENTIALS_TABLE"])


def put_credential(credential_id, value, *, provider):
    now = datetime.now(timezone.utc).isoformat()
    _table().put_item(Item={
        "credential_id": credential_id,
        "provider": provider,
        "value": value,
        "updated_at": now,
    })
    return now


def get_credential(credential_id):
    item = _table().get_item(Key={"credential_id": credential_id}).get("Item")
    if not item or not isinstance(item.get("value"), dict):
        raise KeyError(credential_id)
    return item["value"]


def credential_status(credential_id):
    item = _table().get_item(
        Key={"credential_id": credential_id},
        ProjectionExpression="credential_id, updated_at",
    ).get("Item")
    return {
        "configured": item is not None,
        "updated_at": item.get("updated_at") if item else None,
    }
