"""Save-time WebSub subscribe: the hub sync behind a youtube trigger save.

A youtube trigger used to stay silent for up to five days until the renewal
schedule first subscribed its channel. The designer save/toggle/delete paths
now call ``youtube_subscriptions.reconcile`` (best-effort, from the designer
store): it subscribes the channels a saved workflow newly watches and
unsubscribes the ones that just became orphaned, and returns warning strings
for the save response instead of ever raising — a hub outage must not block
a save. These tests pin that contract: the deltas, the channels other live
workflows still watch being left alone, the warning shapes, and the
never-raises guarantee (including a degraded subscribe-only run when the
live workflow list is unreadable).
"""

import json
import urllib.parse
import urllib.request

import pytest

from src.dapier.triggers.intake import youtube_subscriptions


def workflow(channel_rule, connector="youtube", enabled=True, workflow_id="wf"):
    return {
        "id": workflow_id,
        **({} if enabled else {"enabled": False}),
        "trigger": {
            "connector": connector,
            "event": "video.published",
            "filters": {"channel_id": channel_rule} if channel_rule is not None else {},
        },
        "actions": [],
    }


def topic(channel_id):
    return f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"


def refuse(reason):
    return lambda *args, **kwargs: pytest.fail(reason)


class FakeSecretsClient:
    def get_secret_value(self, SecretId):
        return {"SecretString": "hub-secret"}


class FakeResponse:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class HubRecorder:
    """Stands in for urlopen: records each hub POST's WebSub form body."""

    def __init__(self, status=200, error=None):
        self.status = status
        self.error = error
        self.bodies = []

    def __call__(self, request, timeout=15):
        if self.error is not None:
            raise self.error
        body = urllib.parse.parse_qs(request.data.decode())
        self.bodies.append(body)
        return FakeResponse(self.status)

    def modes(self):
        return [body["hub.mode"][0] for body in self.bodies]

    def topics(self):
        return [body["hub.topic"][0] for body in self.bodies]


@pytest.fixture
def hub(monkeypatch):
    """A configured push path: env, fake secretsmanager, recording hub."""
    recorder = HubRecorder()
    monkeypatch.setattr(youtube_subscriptions, "boto3",
                        type("B", (), {"client": staticmethod(
                            lambda name: FakeSecretsClient())}))
    monkeypatch.setenv("YOUTUBE_WEBHOOK_SECRET_ID", "secret-id")
    monkeypatch.setenv("YOUTUBE_CALLBACK_URL", "https://dapier.example.test/hooks/youtube")
    monkeypatch.setattr(urllib.request, "urlopen", recorder)
    return recorder


def live_workflows(monkeypatch, items):
    monkeypatch.setattr(youtube_subscriptions.engine, "all_workflows", lambda: items)


def test_new_save_subscribes_its_channels_and_warns_nothing(hub, monkeypatch):
    live_workflows(monkeypatch, [])

    warnings = youtube_subscriptions.reconcile(None, workflow({"equals": "UCa"}))

    assert warnings == []
    assert hub.topics() == [topic("UCa")]
    assert hub.modes() == ["subscribe"]
    assert all(body["hub.callback"] == ["https://dapier.example.test/hooks/youtube"]
               for body in hub.bodies)
    assert all(body["hub.secret"] == ["hub-secret"] for body in hub.bodies)
    assert all(body["hub.verify"] == ["async"] for body in hub.bodies)


def test_unchanged_update_never_rings_the_hub(monkeypatch):
    monkeypatch.setattr(youtube_subscriptions.engine, "all_workflows",
                        refuse("an unchanged save must not list live workflows"))
    monkeypatch.setattr(urllib.request, "urlopen",
                        refuse("an unchanged channel list must not call the hub"))

    warnings = youtube_subscriptions.reconcile(workflow({"equals": "UCa"}),
                                               workflow({"equals": "UCa"}))

    assert warnings == []


def test_update_subscribes_only_newly_watched_channels(hub, monkeypatch):
    live_workflows(monkeypatch, [])

    warnings = youtube_subscriptions.reconcile(
        workflow({"in": ["UCa", "UCb"]}), workflow({"in": ["UCa", "UCb", "UCc"]}))

    assert warnings == []
    assert hub.topics() == [topic("UCc")]


def test_update_unsubscribes_dropped_channels(hub, monkeypatch):
    live_workflows(monkeypatch, [])

    warnings = youtube_subscriptions.reconcile(
        workflow({"equals": "UCa"}), workflow({"equals": "UCb"}))

    assert warnings == []
    assert hub.modes() == ["subscribe", "unsubscribe"]
    assert hub.topics() == [topic("UCb"), topic("UCa")]


def test_delete_unsubscribes_orphaned_channels(hub, monkeypatch):
    live_workflows(monkeypatch, [])

    warnings = youtube_subscriptions.reconcile(workflow({"equals": "UCa"}), None)

    assert warnings == []
    assert hub.modes() == ["unsubscribe"]
    assert hub.topics() == [topic("UCa")]


def test_delete_keeps_a_channel_another_workflow_still_watches(hub, monkeypatch):
    live_workflows(monkeypatch, [workflow({"equals": "UCa"}, workflow_id="other")])

    warnings = youtube_subscriptions.reconcile(workflow({"equals": "UCa"}), None)

    assert warnings == [] and not hub.bodies


def test_update_keeps_a_dropped_channel_another_workflow_still_watches(hub, monkeypatch):
    live_workflows(monkeypatch, [workflow({"equals": "UCa"}, workflow_id="other")])

    warnings = youtube_subscriptions.reconcile(workflow({"equals": "UCa"}), workflow(None))

    assert warnings == [] and not hub.bodies


def test_save_skips_a_channel_another_workflow_already_watches(hub, monkeypatch):
    live_workflows(monkeypatch, [workflow({"equals": "UCa"}, workflow_id="other")])

    warnings = youtube_subscriptions.reconcile(None, workflow({"equals": "UCa"}))

    assert warnings == [] and not hub.bodies


def test_disabling_a_save_unsubscribes_its_channel(hub, monkeypatch):
    live_workflows(monkeypatch, [])

    warnings = youtube_subscriptions.reconcile(
        workflow({"equals": "UCa"}), workflow({"equals": "UCa"}, enabled=False))

    assert warnings == []
    assert hub.modes() == ["unsubscribe"]


def test_non_youtube_saves_do_nothing(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen",
                        refuse("a non-youtube save must not call the hub"))

    warnings = youtube_subscriptions.reconcile(
        None, workflow({"equals": "UCa"}, connector="email"))

    assert warnings == []


def test_unconfigured_push_path_is_a_silent_no_op(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen",
                        refuse("no hub call without the push config"))
    monkeypatch.delenv("YOUTUBE_CALLBACK_URL", raising=False)
    monkeypatch.delenv("YOUTUBE_WEBHOOK_SECRET_ID", raising=False)

    warnings = youtube_subscriptions.reconcile(None, workflow({"equals": "UCa"}))

    assert warnings == []


def test_secret_store_failure_becomes_a_warning(hub, monkeypatch):
    def broken_secrets(name):
        raise ValueError("secrets down")

    live_workflows(monkeypatch, [])
    monkeypatch.setattr(youtube_subscriptions, "boto3",
                        type("B", (), {"client": staticmethod(broken_secrets)}))

    warnings = youtube_subscriptions.reconcile(None, workflow({"equals": "UCa"}))

    assert warnings == ["YouTube webhook sync failed: secrets down"]
    assert not hub.bodies


def test_hub_failure_on_subscribe_warns_per_channel(hub, monkeypatch):
    hub.error = RuntimeError("hub unreachable")
    live_workflows(monkeypatch, [])

    warnings = youtube_subscriptions.reconcile(None, workflow({"in": ["UCa", "UCb"]}))

    assert warnings == [
        "YouTube subscribe for channel UCa failed: hub unreachable",
        "YouTube subscribe for channel UCb failed: hub unreachable",
    ]


def test_hub_failure_on_unsubscribe_warns(hub, monkeypatch):
    def fail_unsubscribe(request, timeout=15):
        if "unsubscribe" in request.data.decode():
            raise RuntimeError("hub refused")
        return FakeResponse()

    live_workflows(monkeypatch, [])
    monkeypatch.setattr(urllib.request, "urlopen", fail_unsubscribe)

    warnings = youtube_subscriptions.reconcile(workflow({"equals": "UCa"}), None)

    assert warnings == ["YouTube unsubscribe for channel UCa failed: hub refused"]


def test_unreadable_live_workflows_degrades_to_subscribe_only(hub, monkeypatch):
    """The live set only guards unsubscription; unreadable, the sync must
    still subscribe (the point of the feature) and never raise — the save
    response has no room for a 500."""
    def broken():
        raise RuntimeError("table read failed")

    monkeypatch.setattr(youtube_subscriptions.engine, "all_workflows", broken)

    warnings = youtube_subscriptions.reconcile(None, workflow({"equals": "UCa"}))
    assert warnings == []
    assert hub.modes() == ["subscribe"]

    after_first_call = len(hub.bodies)
    warnings = youtube_subscriptions.reconcile(workflow({"equals": "UCa"}), None)
    assert warnings == []
    assert len(hub.bodies) == after_first_call  # the unsafe unsubscribe is skipped


def test_handler_still_renews_through_the_same_hub_call(hub, monkeypatch):
    live_workflows(monkeypatch, [workflow({"in": ["UCa", "UCb"]})])

    result = json.loads(youtube_subscriptions.handler({}, None)["body"])

    assert result["subscriptions"] == [
        {"channel_id": "UCa", "status": 200},
        {"channel_id": "UCb", "status": 200},
    ]
    assert hub.modes() == ["subscribe", "subscribe"]
    assert hub.topics() == [topic("UCa"), topic("UCb")]


def test_handler_without_items_subscribes_nothing(monkeypatch):
    live_workflows(monkeypatch, [])
    monkeypatch.setattr(urllib.request, "urlopen",
                        refuse("no hub call without watched channels"))
    monkeypatch.setattr(youtube_subscriptions, "boto3",
                        type("B", (), {"client": staticmethod(
                            lambda name: pytest.fail("no secret needed"))}))

    result = json.loads(youtube_subscriptions.handler({}, None)["body"])

    assert result["subscriptions"] == []
