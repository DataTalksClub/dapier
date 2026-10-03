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


def test_subscription_diagnostic_keeps_secret_server_side(monkeypatch):
    import urllib.parse
    from src.dapier.triggers.intake import youtube_subscriptions as subscriptions
    monkeypatch.setattr(subscriptions, "_settings", lambda: ("https://example.test/hooks/youtube", "server-only-secret"))
    seen = {}
    def transport(method, url, **kwargs):
        seen.update(method=method, params=urllib.parse.parse_qs(urllib.parse.urlsplit(url).query))
        return 200, b'<table><tr><th>State</th><td>verified</td></tr><tr><th>Expiration</th><td>2099-01-01 00:00:00 UTC</td></tr><tr><th>Secret</th><td>server-only-secret</td></tr></table>'
    result = subscriptions.subscription_status("UCDvErgK0j5ur3aLgn6U-LqQ", transport=transport)
    assert seen["method"] == "GET"
    assert seen["params"]["hub.secret"] == ["server-only-secret"]
    assert result == {"active": True, "state": "verified", "expires_at": "2099-01-01T00:00:00+00:00", "topic": "https://www.youtube.com/feeds/videos.xml?channel_id=UCDvErgK0j5ur3aLgn6U-LqQ", "callback": "https://example.test/hooks/youtube"}
    assert "secret" not in json.dumps(result)


def test_subscription_diagnostic_never_exposes_http_error_url(monkeypatch):
    import io
    import pytest
    from urllib.error import HTTPError
    from src.dapier.triggers.intake import youtube_subscriptions as subscriptions
    monkeypatch.setattr(subscriptions, "_settings", lambda: ("https://example.test/callback", "server-only-secret"))
    def transport(method, url, **kwargs):
        raise HTTPError(url, 400, "secret-value", {}, io.BytesIO(b"secret-value"))
    with pytest.raises(RuntimeError, match="^YouTube hub diagnostic returned HTTP 400$"):
        subscriptions.subscription_status("UCDvErgK0j5ur3aLgn6U-LqQ", transport=transport)
