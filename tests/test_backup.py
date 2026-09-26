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
