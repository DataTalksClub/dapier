"""The s3 poll source: save validation, bucket-listing fetch semantics,
end-to-end fires, and the S3 chip's sample discovery.

Like the other DynamoDB-backed suites, the tables are fakes and boto3 is a
hand-stubbed client (moto would hide real request-shape mistakes); the poll
source is exercised through the ``poll_sources`` seam — ``resolve("s3")`` —
so the registration itself is under test.
"""
from datetime import datetime, timezone

import boto3
import pytest

import plugins.aws.plugin as s3_connector
from src.dapier.connectors import trigger_discovery
from src.dapier.triggers import poll_sources, poll_triggers
from src.dapier.triggers.email_triggers import TriggerError


def ts(hour, minute=0):
    """A boto3-shaped LastModified: a tz-aware datetime."""
    return datetime(2026, 9, 28, hour, minute, tzinfo=timezone.utc)


def iso(hour, minute=0):
    return ts(hour, minute).isoformat()


class FakeS3:
    """list_objects_v2 stand-in: responses keyed by ContinuationToken."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def list_objects_v2(self, **kwargs):
        self.calls.append(kwargs)
        return self.pages[kwargs.get("ContinuationToken")]


def s3_entry(key, modified, size=100, etag='"etag-1"'):
    return {"Key": key, "Size": size, "LastModified": modified, "ETag": etag}


def stub_aws(monkeypatch, pages):
    """Stored AWS keys plus the FakeS3 the boto3 client seam hands back."""
    fake = FakeS3(pages)
    monkeypatch.setattr(
        "src.dapier.connections.credentials.get_credential",
        lambda credential_id: {"access_key_id": "AKIAX", "secret_access_key": "b" * 40})
    monkeypatch.setattr(boto3, "client", lambda service, **kwargs: fake)
    return fake


def s3_item(**overrides):
    """A stored s3 poll item, exactly as build_item leaves it."""
    item = {"poll_id": "bucket-watch", "source": "s3", "bucket": "acme-exports",
            "prefix": "", "credential_id": "aws", "cursor_mode": "next_cursor",
            "id_path": "", "max_items": 25, "dedupe_ttl_days": 7}
    item.update(overrides)
    return item


def s3_save_body(**overrides):
    body = {"name": "bucket-watch", "expression": "rate(5 minutes)",
            "source": "s3", "bucket": "acme-exports", "prefix": "invoices/",
            "actions": [{"type": "slack", "channel": "#news", "text": "new file"}]}
    body.update(overrides)
    return body


@pytest.fixture()
def spec():
    return poll_sources.resolve("s3")


# --- save: build_item through the source's validate ------------------------------


class TestBuildItem:
    def test_s3_save_stores_the_bucket_and_the_fetch_defaults(self):
        item = poll_triggers.build_item(s3_save_body(), "op@example.test")

        assert item["source"] == "s3"
        assert item["bucket"] == "acme-exports"
        assert item["prefix"] == "invoices/"
        assert item["credential_id"] == "aws"  # the shared keys by default
        assert item["cursor_mode"] == "next_cursor"
        assert item["url"] == ""  # non-http sources never carry an endpoint
        assert item["id_path"] == "id"  # event_for reads data["id"] — the composite id

    def test_an_explicit_credential_is_stored(self):
        item = poll_triggers.build_item(
            s3_save_body(credential_id="backup-keys"), "op@example.test")

        assert item["credential_id"] == "backup-keys"

    def test_a_missing_bucket_is_a_save_error(self):
        with pytest.raises(TriggerError, match="bucket"):
            poll_triggers.build_item(s3_save_body(bucket="  "), "op@example.test")

    def test_an_unknown_source_names_the_choices(self):
        with pytest.raises(TriggerError,
                           match=r"source must be one of: .*\bs3\b"):
            poll_triggers.build_item(s3_save_body(source="bogus"), "op@example.test")

    def test_http_polls_keep_their_shape(self):
        item = poll_triggers.build_item({
            "name": "api-watch", "expression": "rate(1 hour)",
            "url": "https://example.test/list", "id_path": "createdTime",
            "actions": [{"type": "slack", "channel": "#news", "text": "new item"}]},
            "op@example.test")

        assert item["source"] == "http"
        assert item["url"] == "https://example.test/list"
        assert "bucket" not in item

        with pytest.raises(TriggerError, match="url must be"):
            poll_triggers.build_item(
                {"name": "api-watch", "expression": "rate(1 hour)",
                 "actions": [{"type": "slack", "channel": "#news", "text": "x"}]},
                "op@example.test")

    def test_public_view_shows_the_provider_params(self):
        item = poll_triggers.build_item(s3_save_body(), "op@example.test")

        view = poll_triggers.public_view(item)

        assert view["source"] == "s3"
        assert view["bucket"] == "acme-exports"
        assert view["prefix"] == "invoices/"


# --- fetch: ListObjectsV2 against the stored watermark ---------------------------


class TestFetch:
    def test_the_first_fetch_seeds_without_emitting(self, spec, monkeypatch):
        stub_aws(monkeypatch, {None: {"Contents": [
            s3_entry("invoices/a.pdf", ts(12)), s3_entry("invoices/b.pdf", ts(13, 30))]}})

        items, cursor = spec.fetch(s3_item(), None)

        assert items == []
        assert cursor == iso(13, 30)  # the newest object becomes the watermark

    def test_an_empty_bucket_seeds_nothing(self, spec, monkeypatch):
        stub_aws(monkeypatch, {None: {}})

        assert spec.fetch(s3_item(), None) == ([], None)

    def test_only_strictly_newer_objects_fire_oldest_first(self, spec, monkeypatch):
        stub_aws(monkeypatch, {None: {"Contents": [
            s3_entry("invoices/late.pdf", ts(14)),
            s3_entry("invoices/at-cursor.pdf", ts(12)),
            s3_entry("invoices/early.pdf", ts(11)),
            s3_entry("invoices/new.pdf", ts(13))]}})

        items, cursor = spec.fetch(s3_item(), iso(12))

        assert [obj["key"] for obj in items] == ["invoices/new.pdf", "invoices/late.pdf"]
        assert cursor == iso(14)
        assert items[0]["id"] == f"{iso(13)}|invoices/new.pdf"

    def test_nothing_newer_parks_the_incoming_cursor(self, spec, monkeypatch):
        stub_aws(monkeypatch, {None: {"Contents": [s3_entry("invoices/old.pdf", ts(11))]}})

        items, cursor = spec.fetch(s3_item(), iso(12))

        assert items == []
        assert cursor == iso(12)

    def test_same_second_uploads_stay_distinct(self, spec, monkeypatch):
        stub_aws(monkeypatch, {None: {"Contents": [
            s3_entry("invoices/b.pdf", ts(13)), s3_entry("invoices/a.pdf", ts(13))]}})

        items, _cursor = spec.fetch(s3_item(), iso(12))

        assert [obj["key"] for obj in items] == ["invoices/a.pdf", "invoices/b.pdf"]
        assert items[0]["id"] != items[1]["id"]

    def test_prefix_and_bucket_reach_the_request(self, spec, monkeypatch):
        fake = stub_aws(monkeypatch, {None: {}})

        spec.fetch(s3_item(prefix="invoices/"), None)

        assert fake.calls == [{"Bucket": "acme-exports", "MaxKeys": 1000,
                               "Prefix": "invoices/"}]

    def test_truncated_pages_are_followed_to_the_cap(self, spec, monkeypatch):
        fake = stub_aws(monkeypatch, {
            None: {"Contents": [s3_entry("invoices/1.pdf", ts(10))],
                   "IsTruncated": True, "NextContinuationToken": "t1"},
            "t1": {"Contents": [s3_entry("invoices/2.pdf", ts(15))]},
        })

        items, cursor = spec.fetch(s3_item(), iso(12))

        assert [obj["key"] for obj in items] == ["invoices/2.pdf"]
        assert cursor == iso(15)
        assert len(fake.calls) == 2
        assert fake.calls[1]["ContinuationToken"] == "t1"
        assert fake.calls[1]["MaxKeys"] == 999

    def test_a_missing_credential_is_a_fetch_failure(self, spec, monkeypatch):
        monkeypatch.setattr(
            "src.dapier.connections.credentials.get_credential",
            lambda credential_id: (_ for _ in ()).throw(KeyError(credential_id)))

        with pytest.raises(RuntimeError, match="not configured"):
            spec.fetch(s3_item(), None)

    def test_keys_without_aws_secrets_are_a_fetch_failure(self, spec, monkeypatch):
        monkeypatch.setattr(
            "src.dapier.connections.credentials.get_credential",
            lambda credential_id: {"token": "not-aws"})

        with pytest.raises(RuntimeError, match="does not contain AWS keys"):
            spec.fetch(s3_item(), None)

    def test_a_provider_error_is_a_fetch_failure(self, spec, monkeypatch):
        fake = stub_aws(monkeypatch, {None: {}})

        def explode(**_kwargs):
            raise RuntimeError("AccessDenied")

        fake.list_objects_v2 = explode

        with pytest.raises(RuntimeError, match="s3 list failed"):
            spec.fetch(s3_item(), None)

    def test_a_stored_item_without_a_bucket_never_lists(self, spec):
        with pytest.raises(RuntimeError, match="bucket"):
            spec.fetch({"poll_id": "x", "source": "s3"}, None)


# --- fire: seed, emit, dedupe over the fake tables -------------------------------


class FakePollTable:
    """DynamoDB stand-in keyed by poll_id."""

    def __init__(self):
        self.items = {}

    def get_item(self, Key):
        item = self.items.get(Key["poll_id"])
        return {"Item": dict(item)} if item else {}

    def put_item(self, Item):
        self.items[Item["poll_id"]] = dict(Item)

    def delete_item(self, Key):
        self.items.pop(Key["poll_id"], None)


class FakeCursorTable:
    """DynamoDB stand-in for poll cursors and seen-sets (cursor_id key)."""

    def __init__(self):
        self.items = {}

    def get_item(self, Key):
        item = self.items.get(Key["cursor_id"])
        return {"Item": dict(item)} if item else {}

    def put_item(self, Item):
        self.items[Item["cursor_id"]] = dict(Item)

    def delete_item(self, Key):
        self.items.pop(Key["cursor_id"], None)


class TestFire:
    """The full scheduled fire: seed on create, emit new objects, and never
    re-run an object the seen-set already published."""

    def test_seed_then_new_object_then_recycled_page(self, monkeypatch):
        polls, cursors, captured = FakePollTable(), FakeCursorTable(), []
        pages = {None: {"Contents": [
            s3_entry("invoices/4137.pdf", ts(13)),
            s3_entry("invoices/4136.pdf", ts(12))]}}
        stub_aws(monkeypatch, pages)
        monkeypatch.setattr("src.dapier.engine.execute",
                            lambda event, **_kwargs: captured.append(event))
        polls.put_item(Item=poll_triggers.build_item(
            s3_save_body(prefix=""), "op@example.test"))

        # First fire: the bucket's newest object seeds the watermark; the
        # whole existing history must not fire.
        result = poll_triggers.fire("bucket-watch", table_ref=polls,
                                    cursor_table_ref=cursors)

        assert result == {"poll": "bucket-watch", "fired": 0}
        assert captured == []
        assert poll_triggers.get_cursor("bucket-watch", table=cursors) == iso(13)

        # A new object lands; the next fire emits it as a real s3 event.
        pages[None]["Contents"].append(s3_entry("invoices/4138.pdf", ts(14, 30)))

        result = poll_triggers.fire("bucket-watch", table_ref=polls,
                                    cursor_table_ref=cursors)

        assert result == {"poll": "bucket-watch", "fired": 1}
        event = captured[0]
        assert event["connector"] == "s3"
        assert event["event"] == "file.created"
        assert event["source"] == "bucket-watch"
        assert event["data"]["key"] == "invoices/4138.pdf"
        assert event["data"]["item_id"] == f"{iso(14, 30)}|invoices/4138.pdf"
        assert poll_triggers.get_cursor("bucket-watch", table=cursors) == iso(14, 30)

        # A recycled page (the cursor rewound to before the object): the
        # seen-set absorbs it — one skipped, zero runs.
        poll_triggers.put_cursor("bucket-watch", iso(13), table=cursors)

        result = poll_triggers.fire("bucket-watch", table_ref=polls,
                                    cursor_table_ref=cursors)

        assert result == {"poll": "bucket-watch", "fired": 0, "skipped_seen": 1}
        assert len(captured) == 1


# --- the chip's sample discovery -------------------------------------------------


def pin_no_history(monkeypatch):
    monkeypatch.setattr(trigger_discovery, "history_sample",
                        lambda connector, event=None: None)


def discover(**body):
    return trigger_discovery.api_discover({"connector": "s3", **body})


class TestSampleDiscovery:
    def test_a_named_s3_poll_pulls_its_next_object_live(self, monkeypatch):
        pin_no_history(monkeypatch)
        monkeypatch.setattr(poll_triggers, "get_item",
                            lambda name, table_ref=None: s3_item(poll_id=name))
        cursors = []

        def sample_fetch(item, cursor=None, transport=None):
            cursors.append(cursor)
            return [{
                "id": f"{iso(9)}|invoices/4137.pdf",
                "key": "invoices/4137.pdf", "size": 52310,
                "last_modified": iso(9)}]

        monkeypatch.setattr(poll_triggers, "fetch_page", sample_fetch)

        status, payload = discover(event="bucket-watch")

        assert status == 200, payload
        # The everything cursor "" — not the seeding None, which yields no
        # items and would push every live sample down the fallback chain.
        assert cursors == [""]
        assert payload["source"] == "live"
        assert payload["sample"]["connector"] == "s3"
        assert payload["sample"]["event"] == "file.created"
        assert payload["sample"]["data"]["key"] == "invoices/4137.pdf"
        assert payload["sample"]["data"]["item_id"].endswith("|invoices/4137.pdf")

    def test_the_live_pull_runs_the_real_s3_fetch_against_the_bucket(self, monkeypatch):
        """No fetch stub: the sample goes through fetch_page → the s3 poll
        source with the everything cursor, so a stored poll over a bucket
        with objects answers live. (With no cursor the source takes the
        first-fire seeding branch, returns no items, and every such sample
        silently fell to history/synthetic.)"""
        pin_no_history(monkeypatch)
        monkeypatch.setattr(poll_triggers, "get_item",
                            lambda name, table_ref=None: s3_item(poll_id=name))
        fake = stub_aws(monkeypatch, {None: {"Contents": [
            s3_entry("invoices/4137.pdf", ts(9))]}})

        status, payload = discover(event="bucket-watch")

        assert status == 200, payload
        assert payload["source"] == "live"
        assert payload["connection_id"] is None
        assert payload["sample"]["connector"] == "s3"
        assert payload["sample"]["event"] == "file.created"
        assert payload["sample"]["data"]["key"] == "invoices/4137.pdf"
        assert payload["sample"]["data"]["item_id"] == f"{iso(9)}|invoices/4137.pdf"
        assert fake.calls == [{"Bucket": "acme-exports", "MaxKeys": 1000}]

    def test_an_http_poll_name_never_goes_live(self, monkeypatch):
        pin_no_history(monkeypatch)
        monkeypatch.setattr(poll_triggers, "get_item",
                            lambda name, table_ref=None: s3_item(poll_id=name,
                                                                 source="http"))

        def no_fetch(item, cursor=None, transport=None):
            raise AssertionError("a non-s3 poll must not be fetched here")

        monkeypatch.setattr(poll_triggers, "fetch_page", no_fetch)

        status, payload = discover(event="api-watch")

        assert status == 200, payload
        assert payload["source"] == "synthetic"

    def test_without_a_poll_or_history_the_sample_is_synthetic(self, monkeypatch):
        pin_no_history(monkeypatch)
        monkeypatch.setattr(poll_triggers, "get_item",
                            lambda name, table_ref=None: None)

        status, payload = discover()

        assert status == 200, payload
        assert payload["source"] == "synthetic"
        assert payload["sample"]["event"] == "file.created"
        assert payload["sample"]["data"]["bucket"] == "dapier-renders"
        assert payload["sample"]["data"]["key"] == "invoices/4137.pdf"
        assert payload["sample"]["data"]["size"] == 52310
        assert payload["sample"]["data"]["last_modified"]

    def test_unconfigured_poll_triggers_fall_back_never_keyerror(self, monkeypatch):
        # Bare deploys have no POLL_TRIGGERS_TABLE; the sample pull must
        # still answer (see the audit note on unguarded table env vars).
        pin_no_history(monkeypatch)
        monkeypatch.delenv("POLL_TRIGGERS_TABLE", raising=False)

        status, payload = discover(event="bucket-watch")

        assert status == 200, payload
        assert payload["source"] == "synthetic"

    def test_a_failed_live_fetch_falls_back_instead_of_502(self, monkeypatch):
        pin_no_history(monkeypatch)
        monkeypatch.setattr(poll_triggers, "get_item",
                            lambda name, table_ref=None: s3_item(poll_id=name))

        def broken_fetch(item, cursor=None, transport=None):
            raise RuntimeError("no aws credential configured")

        monkeypatch.setattr(poll_triggers, "fetch_page", broken_fetch)

        status, payload = discover(event="bucket-watch")

        assert status == 200, payload
        assert payload["source"] == "synthetic"

    def test_s3_is_in_the_sample_catalog(self):
        assert "s3" in trigger_discovery.trigger_discovery_catalog()["sample"]


if __name__ == "__main__":
    pytest.main([__file__])
