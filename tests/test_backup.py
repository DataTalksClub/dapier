import gzip
import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

import src.dapier.backup as backup


class FakeDynamo:
    def __init__(self, tables):
        self._tables = tables

    def get_paginator(self, operation):
        assert operation == "scan"
        return self

    def paginate(self, TableName=None, **_):
        if TableName not in self._tables:
            raise RuntimeError(f"scan failed for {TableName}")
        return [{"Items": self._tables[TableName]}]


class FakeS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, **kwargs):
        self.objects[Key] = Body


class BrokenManifestS3(FakeS3):
    """The bucket itself rejects writes — the S3-side catastrophe case."""

    def put_object(self, Bucket, Key, Body, **kwargs):
        if "manifest.json" in Key:
            raise RuntimeError("The specified bucket does not exist")
        self.objects[Key] = Body


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("BACKUP_BUCKET", "dapier-backups-test")
    monkeypatch.setenv("CONNECTIONS_TABLE", "dapier-ConnectionsTable-x")
    monkeypatch.setenv("CREDENTIALS_TABLE", "dapier-CredentialsTable-x")
    monkeypatch.setattr(backup, "_claim_email", lambda day: True)
    monkeypatch.setattr(backup, "_release_email", lambda day: None)
    monkeypatch.delenv("BACKUP_ALERT_EMAIL", raising=False)


def test_backup_writes_ndjson_and_manifest(env, monkeypatch):
    items = [
        {"connection_id": "c1", "scopes": {"read", "write"}, "count": Decimal("3"),
         "ratio": Decimal("2.5")},
        {"connection_id": "c2", "count": Decimal("0")},
    ]
    fake_s3 = FakeS3()
    monkeypatch.setattr(backup, "_dynamodb", lambda: FakeDynamo({
        "dapier-ConnectionsTable-x": items,
        "dapier-CredentialsTable-x": [],
    }))
    monkeypatch.setattr(backup, "_s3", lambda: fake_s3)

    manifest = backup.handler({}, {})

    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert manifest["tables"]["dapier-ConnectionsTable-x"]["items"] == 2
    assert manifest["tables"]["dapier-CredentialsTable-x"]["items"] == 0

    snapshot = gzip.decompress(
        fake_s3.objects[f"backups/{day}/dapier-ConnectionsTable-x.json.gz"]
    )
    restored = [json.loads(line) for line in snapshot.decode().splitlines()]
    assert restored[0]["connection_id"] == "c1"
    assert restored[0]["count"] == 3
    assert restored[0]["ratio"] == 2.5
    assert set(restored[0]["scopes"]) == {"read", "write"}
    # An empty table still gets a (valid, empty) snapshot file.
    empty = gzip.decompress(
        fake_s3.objects[f"backups/{day}/dapier-CredentialsTable-x.json.gz"]
    )
    assert empty == b""
    assert manifest["date"] == json.loads(
        fake_s3.objects[f"backups/{day}/manifest.json"]
    )["date"]


def test_backup_raises_after_attempting_every_table(env, monkeypatch):
    posts = []
    monkeypatch.setattr(
        backup, "_send_email",
        lambda payload: posts.append(payload),
    )
    monkeypatch.setenv("BACKUP_ALERT_EMAIL", "ops@example.com")
    fake_s3 = FakeS3()
    monkeypatch.setattr(backup, "_dynamodb", lambda: FakeDynamo({
        "dapier-ConnectionsTable-x": [{"connection_id": "c1"}],
        "dapier-CredentialsTable-x": None,  # paginate raises
    }))
    monkeypatch.setattr(backup, "_s3", lambda: fake_s3)

    with pytest.raises(RuntimeError, match="Credentials"):
        backup.handler({}, {})

    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # The healthy table was still backed up before the raise.
    assert f"backups/{day}/dapier-ConnectionsTable-x.json.gz" in fake_s3.objects
    manifest = json.loads(fake_s3.objects[f"backups/{day}/manifest.json"])
    assert "error" in manifest["tables"]["dapier-CredentialsTable-x"]

    # The failure was reported by email, with the failed table and a per-day
    # idempotency key so re-invocations don't spam.
    assert len(posts) == 1
    payload = posts[0]
    assert payload["email"] == "ops@example.com"
    assert payload["template_key"] == "cli-message"
    assert payload["idempotency_key"] == f"dapier-backup-failure-{day}"
    assert "1 of 2 tables" in payload["context"]["subject"]
    assert "dapier-CredentialsTable-x" in payload["context"]["body"]


def test_successful_backup_sends_no_email(env, monkeypatch):
    posts = []
    monkeypatch.setattr(
        backup, "_send_email",
        lambda payload: posts.append(payload),
    )
    monkeypatch.setenv("BACKUP_ALERT_EMAIL", "ops@example.com")
    monkeypatch.setattr(backup, "_dynamodb", lambda: FakeDynamo({
        "dapier-ConnectionsTable-x": [{"connection_id": "c1"}],
        "dapier-CredentialsTable-x": [],
    }))
    monkeypatch.setattr(backup, "_s3", lambda: FakeS3())

    backup.handler({}, {})

    assert posts == []  # failure-only, like rds-export's notify_run.sh


def test_failure_email_is_skipped_when_unconfigured_and_never_raises(env, monkeypatch):
    def broken_post(payload):
        raise OSError("no network")

    monkeypatch.setattr(backup, "_send_email", broken_post)
    monkeypatch.setenv("BACKUP_ALERT_EMAIL", "ops@example.com")
    monkeypatch.setattr(backup, "_dynamodb", lambda: FakeDynamo({
        "dapier-ConnectionsTable-x": None,
    }))
    monkeypatch.setattr(backup, "_s3", lambda: FakeS3())

    # A broken mailer must not mask the original backup error...
    with pytest.raises(RuntimeError, match="Connections"):
        backup.handler({}, {})


def test_failure_email_skipped_without_config(env, monkeypatch):
    posts = []
    monkeypatch.setattr(
        backup, "_send_email",
        lambda payload: posts.append(payload),
    )
    # env fixture removes BACKUP_ALERT_EMAIL.
    monkeypatch.setattr(backup, "_dynamodb", lambda: FakeDynamo({
        "dapier-ConnectionsTable-x": None,
    }))
    monkeypatch.setattr(backup, "_s3", lambda: FakeS3())

    with pytest.raises(RuntimeError):
        backup.handler({}, {})

    assert posts == []  # only the CloudWatch alarm remains


def test_manifest_failure_also_triggers_the_email(env, monkeypatch):
    """If even the manifest can't be written (bucket gone), the email must
    still go out — this is exactly the all-bets-are-off case."""
    posts = []
    monkeypatch.setattr(
        backup, "_send_email",
        lambda payload: posts.append(payload),
    )
    monkeypatch.setenv("BACKUP_ALERT_EMAIL", "ops@example.com")
    monkeypatch.setattr(backup, "_dynamodb", lambda: FakeDynamo({
        "dapier-ConnectionsTable-x": [],
        "dapier-CredentialsTable-x": [],
    }))
    monkeypatch.setattr(backup, "_s3", lambda: BrokenManifestS3())

    with pytest.raises(RuntimeError, match="manifest.json"):
        backup.handler({}, {})

    assert len(posts) == 1
    payload = posts[0]
    assert "0 of 2 tables" in payload["context"]["subject"]
    assert "manifest.json" in payload["context"]["body"]
    assert "The specified bucket does not exist" in payload["context"]["body"]


def test_failure_email_does_not_send_when_already_claimed(env, monkeypatch):
    posts = []
    monkeypatch.setenv("BACKUP_ALERT_EMAIL", "ops@example.com")
    monkeypatch.setattr(backup, "_claim_email", lambda day: False)
    monkeypatch.setattr(backup, "_send_email", posts.append)
    backup.notify_failure("2026-10-13", {}, {"table": "failed"})
    assert posts == []


def test_failed_email_releases_the_daily_claim(env, monkeypatch):
    released = []
    monkeypatch.setenv("BACKUP_ALERT_EMAIL", "ops@example.com")
    monkeypatch.setattr(backup, "_release_email", released.append)
    monkeypatch.setattr(backup, "_send_email", lambda payload: (_ for _ in ()).throw(OSError("SES unavailable")))
    backup.notify_failure("2026-10-13", {}, {"table": "failed"})
    assert released == ["2026-10-13"]


def test_backup_email_uses_iam_ses_without_a_mailer_key(monkeypatch):
    from src.dapier.engine import notify
    calls = []
    class Ses:
        def send_email(self, **kwargs):
            calls.append(kwargs)
            return {"MessageId": "sent"}
    monkeypatch.setattr(notify, "ses_client", lambda: Ses())
    monkeypatch.setenv("DAPIER_EMAIL_SENDER", "dapier@example.com")
    backup._send_email({"email": "ops@example.com", "context": {"subject": "failure", "body": "details"}})
    assert calls[0]["Source"] == "dapier@example.com"
    assert calls[0]["Destination"] == {"ToAddresses": ["ops@example.com"]}
    assert calls[0]["Message"]["Body"]["Text"]["Data"] == "details"


def test_daily_claim_is_atomic_and_duplicate_is_skipped(monkeypatch):
    from botocore.exceptions import ClientError
    calls = []
    class Table:
        def put_item(self, **kwargs):
            calls.append(kwargs)
            if len(calls) > 1:
                raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
    class Dynamo:
        def Table(self, name):
            assert name == "executions"
            return Table()
    monkeypatch.setenv("EXECUTIONS_TABLE", "executions")
    monkeypatch.setattr(backup.boto3, "resource", lambda name: Dynamo())
    assert backup._claim_email("2026-10-13") is True
    assert backup._claim_email("2026-10-13") is False
    assert calls[0]["ConditionExpression"] == "attribute_not_exists(execution_id)"
    assert calls[0]["Item"]["execution_id"] == "backup-failure-notice:2026-10-13"
