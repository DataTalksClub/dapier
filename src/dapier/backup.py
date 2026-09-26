"""Daily DynamoDB snapshot to S3.

BackupFunction (template.yaml) invokes handler() on a daily schedule. Every
table whose name reaches the function through the ``*_TABLE`` Globals is
scanned fully and written to

    s3://<BackupBucket>/backups/<YYYY-MM-DD>/<table>.json.gz

as gzip-compressed NDJSON (one JSON item per line), followed by a
manifest.json summarising the day. The bucket's lifecycle rule expires
every object after 30 days, giving a rolling month of daily snapshots.

Restoring replays a snapshot's lines back into a table with the same key
schema:

    import gzip, json, boto3

    table = boto3.resource("dynamodb").Table("dapier-ConnectionsTable-...")
    for line in gzip.open("dapier-ConnectionsTable-x.json.gz", "rt"):
        table.put_item(Item=json.loads(line))
"""
import base64
import gzip
import json
import os
from datetime import datetime, timezone
from decimal import Decimal

import boto3


def _dynamodb():
    return boto3.client("dynamodb")


def _s3():
    return boto3.client("s3")


class _Encoder(json.JSONEncoder):
    """DynamoDB item values that are not native JSON: numbers, sets, binary."""

    def default(self, o):
        if isinstance(o, Decimal):
            return int(o) if o == o.to_integral() else float(o)
        if isinstance(o, (set, frozenset)):
            return list(o)
        if isinstance(o, (bytes, bytearray)):
            return base64.b64encode(bytes(o)).decode("ascii")
        return super().default(o)


def backup_table(dynamodb, s3, name, bucket, day):
    """Scan one table into backups/<day>/<name>.json.gz and return its stats."""
    items = []
    for page in dynamodb.get_paginator("scan").paginate(TableName=name):
        items.extend(page.get("Items", []))
    payload = "".join(
        json.dumps(item, cls=_Encoder, separators=(",", ":")) + "\n" for item in items
    )
    body = gzip.compress(payload.encode("utf-8"))
    key = f"backups/{day}/{name}.json.gz"
    s3.put_object(
        Bucket=bucket, Key=key, Body=body,
        ContentType="application/x-ndjson", ContentEncoding="gzip",
    )
    return {"key": key, "items": len(items), "bytes": len(body)}


def handler(event, context):
    bucket = os.environ["BACKUP_BUCKET"]
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    names = sorted(
        {value for key, value in os.environ.items() if key.endswith("_TABLE") and value}
    )
    dynamodb, s3 = _dynamodb(), _s3()
    results, errors = {}, {}
    for name in names:
        try:
            results[name] = backup_table(dynamodb, s3, name, bucket, day)
        except Exception as exc:  # keep backing up the other tables
            errors[name] = str(exc)
    manifest = {
        "date": day,
        "tables": {**results, **{name: {"error": err} for name, err in errors.items()}},
    }
    s3.put_object(
        Bucket=bucket, Key=f"backups/{day}/manifest.json",
        Body=json.dumps(manifest, indent=2), ContentType="application/json",
    )
    print(
        f"backup {day}: {sum(r['items'] for r in results.values())} items "
        f"across {len(results)}/{len(names)} tables"
    )
    if errors:
        raise RuntimeError(f"backup failed for: {', '.join(sorted(errors))}")
    return manifest
