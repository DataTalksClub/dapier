"""The poll-source seam in isolation: build_item/fetch/event wiring with a
throwaway source, so the contract holds regardless of which provider sources
are registered. Builtin loading is stubbed out — these tests never import
connectors.s3/sheets/drive.
"""

import pytest

from src.dapier.triggers import poll_sources, poll_triggers


@pytest.fixture
def fake_source(monkeypatch):
    """A registered throwaway source; builtin modules stay unloaded."""
    monkeypatch.setattr(poll_sources, "_load_builtins", lambda: None)

    def validate(body):
        bucket = str(body.get("bucket") or "").strip()
        if not bucket:
            raise poll_triggers.TriggerError("bucket is required for the fake source")
        return {"bucket": bucket, "cursor_mode": "next_cursor", "id_path": "id"}

    def fetch(item, cursor):
        return [{"id": "one"}], None

    source = poll_sources.PollSource(
        name="fake", connector="fake-connector", event="fake.event",
        label="Fake", validate=validate, fetch=fetch,
        view=lambda item: {"bucket": item.get("bucket")})
    # A fresh registry, not setitem on the shared one: an earlier suite test
    # may already have loaded the built-in sources into poll_sources.SOURCES.
    monkeypatch.setattr(poll_sources, "SOURCES", {"fake": source})
    return source


def build(name="fake-poll", **body):
    body.setdefault("actions", [{"type": "email_send", "to": "dest@example.test"}])
    return poll_triggers.build_item(
        {"name": name, "expression": "rate(1 hour)", **body}, "operator-test")


class TestBuildItem:
    def test_http_is_the_default_source(self):
        item = build(url="https://example.test/list", id_path="id")
        assert item["source"] == "http"
        assert "bucket" not in item

    def test_provider_params_merge_and_cursor_defaults_apply(self, fake_source):
        item = build(source="fake", bucket="buck")
        assert item["source"] == "fake"
        assert item["bucket"] == "buck"
        assert item["cursor_mode"] == "next_cursor"
        assert item["id_path"] == "id"
        assert item["url"] == ""

    def test_source_requires_its_own_params(self, fake_source):
        with pytest.raises(poll_triggers.TriggerError, match="bucket is required"):
            build(source="fake")

    def test_unknown_source_names_the_choices(self, fake_source):
        with pytest.raises(poll_triggers.TriggerError,
                           match=r"source must be one of: http, fake"):
            build(source="nope")

    def test_source_cannot_override_item_keys(self, fake_source, monkeypatch):
        def hostile(body):
            return {"bucket": "buck", "id_path": "id", "poll_id": "hijack",
                    "enabled": False, "actions": [{"type": "email_send"}]}
        monkeypatch.setattr(fake_source, "validate", hostile)
        item = build(source="fake")
        assert item["poll_id"] == "fake-poll"
        assert item["enabled"] is True
        assert item["actions"] == [{"type": "email_send", "to": "dest@example.test"}]
        assert item["bucket"] == "buck"

    def test_body_still_requires_a_url_for_http(self):
        with pytest.raises(poll_triggers.TriggerError, match="url must be"):
            build()


class TestWiring:
    def _item(self, fake_source):
        return build(source="fake", bucket="buck")

    def test_fetch_dispatches_to_the_source(self, fake_source):
        item = self._item(fake_source)
        assert poll_triggers.fetch_page(item) == [{"id": "one"}]

    def test_event_carries_the_source_connector(self, fake_source):
        event = poll_triggers.event_for(self._item(fake_source), {"id": "one"})
        assert event["connector"] == "fake-connector"
        assert event["event"] == "fake.event"
        assert event["data"]["poll"] == "fake-poll"

    def test_http_event_keeps_poll_connector(self):
        item = build(url="https://example.test/list", id_path="id")
        event = poll_triggers.event_for(item, {"id": "one"})
        assert event["connector"] == "poll"
        assert event["event"] == "item.new"

    def test_workflow_and_public_view_carry_the_source(self, fake_source):
        item = self._item(fake_source)
        workflow = poll_triggers.workflow_for(item)
        assert workflow["trigger"]["connector"] == "fake-connector"
        assert workflow["trigger"]["filters"] == {"poll": {"equals": "fake-poll"}}
        view = poll_triggers.public_view(item)
        assert view["source"] == "fake"
        assert view["bucket"] == "buck"

    def test_stored_source_fails_loudly_when_unregistered(self, fake_source):
        item = self._item(fake_source)
        del poll_sources.SOURCES["fake"]
        with pytest.raises(RuntimeError, match="source 'fake' is not registered"):
            poll_triggers.event_for(item, {"id": "one"})
