"""Daily DynamoDB snapshot to S3.

BackupFunction (template.yaml) invokes handler() on a daily schedule. Every
Dapier-owned table whose name reaches the function through the ``*_TABLE``
Globals is scanned fully and written to

    s3://<BackupBucket>/backups/<YYYY-MM-DD>/<table>.json.gz

as gzip-compressed NDJSON (one JSON item per line), followed by a
manifest.json summarising the day. The external ``TEAM_SECRETS_TABLE`` is
owned by another service and is excluded from these snapshots. The bucket's
lifecycle rule expires every object after 30 days, giving a rolling month of
daily snapshots.

Snapshots stream one scan page at a time into a temporary gzip file; peak
Python memory does not grow with table size. Finished gzip chunks stream
straight out as S3 multipart parts (put_object while the snapshot is
small), so a snapshot never has to fit in Lambda's /tmp, and a scan of a
multi-GB table only has to fit the function timeout — raised alongside
the transient slowness seen while a table's GSI backfill throttles
concurrent scans (2026-11-17/18: three 300 s timeout kills per day).

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
from tempfile import TemporaryFile

import boto3


def _dynamodb():
    return boto3.client("dynamodb")


def _s3():
    return boto3.client("s3")


# S3 multipart parts have a 5 MiB minimum (final part exempt); buffering a
# part at a time keeps peak memory at roughly one part plus one scan page.
MULTIPART_PART_SIZE = 8 * 1024 * 1024


class _Snapshot:
    """Lands a gzip stream in S3 as backups/<day>/<name>.json.gz.

    gzip output accumulates in a temporary file; once MULTIPART_PART_SIZE
    bytes have piled up they upload as the next multipart part, so the
    snapshot never has to fit in /tmp or memory. Small snapshots (the
    common case: every table except the inbox) skip multipart entirely and
    go out through the same single put_object as before.
    """

    def __init__(self, s3, bucket, key):
        self.s3, self.bucket, self.key = s3, bucket, key
        self.body = TemporaryFile()
        self._upload_id = None
        self._parts = []
        self._sent = 0  # bytes already uploaded as parts

    def flush(self):
        """Upload full part-size chunks; re-arm the write position at the end."""
        pending = self.body.tell() - self._sent
        while pending >= MULTIPART_PART_SIZE:
            if self._upload_id is None:
                self._upload_id = self.s3.create_multipart_upload(
                    Bucket=self.bucket, Key=self.key,
                    ContentType="application/x-ndjson",
                    ContentEncoding="gzip")["UploadId"]
            self.body.seek(self._sent)
            number = len(self._parts) + 1
            self._parts.append({
                "PartNumber": number,
                "ETag": self.s3.upload_part(
                    Bucket=self.bucket, Key=self.key, UploadId=self._upload_id,
                    PartNumber=number, Body=self.body.read(MULTIPART_PART_SIZE),
                )["ETag"],
            })
            self._sent += MULTIPART_PART_SIZE
            pending -= MULTIPART_PART_SIZE
        # gzip keeps appending at the file position; put it back at the end.
        self.body.seek(0, 2)

    def finish(self):
        """Close out the snapshot; returns its byte size."""
        try:
            if self._upload_id is None:
                self.body.seek(0)
                self.s3.put_object(
                    Bucket=self.bucket, Key=self.key, Body=self.body,
                    ContentType="application/x-ndjson", ContentEncoding="gzip",
                )
            else:
                self.body.seek(self._sent)
                self._parts.append({
                    "PartNumber": len(self._parts) + 1,
                    "ETag": self.s3.upload_part(
                        Bucket=self.bucket, Key=self.key, UploadId=self._upload_id,
                        PartNumber=len(self._parts) + 1, Body=self.body.read(),
                    )["ETag"],
                })
                self.s3.complete_multipart_upload(
                    Bucket=self.bucket, Key=self.key, UploadId=self._upload_id,
                    MultipartUpload={"Parts": self._parts})
        except BaseException:
            self.abort()
            raise
        return self.body.tell()

    def abort(self):
        if self._upload_id is not None:
            self.s3.abort_multipart_upload(
                Bucket=self.bucket, Key=self.key, UploadId=self._upload_id)
            self._upload_id = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.body.close()


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
    key = f"backups/{day}/{name}.json.gz"
    # Keep only a scan page, one encoded item and one part buffer in memory.
    # Building a list, joined JSON string and UTF-8 copy of the whole table
    # exhausted 256 MB. Disk-backed gzip avoids retaining the compressed
    # table in RAM; _Snapshot streams it out so it also never fills /tmp.
    count = 0
    with _Snapshot(s3, bucket, key) as snapshot:
        try:
            with gzip.GzipFile(fileobj=snapshot.body, mode="wb") as compressed:
                for page in dynamodb.get_paginator("scan").paginate(TableName=name):
                    for item in page.get("Items", []):
                        line = json.dumps(item, cls=_Encoder,
                                          separators=(",", ":")) + "\n"
                        compressed.write(line.encode("utf-8"))
                        count += 1
                    # zlib holds compressed output in its own buffers until
                    # it fills; sync-flush per page so the temp file shows
                    # the bytes and _Snapshot can judge a part boundary.
                    compressed.flush()
                    snapshot.flush()
            size = snapshot.finish()
        except BaseException:  # leave no orphaned multipart upload behind
            snapshot.abort()
            raise
    return {"key": key, "items": count, "bytes": size}


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
        {value for key, value in os.environ.items()
         if key.endswith("_TABLE") and key != "TEAM_SECRETS_TABLE" and value}
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
