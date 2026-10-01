"""Subscribe-on-save: designer saves, toggles, deletes, and rollbacks sync
the YouTube WebSub hub right away (designer_store._sync_youtube ->
youtube_subscriptions.reconcile), with hub failures surfaced as ``warnings``
on the response instead of blocking the change — the renewal schedule
(youtube_subscriptions.handler) re-subscribes whatever a failed call missed.
The CLI prints those warnings; the console renders the same payloads.
"""

import pytest

from dapier_cli import commands as cli_commands
from src.dapier.api import designer_store
from src.dapier.api import runs as runs_module
from src.dapier.triggers import published_workflows
from src.dapier.triggers.intake import youtube_subscriptions


YT_YAML = """\
id: yt-flow
enabled: true
trigger:
  connector: youtube
  event: video.published
  filters:
    channel_id:
      equals: UCabc123
actions:
  - id: a1
    type: webhook
    url: https://example.test/hook
"""


class StubTable:
    def __init__(self):
        self.items = {}

    def put_item(self, Item):
        self.items[Item["workflow_id"]] = Item

    def get_item(self, Key):
        item = self.items.get(Key["workflow_id"])
        return {"Item": item} if item else {}

    def delete_item(self, Key):
        self.items.pop(Key["workflow_id"], None)

    def scan(self, **_):
        return {"Items": list(self.items.values())}


@pytest.fixture
def store(monkeypatch):
    """The publish table configured and stubbed; git commits faked."""
    monkeypatch.setenv(published_workflows.TABLE_ENV, "published-test")
    table = StubTable()
    monkeypatch.setattr(published_workflows, "get_table", lambda table_ref=None: table)
    monkeypatch.setattr(designer_store, "commit_workflow",
                        lambda yaml_text, **kw: {"file": "yt-flow.yaml", "commit": "cafe123"})
    monkeypatch.setattr(designer_store, "commit_delete",
                        lambda source, **kw: {"commit": "deadbee"})
    monkeypatch.setattr(runs_module, "delayed_runs", lambda workflow_id, **kw: [])
    return table


@pytest.fixture
def hub(monkeypatch):
    """The hub faked end to end: settings resolve, calls recorded."""
    monkeypatch.setattr(youtube_subscriptions, "_settings",
                        lambda: ("https://dapier.example.test/hooks/youtube", "s3cret"))
    calls = []

    def fake(mode, channel_id, **kwargs):
        calls.append((mode, channel_id))
        return 202

    monkeypatch.setattr(youtube_subscriptions, "hub_request", fake)
    monkeypatch.setattr(youtube_subscriptions.engine, "all_workflows", lambda: [])
    return calls


def test_save_subscribes_the_channel(store, hub):
    status, payload = designer_store.api_save({"yaml": YT_YAML}, operator="op-1", live=True)
    assert status == 200 and payload["published"] is True
    assert hub == [("subscribe", "UCabc123")]
    assert "warnings" not in payload


def test_unchanged_resave_never_rings_the_hub(store, hub):
    designer_store.api_save({"yaml": YT_YAML}, operator="op-1", live=True)
    hub.clear()
    revised = YT_YAML.replace("https://example.test/hook", "https://example.test/hook2")
    status, payload = designer_store.api_save({"yaml": revised}, operator="op-2", live=True)
    assert status == 200 and payload["published"] is True
    assert hub == []
    assert "warnings" not in payload


def test_channel_edit_swaps_the_subscription(store, hub):
    designer_store.api_save({"yaml": YT_YAML}, operator="op-1", live=True)
    hub.clear()
    other = YT_YAML.replace("UCabc123", "UCnext999")
    status, _ = designer_store.api_save({"yaml": other}, operator="op-2", live=True)
    assert status == 200
    assert ("subscribe", "UCnext999") in hub
    assert ("unsubscribe", "UCabc123") in hub


def test_rollback_resubscribes_the_restored_channels(store, hub):
    designer_store.api_save({"yaml": YT_YAML}, operator="op-1", live=True)
    designer_store.api_save({"yaml": YT_YAML.replace("UCabc123", "UCnext999")}, operator="op-2", live=True)
    hub.clear()
    status, payload = designer_store.api_rollback("yt-flow.yaml", {"revision": 1}, operator="op-3")
    assert status == 200 and payload["published"] is True
    assert ("subscribe", "UCabc123") in hub
    assert ("unsubscribe", "UCnext999") in hub


def test_disabling_unsubscribes_and_reports_hub_failures(store, hub, monkeypatch):
    designer_store.api_save({"yaml": YT_YAML}, operator="op-1", live=True)
    hub.clear()

    def broken(mode, channel_id, **kwargs):
        raise RuntimeError("hub down")

    monkeypatch.setattr(youtube_subscriptions, "hub_request", broken)
    status, payload = designer_store.api_toggle("yt-flow.yaml", {"enabled": False},
                                                operator="op-2")
    assert status == 200 and payload["enabled"] is False
    assert payload["warnings"] == [
        "YouTube unsubscribe for channel UCabc123 failed: hub down"]

    status, payload = designer_store.api_toggle("yt-flow.yaml", {"enabled": True},
                                                operator="op-2")
    assert status == 200
    assert payload["warnings"] == [
        "YouTube subscribe for channel UCabc123 failed: hub down"]


def test_deleting_unsubscribes_and_never_blocks_on_hub_failures(store, hub, monkeypatch):
    designer_store.api_save({"yaml": YT_YAML}, operator="op-1", live=True)
    hub.clear()
    status, payload = designer_store.api_delete("yt-flow.yaml", operator="op-2")
    assert status == 200 and payload["deleted"] is True
    assert hub == [("unsubscribe", "UCabc123")]

    designer_store.api_save({"yaml": YT_YAML}, operator="op-1", live=True)
    hub.clear()

    def broken(mode, channel_id, **kwargs):
        raise RuntimeError("hub down")

    monkeypatch.setattr(youtube_subscriptions, "hub_request", broken)
    status, payload = designer_store.api_delete("yt-flow.yaml", operator="op-2")
    assert status == 200 and payload["deleted"] is True
    assert payload["warnings"]


def test_bulk_toggle_rows_carry_warnings(store, hub, monkeypatch):
    designer_store.api_save({"yaml": YT_YAML}, operator="op-1", live=True)
    hub.clear()

    def broken(mode, channel_id, **kwargs):
        raise RuntimeError("hub down")

    monkeypatch.setattr(youtube_subscriptions, "hub_request", broken)
    status, payload = designer_store.api_bulk(
        {"ids": ["yt-flow.yaml"], "action": "disable"}, operator="op-2")
    assert status == 200 and payload["ok"] == 1
    assert payload["results"][0]["warnings"]


def test_unconfigured_push_path_saves_silently(store, monkeypatch):
    def unconfigured():
        raise RuntimeError("YouTube webhook push is not configured")

    def fail(*args, **kwargs):
        raise AssertionError("hub must not be called")

    monkeypatch.setattr(youtube_subscriptions, "_settings", unconfigured)
    monkeypatch.setattr(youtube_subscriptions, "hub_request", fail)
    status, payload = designer_store.api_save({"yaml": YT_YAML}, operator="op-1", live=True)
    assert status == 200 and payload["published"] is True
    assert "warnings" not in payload


def test_non_youtube_workflows_never_touch_the_hub(store, hub):
    yaml_text = YT_YAML.replace("connector: youtube", "connector: email").replace(
        "event: video.published", "event: message.received").replace(
        "    channel_id:\n      equals: UCabc123\n", "    route:\n      equals: yt-flow\n")
    status, payload = designer_store.api_save({"yaml": yaml_text}, operator="op-1", live=True)
    assert status == 200
    assert hub == []


# ---- CLI: the same responses, with the warnings printed ----

def _fake_api(payload):
    return lambda api_url, method, path, body=None, **kwargs: payload


def test_cli_save_prints_warnings(monkeypatch, capsys):
    from dapier_cli import api as cli_api

    monkeypatch.setattr(cli_api, "call", _fake_api({
        "file": "yt-flow.yaml", "commit": "cafe123", "published": True,
        "warnings": ["YouTube subscribe for channel UCabc123 failed: hub down"]}))
    assert cli_commands.workflows._save_workflow_yaml("https://api.example.test", "id: x", None) == 0
    out, _ = capsys.readouterr()
    assert "Warning: YouTube subscribe for channel UCabc123 failed: hub down" in out


def test_cli_toggle_prints_per_workflow_warnings(monkeypatch, capsys):
    from dapier_cli import api as cli_api

    monkeypatch.setattr(cli_api, "call", _fake_api({
        "results": [{"id": "yt-flow", "ok": True, "file": "yt-flow.yaml",
                     "enabled": False, "commit": "deadbee",
                     "warnings": ["YouTube unsubscribe for channel UCabc123 failed: x"]}],
        "ok": 1, "requested": 1, "action": "disable"}))
    assert cli_commands.workflows_set_enabled("https://api.example.test",
                                              ["yt-flow.yaml"], False) == 0
    out, _ = capsys.readouterr()
    assert "yt-flow.yaml is Off — live now." in out
    assert "Warning: YouTube unsubscribe for channel UCabc123 failed: x" in out


def test_cli_delete_prints_warnings(monkeypatch, capsys):
    from dapier_cli import api as cli_api

    monkeypatch.setattr(cli_api, "call", _fake_api({
        "file": "yt-flow.yaml", "deleted": True, "was_published": True,
        "warnings": ["YouTube unsubscribe for channel UCabc123 failed: x"]}))
    assert cli_commands.workflows_delete("https://api.example.test", "yt-flow.yaml",
                                         assume_yes=True) == 0
    out, _ = capsys.readouterr()
    assert "Warning: YouTube unsubscribe for channel UCabc123 failed: x" in out
