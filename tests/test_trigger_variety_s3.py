"""S3 trigger variety: one documented sample per declared file event, and
the updated/deleted diff sources that publish two of them.

The catalog declares file.created / file.updated / file.deleted and the
poll sources publish all three (``s3`` on the last_modified watermark,
``s3.updates``/``s3.deletions`` on a listing snapshot parked as the
cursor), so the sample pull — the shared api_discover dispatch behind
`dapier triggers sample` and the designer's test panel — must serve each
event its own realistic payload, recorded history must never answer one
event's ask with another event's run, and the diff sources must never fire
on an ambiguous listing (first poll, re-scoped prefix, capped listing).
The bucket is moto's; the stored AWS keys are stubbed at the credential
store, where the connector resolves them.
"""
import json
import time
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

from src.dapier.connectors import s3 as s3_connector
from src.dapier.connectors import trigger_discovery
from src.dapier.connectors.registry import CONNECTORS
from src.dapier.triggers import poll_sources, poll_triggers

import src.dapier.connectors  # noqa: F401  (import = registration)


ACTIONS = [{"type": "email_send", "to": "dest@example.test"}]

BUCKET = "watched"


# --- the chip's sample pull: one payload per declared event --------------------


@pytest.fixture(autouse=True)
def no_recorded_history(monkeypatch):
    """Pin the fallback chain to synthetic: history depends on run tables."""
    monkeypatch.setattr(
        trigger_discovery, "history_sample",
        lambda connector, event=None: None)


def _sample(**kwargs):
    status, payload = trigger_discovery.api_discover(
        {"connector": "s3", **kwargs})
    assert status == 200, payload
    return payload


def test_every_declared_event_serves_its_own_sample():
    events = CONNECTORS["s3"].events
    assert events == ("file.created", "file.updated", "file.deleted")
    for event in events:
        payload = _sample(event=event)
        assert payload["event"] == event, event
        assert payload["sample"]["event"] == event, event
        assert payload["source"] == "synthetic", event
        data = payload["sample"]["data"]
        assert data["key"] == "invoices/4137.pdf", event


def test_created_and_updated_carry_object_state_deleted_only_the_key():
    created = _sample(event="file.created")["sample"]["data"]
    updated = _sample(event="file.updated")["sample"]["data"]
    deleted = _sample(event="file.deleted")["sample"]["data"]
    for state in (created, updated):
        assert state["bucket"] == "dapier-renders"
        assert state["size"] and state["last_modified"] and state["etag"]
    # a rewrite moves the changed markers: etag, size, last_modified
    assert created["etag"] != updated["etag"]
    assert created["size"] != updated["size"]
    assert created["last_modified"] < updated["last_modified"]
    # the object is gone: the identity alone, the same {id, key} the
    # deletions source publishes
    assert deleted == {"id": created["key"], "key": created["key"]}
    assert "etag" not in deleted and "size" not in deleted


def test_default_and_unknown_events_fall_back_to_file_created():
    default = _sample()
    assert default["sample"]["event"] == "file.created"
    assert default["sample"]["data"]["etag"] == \
        '"9b2cf535f27731c974343645a3985328"'
    unknown = _sample(event="file.restored")
    assert unknown["sample"]["event"] == "file.created"
    assert unknown["sample"]["data"] == default["sample"]["data"]


def test_history_never_answers_one_event_with_another_events_run(monkeypatch):
    recorded = {
        "connector": "s3", "event": "file.created",
        "data": {"bucket": "real-bucket", "key": "real/thing.txt",
                 "size": 42, "last_modified": "2026-09-28T10:00:00+00:00",
                 "etag": '"real1"'},
        "id": "s3:real", "source": "bucket-watch",
        "occurred_at": "2026-09-28T10:00:00Z",
    }

    def fake_history(connector, event=None):
        return dict(recorded) if event == "file.created" else None

    monkeypatch.setattr(trigger_discovery, "history_sample", fake_history)
    hit = _sample(event="file.created")
    assert hit["source"] == "history"
    assert hit["sample"]["data"]["key"] == "real/thing.txt"
    miss = _sample(event="file.updated")
    assert miss["source"] == "synthetic"
    assert miss["sample"]["event"] == "file.updated"
    assert miss["sample"]["data"]["etag"] != '"real1"'
    gone = _sample(event="file.deleted")
    assert gone["source"] == "synthetic"
    assert gone["sample"]["event"] == "file.deleted"
    assert "etag" not in gone["sample"]["data"]


def test_a_dotted_ask_never_matches_a_stored_poll_name(monkeypatch):
    """Poll ids cannot contain dots, so event=file.updated is a per-event
    ask even when a poll exists — and a per-event ask is never answered
    live with another event's shape."""
    monkeypatch.setattr(poll_triggers, "get_item",
                        lambda name, table_ref=None: None)

    payload = _sample(event="file.updated")

    assert payload["sample"]["event"] == "file.updated"


# --- the updated/deleted sources: registration, save shape, fires --------------


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


def build(source, name="s3-watch", **body):
    body.setdefault("actions", [dict(ACTIONS[0])])
    body["source"] = source
    body["expression"] = "rate(5 minutes)"
    return poll_triggers.build_item({"name": name, **body}, "op@example.test")


@pytest.fixture
def aws_keys(monkeypatch):
    """The stored key pair the connector resolves from the credential store."""
    monkeypatch.setattr(
        "src.dapier.connections.credentials.get_credential",
        lambda credential_id: {"access_key_id": "testing",
                               "secret_access_key": "testing"})


def bucket_with(*keys_and_bodies):
    client = boto3.client("s3", region_name="eu-west-1")
    client.create_bucket(Bucket=BUCKET,
                         CreateBucketConfiguration={"LocationConstraint": "eu-west-1"})
    for key, body in keys_and_bodies:
        client.put_object(Bucket=BUCKET, Key=key, Body=body)
    return client


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


def snapshot_of(cursors, name):
    """The parked listing snapshot, parsed back (the updates/deletions
    cursor is the diff baseline in a JSON string)."""
    parked = poll_triggers.get_cursor(name, table=cursors)
    assert parked and parked.lstrip().startswith("{"), parked
    return json.loads(parked)


def test_the_diff_sources_register_under_the_s3_chip():
    updates = poll_sources.resolve("s3.updates")
    deletions = poll_sources.resolve("s3.deletions")

    assert (updates.connector, updates.event) == ("s3", "file.updated")
    assert (deletions.connector, deletions.event) == ("s3", "file.deleted")
    for source in (updates, deletions):
        assert source.event in CONNECTORS["s3"].events, source.name


def test_the_diff_sources_share_the_s3_save_shape():
    for source in ("s3.updates", "s3.deletions"):
        item = build(source, bucket=BUCKET, prefix="inbox/",
                     credential_id="backup-keys")

        assert item["source"] == source
        assert item["bucket"] == BUCKET
        assert item["prefix"] == "inbox/"
        assert item["credential_id"] == "backup-keys"
        assert item["cursor_mode"] == "next_cursor"
        assert item["id_path"] == "id"
        assert item["url"] == ""

        view = poll_triggers.public_view(item)

        assert view["source"] == source
        assert view["bucket"] == BUCKET
        assert view["prefix"] == "inbox/"


def test_the_fired_workflows_match_the_chip():
    assert poll_triggers.workflow_for(
        build("s3.updates", name="edit-watch", bucket=BUCKET))["trigger"] == {
        "connector": "s3", "event": "file.updated",
        "filters": {"poll": {"equals": "edit-watch"}}}
    assert poll_triggers.workflow_for(
        build("s3.deletions", name="gone-watch", bucket=BUCKET))["trigger"] == {
        "connector": "s3", "event": "file.deleted",
        "filters": {"poll": {"equals": "gone-watch"}}}


@mock_aws
def test_updates_first_fire_seeds_the_snapshot_and_fires_nothing(aws_keys):
    bucket_with(("inbox/a.txt", b"hello"))
    cursors = FakeCursorTable()

    result, fired = run_fire(build("s3.updates", bucket=BUCKET, prefix="inbox/"),
                             cursors=cursors)

    assert result == {"poll": "s3-watch", "fired": 0}  # history, not news
    assert fired == []
    snapshot = snapshot_of(cursors, "s3-watch")
    assert snapshot["prefix"] == "inbox/"
    assert snapshot["truncated"] is False
    assert list(snapshot["keys"]) == ["inbox/a.txt"]


@mock_aws
def test_an_overwrite_fires_as_file_updated(aws_keys):
    """A rewritten object re-lists its key with a changed marker (etag,
    size): the updates source fires the object facts, not the created
    event."""
    client = bucket_with(("inbox/a.txt", b"hello"))
    cursors = FakeCursorTable()
    run_fire(build("s3.updates", bucket=BUCKET, prefix="inbox/"),
             cursors=cursors)

    client.put_object(Bucket=BUCKET, Key="inbox/a.txt", Body=b"rewritten, longer")
    result, fired = run_fire(build("s3.updates", bucket=BUCKET, prefix="inbox/"),
                             cursors=cursors)

    assert result == {"poll": "s3-watch", "fired": 1}
    event = fired[0]
    assert event["connector"] == "s3"
    assert event["event"] == "file.updated"
    assert event["data"]["poll"] == "s3-watch"
    assert event["data"]["key"] == "inbox/a.txt"
    assert event["data"]["size"] == len(b"rewritten, longer")
    assert event["data"]["etag"] and event["data"]["last_modified"]


@mock_aws
def test_an_unchanged_or_merely_newer_bucket_fires_no_updates(aws_keys):
    """A re-listed key with the same fingerprint is not an edit, and a
    brand-new key is the created source's news — never double-fired."""
    client = bucket_with(("inbox/a.txt", b"hello"))
    cursors = FakeCursorTable()
    item = build("s3.updates", bucket=BUCKET, prefix="inbox/")
    run_fire(item, cursors=cursors)

    time.sleep(1.1)  # LastModified has whole-second precision
    client.put_object(Bucket=BUCKET, Key="inbox/b.txt", Body=b"new")
    result, fired = run_fire(item, cursors=cursors)

    assert result == {"poll": "s3-watch", "fired": 0}
    assert fired == []


@mock_aws
def test_a_refetched_page_does_not_refire_its_update(aws_keys):
    """A rewound cursor re-diffs the same baseline; the seen-set absorbs
    the already-published version instead of re-running it."""
    client = bucket_with(("inbox/a.txt", b"hello"))
    cursors = FakeCursorTable()
    item = build("s3.updates", bucket=BUCKET, prefix="inbox/")
    run_fire(item, cursors=cursors)
    baseline = poll_triggers.get_cursor("s3-watch", table=cursors)

    client.put_object(Bucket=BUCKET, Key="inbox/a.txt", Body=b"rewritten")
    result, fired = run_fire(item, cursors=cursors)
    assert result == {"poll": "s3-watch", "fired": 1}

    # rewind the cursor to the pre-edit snapshot: the page refetches and
    # re-detects the same edit, but the seen-set has its id already
    poll_triggers.put_cursor("s3-watch", baseline, table=cursors)
    result, fired = run_fire(item, cursors=cursors)

    assert result["fired"] == 0
    assert result["skipped_seen"] == 1
    assert fired == []


@mock_aws
def test_deletions_first_fire_seeds_and_deletion_fires_the_key(aws_keys):
    client = bucket_with(("inbox/a.txt", b"hello"),
                         ("inbox/b.txt", b"bye"))
    cursors = FakeCursorTable()
    item = build("s3.deletions", bucket=BUCKET, prefix="inbox/")
    run_fire(item, cursors=cursors)
    assert snapshot_of(cursors, "s3-watch")["keys"].keys() == \
        {"inbox/a.txt", "inbox/b.txt"}

    client.delete_object(Bucket=BUCKET, Key="inbox/a.txt")
    result, fired = run_fire(item, cursors=cursors)

    assert result == {"poll": "s3-watch", "fired": 1}
    event = fired[0]
    assert event["connector"] == "s3"
    assert event["event"] == "file.deleted"
    assert event["data"]["poll"] == "s3-watch"
    assert event["data"]["key"] == "inbox/a.txt"
    assert event["data"]["item_id"] == "inbox/a.txt"
    # the snapshot rotated: the deleted key is no longer the baseline
    assert snapshot_of(cursors, "s3-watch")["keys"].keys() == {"inbox/b.txt"}


@mock_aws
def test_a_key_added_after_seeding_is_never_a_deletion(aws_keys):
    client = bucket_with(("inbox/a.txt", b"hello"))
    cursors = FakeCursorTable()
    item = build("s3.deletions", bucket=BUCKET, prefix="inbox/")
    run_fire(item, cursors=cursors)

    client.put_object(Bucket=BUCKET, Key="inbox/b.txt", Body=b"new")
    result, fired = run_fire(item, cursors=cursors)

    assert result == {"poll": "s3-watch", "fired": 0}
    assert fired == []


@mock_aws
def test_a_prefix_change_re_seeds_instead_of_firing_deletions(aws_keys):
    """The operator re-scopes the watch: keys outside the new prefix must
    not read as mass deletions — the old keys were a different watch."""
    bucket_with(("logs/a.txt", b"hello"))
    cursors = FakeCursorTable()
    item = build("s3.deletions", bucket=BUCKET, prefix="logs/")
    run_fire(item, cursors=cursors)

    rescoped = build("s3.deletions", bucket=BUCKET, prefix="other/")
    result, fired = run_fire(rescoped, cursors=cursors)

    assert result == {"poll": "s3-watch", "fired": 0}
    assert fired == []
    assert snapshot_of(cursors, "s3-watch")["prefix"] == "other/"


@mock_aws
def test_a_foreign_cursor_re_seeds_instead_of_diffing(aws_keys):
    """A watermark-style cursor (an s3 created trigger's shape) is not a
    snapshot: diffing it would read every key as an edit and every absent
    one as deleted."""
    bucket_with(("inbox/a.txt", b"hello"))
    cursors = FakeCursorTable()
    item = build("s3.deletions", bucket=BUCKET, prefix="inbox/")
    poll_triggers.put_cursor("s3-watch", "2026-09-28T09:00:00+00:00",
                             table=cursors)

    result, fired = run_fire(item, cursors=cursors)

    assert result == {"poll": "s3-watch", "fired": 0}
    assert fired == []
    assert snapshot_of(cursors, "s3-watch")["prefix"] == "inbox/"


@mock_aws
def test_a_capped_listing_never_fires_deletions(aws_keys):
    """Keys beyond the listing cap are unlisted, not absent: neither a
    capped baseline nor a capped re-listing may prove a deletion."""
    bucket_with(("inbox/a.txt", b"hello"), ("inbox/b.txt", b"bye"))
    item = build("s3.deletions", bucket=BUCKET, prefix="inbox/")

    with patch.object(s3_connector, "S3_POLL_MAX_KEYS", 1):
        cursors = FakeCursorTable()
        run_fire(item, cursors=cursors)
        assert snapshot_of(cursors, "s3-watch")["truncated"] is True

        client = boto3.client("s3", region_name="eu-west-1")
        client.delete_object(Bucket=BUCKET, Key="inbox/b.txt")
        result, fired = run_fire(item, cursors=cursors)

    assert result == {"poll": "s3-watch", "fired": 0}
    assert fired == []


@mock_aws
def test_a_shrunk_listing_with_a_capped_baseline_fires_nothing(aws_keys):
    """The bucket grew past the cap between fires and shrank back: the
    capped baseline proves nothing either, so the deletion waits for an
    uncapped fire."""
    client = bucket_with(("inbox/a.txt", b"hello"), ("inbox/b.txt", b"bye"))
    cursors = FakeCursorTable()
    item = build("s3.deletions", bucket=BUCKET, prefix="inbox/")

    with patch.object(s3_connector, "S3_POLL_MAX_KEYS", 1):
        run_fire(item, cursors=cursors)  # capped baseline

    client.delete_object(Bucket=BUCKET, Key="inbox/b.txt")
    result, fired = run_fire(item, cursors=cursors)

    assert result == {"poll": "s3-watch", "fired": 0}
    assert fired == []


# --- the chip's live pull for the new sources ------------------------------------


@mock_aws
def test_an_updates_poll_pulls_its_newest_object_live(monkeypatch, aws_keys):
    bucket_with(("invoices/4136.pdf", b"older"), ("invoices/4137.pdf", b"newest"))
    item = build("s3.updates", name="edit-watch", bucket=BUCKET)
    monkeypatch.setattr(poll_triggers, "get_item",
                        lambda name, table_ref=None: item)

    status, payload = trigger_discovery.api_discover(
        {"connector": "s3", "event": "edit-watch"})

    assert status == 200, payload
    assert payload["source"] == "live"
    assert payload["sample"]["event"] == "file.updated"
    assert payload["sample"]["data"]["key"] == "invoices/4137.pdf"
    assert payload["sample"]["data"]["poll"] == "edit-watch"


@mock_aws
def test_a_deletions_poll_falls_to_the_documented_example(monkeypatch, aws_keys):
    """A live pull cannot show an absent object: the documented file.deleted
    example answers, never a raise."""
    bucket_with(("invoices/4137.pdf", b"still here"))
    item = build("s3.deletions", name="gone-watch", bucket=BUCKET)
    monkeypatch.setattr(poll_triggers, "get_item",
                        lambda name, table_ref=None: item)

    status, payload = trigger_discovery.api_discover(
        {"connector": "s3", "event": "gone-watch"})

    assert status == 200, payload
    assert payload["source"] == "synthetic"
    assert payload["sample"]["event"] == "file.deleted"
    assert payload["sample"]["data"]["key"] == "invoices/4137.pdf"


@mock_aws
def test_a_failed_live_updates_pull_falls_back_instead_of_502(monkeypatch, aws_keys):
    item = build("s3.updates", name="edit-watch", bucket="no-such-bucket-here")
    monkeypatch.setattr(poll_triggers, "get_item",
                        lambda name, table_ref=None: item)

    status, payload = trigger_discovery.api_discover(
        {"connector": "s3", "event": "edit-watch"})

    assert status == 200, payload
    assert payload["source"] == "synthetic"
    assert payload["sample"]["event"] == "file.updated"


if __name__ == "__main__":
    pytest.main([__file__])
