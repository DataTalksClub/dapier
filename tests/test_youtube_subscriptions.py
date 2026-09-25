import json
import urllib.parse
import urllib.request

import pytest

from src.dapier.triggers.intake import youtube_subscriptions


def workflow(channel_rule, connector="youtube", enabled=True, event="video.published"):
    return {
        "id": "wf",
        **({} if enabled else {"enabled": False}),
        "trigger": {
            "connector": connector,
            "event": event,
            "filters": {"channel_id": channel_rule} if channel_rule is not None else {},
        },
        "actions": [],
    }


def test_collects_equals_channel_from_youtube_trigger():
    assert youtube_subscriptions.channels_from_workflows([
        workflow({"equals": "UCa"}),
    ]) == ["UCa"]


def test_collects_in_list_and_deduplicates_in_order():
    assert youtube_subscriptions.channels_from_workflows([
        workflow({"in": ["UCb", "UCa"]}),
        workflow({"equals": "UCa"}),
        workflow({"in": ["UCc", ""]}),
    ]) == ["UCb", "UCa", "UCc"]


def test_ignores_other_connectors_disabled_and_unfiltered_items():
    assert youtube_subscriptions.channels_from_workflows([
        workflow({"equals": "UCa"}, connector="email"),
        workflow({"equals": "UCa"}, enabled=False),
        workflow(None),
        workflow({"prefix": "UC"}),
    ]) == []


class FakeSecretsClient:
    def get_secret_value(self, SecretId):
        return {"SecretString": "hub-secret"}


class FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def test_handler_subscribes_each_configured_channel(monkeypatch):
    monkeypatch.setattr(youtube_subscriptions.engine, "all_workflows",
                        lambda: [workflow({"in": ["UCa", "UCb"]})])
    monkeypatch.setattr(youtube_subscriptions, "boto3",
                        type("B", (), {"client": staticmethod(lambda name: FakeSecretsClient())}))
    monkeypatch.setenv("YOUTUBE_WEBHOOK_SECRET_ID", "secret-id")
    monkeypatch.setenv("YOUTUBE_CALLBACK_URL", "https://dapier.example.test/hooks/youtube")
    bodies = []

    def fake_urlopen(request, timeout=15):
        bodies.append(urllib.parse.parse_qs(request.data.decode()))
        return FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    result = json.loads(youtube_subscriptions.handler({}, None)["body"])

    assert [item["channel_id"] for item in result["subscriptions"]] == ["UCa", "UCb"]
    assert all(body["hub.callback"] == ["https://dapier.example.test/hooks/youtube"]
               for body in bodies)
    assert all(body["hub.secret"] == ["hub-secret"] for body in bodies)
    assert bodies[0]["hub.topic"] == [
        "https://www.youtube.com/feeds/videos.xml?channel_id=UCa"]


def test_handler_without_items_subscribes_nothing(monkeypatch):
    monkeypatch.setattr(youtube_subscriptions.engine, "all_workflows", lambda: [])
    sent = []

    def fake_urlopen(request, timeout=15):
        sent.append(request)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(youtube_subscriptions, "boto3",
                        type("B", (), {"client": staticmethod(
                            lambda name: pytest.fail("no secret needed"))}))

    result = json.loads(youtube_subscriptions.handler({}, None)["body"])

    assert result["subscriptions"] == [] and not sent


def test_handler_reads_secret_id_from_environment(monkeypatch):
    monkeypatch.setattr(youtube_subscriptions.engine, "all_workflows",
                        lambda: [workflow({"equals": "UCa"})])
    seen = {}

    class RecordingSecrets:
        def get_secret_value(self, SecretId):
            seen["secret_id"] = SecretId
            return {"SecretString": "s"}

    monkeypatch.setattr(youtube_subscriptions, "boto3",
                        type("B", (), {"client": staticmethod(lambda name: RecordingSecrets())}))
    monkeypatch.setenv("YOUTUBE_WEBHOOK_SECRET_ID", "the-secret-id")
    monkeypatch.setenv("YOUTUBE_CALLBACK_URL", "https://dapier.example.test/hooks/youtube")
    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout=15: FakeResponse())

    youtube_subscriptions.handler({}, None)

    assert seen["secret_id"] == "the-secret-id"
