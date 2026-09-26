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


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("BACKUP_BUCKET", "dapier-backups-test")
    monkeypatch.setenv("CONNECTIONS_TABLE", "dapier-ConnectionsTable-x")
    monkeypatch.setenv("CREDENTIALS_TABLE", "dapier-CredentialsTable-x")
    monkeypatch.delenv("DATAMAILER_URL", raising=False)
    monkeypatch.delenv("DATAMAILER_API_KEY", raising=False)
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
        backup, "_post_json",
        lambda url, key, payload: posts.append((url, key, payload)),
    )
    monkeypatch.setenv("DATAMAILER_URL", "https://datamailer.example")
    monkeypatch.setenv("DATAMAILER_API_KEY", "dm-key")
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
    url, api_key, payload = posts[0]
    assert url == "https://datamailer.example/api/transactional/send"
    assert api_key == "dm-key"
    assert payload["email"] == "ops@example.com"
    assert payload["template_key"] == "cli-message"
    assert payload["idempotency_key"] == f"dapier-backup-failure-{day}"
    assert "1 of 2 tables" in payload["context"]["subject"]
    assert "dapier-CredentialsTable-x" in payload["context"]["body"]


def test_successful_backup_sends_no_email(env, monkeypatch):
    posts = []
    monkeypatch.setattr(
        backup, "_post_json",
        lambda url, key, payload: posts.append(payload),
    )
    monkeypatch.setenv("DATAMAILER_URL", "https://datamailer.example")
    monkeypatch.setenv("DATAMAILER_API_KEY", "dm-key")
    monkeypatch.setenv("BACKUP_ALERT_EMAIL", "ops@example.com")
    monkeypatch.setattr(backup, "_dynamodb", lambda: FakeDynamo({
        "dapier-ConnectionsTable-x": [{"connection_id": "c1"}],
        "dapier-CredentialsTable-x": [],
    }))
    monkeypatch.setattr(backup, "_s3", lambda: FakeS3())

    backup.handler({}, {})

    assert posts == []  # failure-only, like rds-export's notify_run.sh


def test_failure_email_is_skipped_when_unconfigured_and_never_raises(env, monkeypatch):
    def broken_post(url, key, payload):
        raise OSError("no network")

    monkeypatch.setattr(backup, "_post_json", broken_post)
    monkeypatch.setenv("DATAMAILER_URL", "https://datamailer.example")
    monkeypatch.setenv("DATAMAILER_API_KEY", "dm-key")
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
        backup, "_post_json",
        lambda url, key, payload: posts.append(payload),
    )
    # env fixture removes the DATAMAILER_* / BACKUP_ALERT_EMAIL vars.
    monkeypatch.setattr(backup, "_dynamodb", lambda: FakeDynamo({
        "dapier-ConnectionsTable-x": None,
    }))
    monkeypatch.setattr(backup, "_s3", lambda: FakeS3())

    with pytest.raises(RuntimeError):
        backup.handler({}, {})

    assert posts == []  # only the CloudWatch alarm remains
