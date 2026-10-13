"""Daily DynamoDB snapshot to S3.

BackupFunction (template.yaml) invokes handler() on a daily schedule. Every
table whose name reaches the function through the ``*_TABLE`` Globals is
scanned fully and written to

    s3://<BackupBucket>/backups/<YYYY-MM-DD>/<table>.json.gz

as gzip-compressed NDJSON (one JSON item per line), followed by a
manifest.json summarising the day. The bucket's lifecycle rule expires
every object after 30 days, giving a rolling month of daily snapshots.

A failed run also emails the operator through the AWS mail
service SES with the function's IAM role: report failures, stay silent on
success. No mailer API key is needed. Without BACKUP_ALERT_EMAIL the email
is skipped and only the CloudWatch alarm remains.

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


def _claim_email(day):
    """One failure email per backup date, including Lambda retries."""
    from botocore.exceptions import ClientError

    try:
        boto3.resource("dynamodb").Table(os.environ["EXECUTIONS_TABLE"]).put_item(
            Item={"execution_id": f"backup-failure-notice:{day}",
                  "kind": "backup-failure-notice", "status": "notified",
                  "expires_at": int(datetime.now(timezone.utc).timestamp()) + 90 * 86400},
            ConditionExpression="attribute_not_exists(execution_id)",
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise
    return True


def _release_email(day):
    boto3.resource("dynamodb").Table(os.environ["EXECUTIONS_TABLE"]).delete_item(
        Key={"execution_id": f"backup-failure-notice:{day}"})


def _send_email(payload):
    from .engine.notify import operator_sender, ses_client

    return ses_client().send_email(
        Source=operator_sender(),
        Destination={"ToAddresses": [payload["email"]]},
        Message={"Subject": {"Data": payload["context"]["subject"], "Charset": "utf-8"},
                 "Body": {"Text": {"Data": payload["context"]["body"], "Charset": "utf-8"}}},
    )


def notify_failure(day, results, errors, manifest_error=None):
    """Email the failure report; never raises (best-effort like notify_run.sh)."""
    recipient = os.environ.get("BACKUP_ALERT_EMAIL", "")
    if not recipient:
        print("backup failure email skipped: recipient is not configured")
        return
    failed = "\n".join(f"  {name}: {err}" for name, err in sorted(errors.items()))
    backed_up = "\n".join(
        f"  {name}: {stats['items']} items" for name, stats in sorted(results.items())
    ) or "  (none)"
    if manifest_error:
        failed += f"\n  manifest.json: {manifest_error}"
    body = (
        f"Something didn't work — the dapier DynamoDB backup failed.\n"
        f"\n"
        f"Date: {day}\n"
        f"Failed tables ({len(errors)} of {len(results) + len(errors)}):\n"
        f"{failed}\n"
        f"\n"
        f"Tables that were still backed up ({len(results)}):\n"
        f"{backed_up}\n"
        f"\n"
        f"Snapshots of the healthy tables are in s3://{os.environ['BACKUP_BUCKET']}/"
        f"backups/{day}/. Retry with:\n"
        f"  aws lambda invoke --function-name $BACKUP_FUNCTION_NAME out.json"
    )
    payload = {
        "email": recipient,
        "template_key": "cli-message",
        "context": {
            "subject": f"Backup failure: dapier dynamodb "
                       f"({len(errors)} of {len(results) + len(errors)} tables)",
            "body": body,
        },
        # Retries/manual re-invocations on the same day don't re-send.
        "idempotency_key": f"dapier-backup-failure-{day}",
    }
    try:
        if not _claim_email(day):
            return
        try:
            _send_email(payload)
        except Exception:
            _release_email(day)
            raise
        print(f"backup failure email sent to {recipient}")
    except Exception as exc:  # the alarm still fires on the raise below
        print(f"backup failure email could not be sent: {exc}")


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
    manifest_error = None
    try:
        s3.put_object(
            Bucket=bucket, Key=f"backups/{day}/manifest.json",
            Body=json.dumps(manifest, indent=2), ContentType="application/json",
        )
    except Exception as exc:  # e.g. the bucket itself is gone — report, don't die
        manifest_error = str(exc)
    print(
        f"backup {day}: {sum(r['items'] for r in results.values())} items "
        f"across {len(results)}/{len(names)} tables"
    )
    if errors or manifest_error:
        notify_failure(day, results, errors, manifest_error)
        detail = ", ".join(sorted(errors)) or f"manifest.json ({manifest_error})"
        raise RuntimeError(f"backup failed for: {detail}")
    return manifest
