"""The S3 poll source: a bucket listing as poll items — the boto3-backed
fetch for the JSON-only generic poller (docs/connector-coverage-audit.md
gap 3, "no S3 trigger").

Covers the save-time shape (bucket/prefix/credential_id validate), the
boto3 list (JSON items: key, size, last_modified, etag), the first-fire
seeding that keeps a new trigger off the bucket's history, strictly-newer
emission, and the seen-store dedupe of a recycled page. The bucket is moto's;
the stored AWS keys are stubbed at the credential store, where the connector
resolves them.
"""
import json
import re
import time
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

from src.dapier.triggers import poll_sources, poll_triggers
from src.dapier.triggers.email_triggers import TriggerError

ACTIONS = [{"type": "email_send", "to": "dest@example.test"}]

BUCKET = "watched"


class FakePollTable:
    """DynamoDB stand-in keyed by poll_id, holding the stored trigger."""

    def __init__(self, item=None):
        self.items = {}
        if item is not None:
            self.items[item["poll_id"]] = dict(item)

    def get_item(self, Key):
        item = self.items.get(Key["poll_id"])
        return {"Item": dict(item)} if item else {}


class FakeCursorTable:
    """DynamoDB stand-in keyed by cursor_id (poll cursors) or scope_id
    (the seen store's per-trigger sets, triggers.seen)."""

    def __init__(self):
        self.items = {}

    @staticmethod
    def _partition(key_or_item):
        return (key_or_item.get("cursor_id")
                if "cursor_id" in key_or_item else key_or_item.get("scope_id"))

    def get_item(self, Key):
        item = self.items.get(self._partition(Key))
        return {"Item": dict(item)} if item else {}

    def put_item(self, Item):
        self.items[self._partition(Item)] = dict(Item)

    def delete_item(self, Key):
        self.items.pop(self._partition(Key), None)


def build(name="s3-watch", **body):
    body.setdefault("actions", [dict(ACTIONS[0])])
    body["source"] = "s3"
    body["expression"] = "rate(5 minutes)"
    return poll_triggers.build_item({"name": name, **body}, "op@example.test")


@pytest.fixture
def aws_keys(monkeypatch):
    """The stored key pair the connector resolves from the credential store."""
    monkeypatch.setattr(
        "src.dapier.connections.credentials.get_credential",
        lambda credential_id: {"access_key_id": "testing",
                               "secret_access_key": "testing"})


def run_fire(item, *, cursors, fail_on=None):
    """One fire() against moto with real cursor/seen machinery."""
    fired_events = []
    polls = FakePollTable(item)

    def fake_execute(event, **_kwargs):
        if fail_on is not None and event["data"]["item_id"] == fail_on:
            raise RuntimeError("downstream exploded")
        fired_events.append(event)

    with patch("src.dapier.engine.execute", side_effect=fake_execute), \
         patch("src.dapier.engine.notify.notify_failure"):
        result = poll_triggers.fire(item["poll_id"], table_ref=polls,
                                    cursor_table_ref=cursors)

    return result, fired_events


# --- save-time shape --------------------------------------------------------------


class TestS3Save:
    def test_an_s3_poll_builds_a_seen_store_poll(self):
        item = build(bucket=BUCKET, prefix="inbox/")

        assert item["source"] == "s3"
        assert item["bucket"] == BUCKET
        assert item["prefix"] == "inbox/"
        assert item["credential_id"] == "aws"  # the shared keys by default
        assert item["cursor_mode"] == "next_cursor"
        assert item["url"] == ""

    def test_prefix_and_credential_are_optional(self):
        item = build(bucket=BUCKET)

        assert item["prefix"] == ""
        assert item["credential_id"] == "aws"

    def test_the_bucket_is_required(self):
        with pytest.raises(TriggerError, match="bucket"):
            build()

    def test_a_named_credential_is_kept(self):
        item = build(bucket=BUCKET, credential_id="backup-keys")

        assert item["credential_id"] == "backup-keys"

    def test_the_view_shows_the_watched_bucket(self):
        view = poll_triggers.public_view(build(bucket=BUCKET, prefix="inbox/"))

        assert view["source"] == "s3"
        assert view["bucket"] == BUCKET
        assert view["prefix"] == "inbox/"


# --- the boto3 fetch ----------------------------------------------------------------


class TestS3Fetch:
    @mock_aws
    def test_the_listing_becomes_json_items(self, aws_keys):
        client = boto3.client("s3", region_name="eu-west-1")
        client.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "eu-west-1"})
        client.put_object(Bucket=BUCKET, Key="inbox/a.txt", Body=b"hello")
        client.put_object(Bucket=BUCKET, Key="inbox/b.csv", Body=b"a,b,c")

        item = build(bucket=BUCKET, prefix="inbox/")
        # A watch already seeded: everything in the bucket is newer than an
        # old watermark.
        items, _next = poll_triggers.fetch_page_response(
            item, cursor="2000-01-01T00:00:00+00:00")

        assert [entry["key"] for entry in items] == ["inbox/a.txt", "inbox/b.csv"]
        for entry in items:
            assert entry["size"] > 0
            assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T[\d:.]+\+00:00",
                                entry["last_modified"])
            assert entry["etag"]
            assert entry["id"] == f"{entry['last_modified']}|{entry['key']}"

    @mock_aws
    def test_the_prefix_limits_the_watch(self, aws_keys):
        client = boto3.client("s3", region_name="eu-west-1")
        client.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "eu-west-1"})
        client.put_object(Bucket=BUCKET, Key="other/hidden.txt", Body=b"x")
        client.put_object(Bucket=BUCKET, Key="inbox/seen.txt", Body=b"x")

        items, _next = poll_triggers.fetch_page_response(
            build(bucket=BUCKET, prefix="inbox/"), cursor="2000-01-01T00:00:00+00:00")

        assert [entry["key"] for entry in items] == ["inbox/seen.txt"]

    @mock_aws
    def test_a_failed_list_is_a_runtime_error(self, aws_keys):
        with pytest.raises(RuntimeError, match="s3 list failed"):
            poll_triggers.fetch_page(build(bucket="no-such-bucket-here"))

    @mock_aws
    def test_a_missing_credential_is_a_runtime_error(self, monkeypatch):
        monkeypatch.setattr(
            "src.dapier.connections.credentials.get_credential",
            lambda credential_id: (_ for _ in ()).throw(KeyError(credential_id)))

        with pytest.raises(RuntimeError, match="not configured"):
            poll_triggers.fetch_page(build(bucket=BUCKET))


# --- fire: seeding, emission, dedupe --------------------------------------------------


class TestS3Fire:
    @mock_aws
    def test_the_first_fire_seeds_and_fires_nothing(self, aws_keys):
        client = boto3.client("s3", region_name="eu-west-1")
        client.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "eu-west-1"})
        client.put_object(Bucket=BUCKET, Key="inbox/a.txt", Body=b"hello")

        result, fired = run_fire(build(bucket=BUCKET, prefix="inbox/"),
                                 cursors=FakeCursorTable())

        assert result == {"poll": "s3-watch", "fired": 0}  # history, not news
        assert fired == []

    @mock_aws
    def test_a_new_object_fires_as_an_s3_event(self, aws_keys):
        client = boto3.client("s3", region_name="eu-west-1")
        client.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "eu-west-1"})
        client.put_object(Bucket=BUCKET, Key="inbox/a.txt", Body=b"hello")
        cursors = FakeCursorTable()
        run_fire(build(bucket=BUCKET, prefix="inbox/"), cursors=cursors)

        time.sleep(1.1)  # LastModified has whole-second precision
        client.put_object(Bucket=BUCKET, Key="inbox/b.txt", Body=b"new")
        result, fired = run_fire(build(bucket=BUCKET, prefix="inbox/"),
                                 cursors=cursors)

        assert result == {"poll": "s3-watch", "fired": 1}
        event = fired[0]
        assert event["connector"] == "s3"
        assert event["event"] == "file.created"
        assert event["data"]["poll"] == "s3-watch"
        assert event["data"]["key"] == "inbox/b.txt"

    @mock_aws
    def test_an_unchanged_bucket_fires_nothing(self, aws_keys):
        client = boto3.client("s3", region_name="eu-west-1")
        client.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "eu-west-1"})
        client.put_object(Bucket=BUCKET, Key="inbox/a.txt", Body=b"hello")
        cursors = FakeCursorTable()
        run_fire(build(bucket=BUCKET, prefix="inbox/"), cursors=cursors)

        result, fired = run_fire(build(bucket=BUCKET, prefix="inbox/"),
                                 cursors=cursors)

        assert result["fired"] == 0
        assert fired == []

    @mock_aws
    def test_an_overwrite_fires_again(self, aws_keys):
        """A rewritten object is a new version of the file: its composite id
        (last_modified|key) moves, so the new version fires."""
        client = boto3.client("s3", region_name="eu-west-1")
        client.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "eu-west-1"})
        client.put_object(Bucket=BUCKET, Key="inbox/a.txt", Body=b"hello")
        cursors = FakeCursorTable()
        run_fire(build(bucket=BUCKET, prefix="inbox/"), cursors=cursors)

        time.sleep(1.1)  # LastModified has whole-second precision
        client.put_object(Bucket=BUCKET, Key="inbox/a.txt", Body=b"rewritten")
        result, fired = run_fire(build(bucket=BUCKET, prefix="inbox/"),
                                 cursors=cursors)

        assert result["fired"] == 1
        assert fired[0]["data"]["key"] == "inbox/a.txt"

    @mock_aws
    def test_a_failed_object_stays_unseen_so_it_retries(self, aws_keys):
        client = boto3.client("s3", region_name="eu-west-1")
        client.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "eu-west-1"})
        client.put_object(Bucket=BUCKET, Key="inbox/a.txt", Body=b"hello")
        cursors = FakeCursorTable()
        run_fire(build(bucket=BUCKET, prefix="inbox/"), cursors=cursors)

        time.sleep(1.1)  # LastModified has whole-second precision
        client.put_object(Bucket=BUCKET, Key="inbox/b.txt", Body=b"new")
        client.put_object(Bucket=BUCKET, Key="inbox/c.txt", Body=b"newer")
        item = build(bucket=BUCKET, prefix="inbox/")
        first_page, _ = poll_triggers.fetch_page_response(
            item, cursor="2000-01-01T00:00:00+00:00")
        fail_on = first_page[-1]["id"]
        result, fired = run_fire(item, cursors=cursors, fail_on=fail_on)

        assert result["fired"] == 1  # the page stops at the failed object
        seen_scope = cursors.items["seen#poll#s3-watch"]["seen"]
        assert len(seen_scope) == 1  # only the succeeded object is seen

        retry, fired_retry = run_fire(item, cursors=cursors)

        assert retry["fired"] == 1  # the failed object runs this time
        assert fired_retry[0]["data"]["key"] == "inbox/c.txt"


if __name__ == "__main__":
    pytest.main([__file__])
