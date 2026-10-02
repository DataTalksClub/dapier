"""Zapier-style auto-disable on repeated failures (gap-analysis item 1).

A consecutive-failure counter per workflow (triggers.failure_counts, one row
in the cursors table) resets on a completed run; at the workflow's
``auto_pause_after`` threshold the worker stamps ``auto_paused`` onto the
stored definition (designer_store.api_auto_pause), the matcher refuses the
workflow like a disabled one, and the owner gets a pause notice. Re-enabling
(``workflows on``, the console toggle — the same api_toggle) is the resume
verb: it clears the flag and the streak on every surface with no new route.
"""

import json
import time
from datetime import datetime

import pytest
from botocore.exceptions import ClientError

from src.dapier.api import designer_store
from src.dapier.api import overview as overview_api
from src.dapier.api.designer_store import WorkflowError
from src.dapier.engine import matching
from src.dapier.engine import notify
from src.dapier.engine import worker
from src.dapier.triggers import failure_counts
from src.dapier.triggers import published_workflows


WORKFLOW = {
    "id": "flaky-flow",
    "enabled": True,
    "trigger": {"connector": "email", "event": "message.received", "filters": {}},
    "actions": [{"id": "a1", "type": "webhook", "url": "https://example.test"}],
}

EVENT = {"id": "evt-1", "connector": "email", "event": "message.received"}

WORKFLOW_YAML = """\
id: flaky-flow
enabled: true
auto_pause_after: {threshold}
trigger:
  connector: email
  event: message.received
actions:
  - id: a1
    type: webhook
    url: https://example.test/hook
"""


def failure(message="OAuth expired"):
    """An action exception the way engine.logic tags it for the worker."""
    exc = RuntimeError(message)
    exc.dapier_workflow = "flaky-flow"
    exc.dapier_step_id = "a1"
    exc.dapier_step_type = "webhook"
    return exc


class FakeCounterTable:
    """The DynamoDB semantics failure_counts relies on: one conditional
    update that ADDs to the streak and stamps the item (the condition
    honours last_event, the redelivery guard), plus get/delete/scan."""

    def __init__(self):
        self.items = {}

    def get_item(self, Key):
        item = self.items.get(Key["cursor_id"])
        return {"Item": dict(item)} if item else {}

    def update_item(self, Key, UpdateExpression=None, ConditionExpression=None,
                    ExpressionAttributeNames=None, ExpressionAttributeValues=None,
                    ReturnValues=None, **kwargs):
        values = ExpressionAttributeValues or {}
        cursor_id = Key["cursor_id"]
        item = dict(self.items.get(cursor_id) or {})
        if item.get("last_event") is not None and item["last_event"] == values[":event"]:
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}},
                              "UpdateItem")
        item.update({
            "cursor_id": cursor_id,
            "last_error": values[":error"],
            "last_event": values[":event"],
            "last_failed_at": values[":at"],
            "updated_at": values[":at"],
            "expires_at": values[":expires"],
            "fails": int(item.get("fails") or 0) + int(values[":one"]),
        })
        self.items[cursor_id] = item
        return {"Attributes": dict(item)} if ReturnValues == "ALL_NEW" else {}

    def delete_item(self, Key):
        self.items.pop(Key["cursor_id"], None)

    def scan(self, **kwargs):
        assert "ExclusiveStartKey" not in kwargs
        return {"Items": list(self.items.values())}


class StubPublishedTable:
    """The published-workflows table (test_workflow_tags's shape): the rows
    api_auto_pause and the toggle read and re-publish."""

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


class FakeSes:
    def __init__(self):
        self.calls = []

    def send_email(self, **kwargs):
        self.calls.append(kwargs)
        return {"MessageId": "ses-1"}


class FakeExecTable:
    def __init__(self):
        self.items = {}

    def put_item(self, Item, ConditionExpression=None, **kwargs):
        # The notice claim: attribute_not_exists(execution_id) is the gate.
        if ConditionExpression and Item["execution_id"] in self.items:
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}},
                              "PutItem")
        self.items[Item["execution_id"]] = Item


@pytest.fixture
def counter(monkeypatch):
    monkeypatch.setenv("CURSORS_TABLE", "cursors-test")
    table = FakeCounterTable()
    monkeypatch.setattr(failure_counts, "get_table", lambda table_ref=None: table)
    return table


@pytest.fixture
def published(monkeypatch):
    monkeypatch.setenv(published_workflows.TABLE_ENV, "published-test")
    table = StubPublishedTable()
    monkeypatch.setattr(published_workflows, "get_table", lambda table_ref=None: table)
    return table


@pytest.fixture
def live(published):
    published_workflows.publish(dict(WORKFLOW))
    return published


def live_workflow():
    return published_workflows.get_item("flaky-flow")["workflow"]


# ---------------------------------------------------------------- the store


def test_record_failure_counts_and_trips_at_the_threshold():
    table = FakeCounterTable()
    assert failure_counts.record_failure("wf", "boom", event_id="e1",
                                         threshold=3, table_ref=table) == (1, False)
    assert failure_counts.record_failure("wf", "boom", event_id="e2",
                                         threshold=3, table_ref=table) == (2, False)
    count, tripped = failure_counts.record_failure("wf", "boom", event_id="e3",
                                                   threshold=3, table_ref=table,
                                                   now=1_800_000_000)
    assert (count, tripped) == (3, True)
    item = table.items["fails#wf"]
    assert item["last_error"] == "boom"
    assert item["last_event"] == "e3"
    assert item["expires_at"] == 1_800_000_000 + failure_counts.TTL_DAYS * 86400


def test_a_redelivered_event_never_counts_twice():
    table = FakeCounterTable()
    assert failure_counts.record_failure("wf", "boom", event_id="e1",
                                         threshold=5, table_ref=table) == (1, False)
    # The failed record is retried until it leaves the queue; the same event
    # reports the count without counting again — and without re-tripping.
    assert failure_counts.record_failure("wf", "boom", event_id="e1",
                                         threshold=1, table_ref=table) == (1, False)
    assert failure_counts.record_failure("wf", "boom", event_id="e2",
                                         threshold=2, table_ref=table) == (2, True)


def test_record_without_a_threshold_counts_but_never_trips():
    table = FakeCounterTable()
    assert failure_counts.record_failure("wf", "boom", event_id="e1",
                                         table_ref=table) == (1, False)
    assert failure_counts.record_failure("wf", "boom", event_id="e2",
                                         table_ref=table) == (2, False)


def test_reset_clears_the_streak():
    table = FakeCounterTable()
    failure_counts.record_failure("wf", "boom", event_id="e1", table_ref=table)
    assert failure_counts.count("wf", table_ref=table) == 1

    failure_counts.reset("wf", table_ref=table)

    assert failure_counts.count("wf", table_ref=table) == 0


def test_reset_is_quiet_without_a_table(monkeypatch):
    monkeypatch.delenv("CURSORS_TABLE", raising=False)
    failure_counts.reset("wf")  # must not raise on the success path


def test_reset_survives_a_broken_table(monkeypatch):
    def broken(table_ref=None):
        raise RuntimeError("dynamodb down")
    monkeypatch.setenv("CURSORS_TABLE", "cursors-test")
    monkeypatch.setattr(failure_counts, "get_table", broken)
    failure_counts.reset("wf")  # best-effort, like the seen store's forget


def test_count_and_all_counts_read_back():
    table = FakeCounterTable()
    assert failure_counts.count("wf", table_ref=table) == 0
    failure_counts.record_failure("wf", "boom", event_id="e1", table_ref=table)
    assert failure_counts.count("wf", table_ref=table) == 1
    assert failure_counts.all_counts(table_ref=table) == {"wf": 1}


def test_unconfigured_store_raises(monkeypatch):
    monkeypatch.delenv("CURSORS_TABLE", raising=False)
    with pytest.raises(failure_counts.StoreError):
        failure_counts.record_failure("wf", "boom", event_id="e1")
    with pytest.raises(failure_counts.StoreError):
        failure_counts.count("wf")


def test_all_counts_reads_the_whole_store():
    table = FakeCounterTable()
    failure_counts.record_failure("wf-a", "boom", event_id="e1", table_ref=table)
    failure_counts.record_failure("wf-b", "boom", event_id="e2", table_ref=table)
    failure_counts.record_failure("wf-b", "boom", event_id="e3", table_ref=table)
    # A seen-set row shares the table; only fails# rows belong in the view.
    table.items["seen#poll#orders"] = {"cursor_id": "seen#poll#orders"}
    assert failure_counts.all_counts(table_ref=table) == {"wf-a": 1, "wf-b": 2}


# --------------------------------------------------------------- threshold


def test_auto_pause_threshold_defaults_overrides_and_disables():
    assert worker.auto_pause_threshold(None) == worker.AUTO_PAUSE_DEFAULT == 5
    assert worker.auto_pause_threshold({}) == 5
    assert worker.auto_pause_threshold({"auto_pause_after": 2}) == 2
    assert worker.auto_pause_threshold({"auto_pause_after": True}) == 5
    assert worker.auto_pause_threshold({"auto_pause_after": False}) is None
    assert worker.auto_pause_threshold({"auto_pause_after": 0}) is None
    assert worker.auto_pause_threshold({"auto_pause_after": "soon"}) == 5


# ---------------------------------------------------------------- matching


def test_matches_refuses_an_auto_paused_workflow():
    event = {"connector": "email", "event": "message.received", "data": {}}
    assert matching.matches(dict(WORKFLOW), event) is True
    assert matching.matches({**WORKFLOW, "auto_paused": True}, event) is False
    assert matching.matches({**WORKFLOW, "auto_paused": False}, event) is True
    assert matching.matches({**WORKFLOW, "enabled": False}, event) is False


# ------------------------------------------------------- the worker's hooks


def test_streak_trips_the_pause_at_the_default_threshold(counter, live, monkeypatch):
    monkeypatch.setattr(worker, "all_workflows", lambda: [dict(WORKFLOW)])
    for n in range(4):
        assert worker._auto_pause_on_failure(
            failure(), dict(EVENT, id=f"evt-{n}")) is False
    assert failure_counts.count("flaky-flow") == 4
    assert live_workflow().get("auto_paused") is None

    assert worker._auto_pause_on_failure(
        failure(), dict(EVENT, id="evt-4")) is True

    assert failure_counts.count("flaky-flow") == 5
    paused = live_workflow()
    assert paused["auto_paused"] is True
    assert paused["auto_paused_reason"] == "OAuth expired"
    assert paused["auto_paused_at"]
    assert paused["enabled"] is True  # the pause is its own state, not a disable
    assert published_workflows.list_versions("flaky-flow")[0]["cause"] == "auto-pause"


def test_an_override_threshold_trips_earlier(counter, live, monkeypatch):
    monkeypatch.setattr(worker, "all_workflows",
                        lambda: [{**WORKFLOW, "auto_pause_after": 2}])
    assert worker._auto_pause_on_failure(failure(), EVENT) is False
    assert worker._auto_pause_on_failure(
        failure(), dict(EVENT, id="evt-2")) is True
    assert live_workflow()["auto_paused"] is True


def test_auto_pause_after_false_disables_the_trip_wire(counter, live, monkeypatch):
    monkeypatch.setattr(worker, "all_workflows",
                        lambda: [{**WORKFLOW, "auto_pause_after": False}])
    for _ in range(7):
        assert worker._auto_pause_on_failure(failure(), EVENT) is False
    assert failure_counts.count("flaky-flow") == 0  # not even counting
    assert live_workflow().get("auto_paused") is None


def test_a_redelivered_failed_record_never_counts_twice(counter, live, monkeypatch):
    monkeypatch.setattr(worker, "all_workflows", lambda: [dict(WORKFLOW)])
    assert worker._auto_pause_on_failure(failure(), EVENT) is False
    assert failure_counts.count("flaky-flow") == 1
    assert worker._auto_pause_on_failure(failure(), EVENT) is False
    assert failure_counts.count("flaky-flow") == 1
    assert worker._auto_pause_on_failure(
        failure(), dict(EVENT, id="evt-2")) is False
    assert failure_counts.count("flaky-flow") == 2


def test_failure_count_store_error_never_breaks_the_failure_path(counter, live, monkeypatch):
    monkeypatch.setattr(worker, "all_workflows", lambda: [dict(WORKFLOW)])
    def broken(*args, **kwargs):
        raise failure_counts.StoreError("no cursors table")
    monkeypatch.setattr(failure_counts, "record_failure", broken)
    assert worker._auto_pause_on_failure(failure(), EVENT) is False
    assert live_workflow().get("auto_paused") is None


def test_a_stored_trigger_is_never_paused(counter, published, monkeypatch):
    """Only managed definitions can carry the flag; a stored trigger's
    synthetic workflow has nothing to pause (and no published item)."""
    trigger = {"id": "hook-orders", "trigger": WORKFLOW["trigger"]}
    monkeypatch.setattr(worker, "all_workflows", lambda: [trigger])
    for _ in range(5):
        assert worker._auto_pause_on_failure(failure(), EVENT) is False
    assert published.items == {}


def test_execute_resets_the_streak_on_a_completed_run(counter, monkeypatch):
    monkeypatch.setattr(worker, "all_workflows", lambda: [dict(WORKFLOW)])
    monkeypatch.setattr(worker, "run_chain", lambda *args, **kwargs: None)
    failure_counts.record_failure("flaky-flow", "earlier", event_id="evt-0",
                                  table_ref=counter)

    worker.execute(dict(EVENT))

    assert failure_counts.count("flaky-flow") == 0


def test_execute_keeps_the_streak_when_the_run_fails(counter, monkeypatch):
    """The exception leaves execute, so the workflow that failed keeps its
    streak — only a completed run resets it."""
    monkeypatch.setattr(worker, "all_workflows", lambda: [dict(WORKFLOW)])
    def boom(*args, **kwargs):
        raise RuntimeError("step failed")
    monkeypatch.setattr(worker, "run_chain", boom)
    failure_counts.record_failure("flaky-flow", "earlier", event_id="evt-0",
                                  table_ref=counter)

    with pytest.raises(RuntimeError):
        worker.execute(dict(EVENT))

    assert failure_counts.count("flaky-flow") == 1


def test_resume_drops_a_parked_run_of_an_auto_paused_workflow(monkeypatch):
    monkeypatch.setattr(worker, "all_workflows",
                        lambda: [{**WORKFLOW, "auto_paused": True}])
    monkeypatch.setenv("EXECUTIONS_TABLE", "executions")
    cancelled, ran = [], []
    monkeypatch.setattr(worker, "_cancel_paused_step",
                        lambda wf, action_id, ev: cancelled.append(action_id))
    monkeypatch.setattr(worker, "_is_pending", lambda *a, **k: True)
    monkeypatch.setattr(worker, "_mark_completed", lambda *a, **k: None)
    monkeypatch.setattr(worker, "_release_action", lambda *a, **k: None)
    monkeypatch.setattr(worker, "_run_connector", lambda *a, **k: ran.append(1))
    resume = {
        "workflow_id": "flaky-flow", "event": EVENT,
        "resume_at": time.time() - 5,
        "delay_action_id": "pause",
        "paused_ids": ["pause"],
        "segments": [{"steps": [{"id": "after", "type": "webhook"}],
                      "prefix": "", "scope": None}],
        "step_outputs": {},
        "run_id": "flaky-flow:evt-1",
    }

    assert worker._resume_run(resume) is None

    assert cancelled == ["pause"]  # the parked steps close cancelled, like a disable
    assert ran == []


def test_resume_resets_the_streak_when_the_run_completes(counter, monkeypatch):
    monkeypatch.setattr(worker, "all_workflows", lambda: [dict(WORKFLOW)])
    monkeypatch.setattr(worker, "_is_pending", lambda *a, **k: True)
    monkeypatch.setattr(worker, "_mark_completed", lambda *a, **k: None)
    monkeypatch.setattr(worker, "_release_action", lambda *a, **k: None)
    monkeypatch.setattr(worker, "_run_connector", lambda *a, **k: {"ok": True})
    failure_counts.record_failure("flaky-flow", "earlier", event_id="evt-0",
                                  table_ref=counter)
    resume = {
        "workflow_id": "flaky-flow", "event": EVENT,
        "resume_at": time.time() - 5,
        "delay_action_id": "pause",
        "paused_ids": ["pause"],
        "segments": [{"steps": [{"id": "after", "type": "webhook"}],
                      "prefix": "", "scope": None}],
        "step_outputs": {},
        "run_id": "flaky-flow:evt-1",
    }

    worker._resume_run(resume)

    assert failure_counts.count("flaky-flow") == 0


# ----------------------------------------------------------- handler wiring


def _handler_env(counter, live, monkeypatch, notified, streak=4):
    monkeypatch.setattr(worker, "all_workflows", lambda: [dict(WORKFLOW)])
    monkeypatch.setattr(worker, "notify_failure",
                        lambda exc, event: notified.append(("failure", event)))
    monkeypatch.setattr(worker, "notify_auto_pause",
                        lambda exc, event: notified.append(("auto-pause", event)))
    monkeypatch.setattr(worker.inbox, "record", lambda event: "inbox-1")
    monkeypatch.setattr(worker.inbox, "complete", lambda *a, **k: None)

    def boom(payload, **hooks):
        raise failure()

    monkeypatch.setattr(worker, "execute", boom)
    for n in range(streak):
        failure_counts.record_failure("flaky-flow", "earlier", event_id=f"e{n}",
                                      table_ref=counter)


def test_handler_sends_the_pause_notice_when_the_streak_trips(counter, live, monkeypatch):
    notified = []
    _handler_env(counter, live, monkeypatch, notified)

    result = worker.handler(
        {"Records": [{"messageId": "m1", "body": json.dumps(EVENT)}]}, None)

    assert result == {"batchItemFailures": [{"itemIdentifier": "m1"}]}
    assert notified == [("failure", EVENT), ("auto-pause", EVENT)]


def test_handler_stays_quiet_about_the_pause_below_the_threshold(counter, live,
                                                                 monkeypatch):
    notified = []
    _handler_env(counter, live, monkeypatch, notified, streak=1)

    result = worker.handler(
        {"Records": [{"messageId": "m1", "body": json.dumps(EVENT)}]}, None)

    assert result == {"batchItemFailures": [{"itemIdentifier": "m1"}]}
    assert notified == [("failure", EVENT)]


def test_handler_sends_the_pause_notice_for_failed_schedule_triggers(
        counter, live, monkeypatch):
    notified = []
    monkeypatch.setattr(worker, "all_workflows", lambda: [dict(WORKFLOW)])
    monkeypatch.setattr(worker, "notify_failure",
                        lambda exc, event: notified.append(("failure", event)))
    monkeypatch.setattr(worker, "notify_auto_pause",
                        lambda exc, event: notified.append(("auto-pause", event)))

    def boom(payload, **hooks):
        raise failure()

    monkeypatch.setattr(worker, "execute", boom)
    for n in range(4):
        failure_counts.record_failure("flaky-flow", "earlier", event_id=f"e{n}",
                                      table_ref=counter)

    with pytest.raises(RuntimeError):
        worker.handler({"trigger": "schedule", "schedule_id": "nightly"}, None)

    assert [kind for kind, _event in notified] == ["failure", "auto-pause"]


def test_the_pause_notice_emails_the_owner_once_per_event(monkeypatch):
    """The notice rides the executions-table claim like the failure email:
    a redelivered record cannot announce the pause twice."""
    monkeypatch.setenv("EXECUTIONS_TABLE", "executions")
    table = FakeExecTable()
    import boto3

    class Dynamo:
        def Table(self, _name):
            return table

    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())
    monkeypatch.setenv("DAPIER_EMAIL_SENDER", "ops@example.test")
    monkeypatch.setattr(matching, "all_workflows", lambda: [dict(WORKFLOW)])
    ses = FakeSes()

    first = notify.notify_auto_pause(failure(), EVENT, ses=ses)
    second = notify.notify_auto_pause(failure(), EVENT, ses=ses)

    assert first["to"] == ["ops@example.test"]
    assert second is None
    assert "flaky-flow:auto-pause-notice:evt-1" in table.items
    sent = ses.calls[0]
    assert sent["Destination"]["ToAddresses"] == ["ops@example.test"]


# ---------------------------------------------------------------- the flag


def test_api_auto_pause_stamps_the_flag_once(live):
    assert designer_store.api_auto_pause("flaky-flow", error="boom") is not None

    assert live_workflow()["auto_paused"] is True
    # Already paused: re-stamping would churn revisions on every
    # past-threshold failure, so the second call is a no-op.
    assert designer_store.api_auto_pause("flaky-flow", error="boom again") is None
    assert int(published_workflows.get_item("flaky-flow")["revision"]) == 2
    versions = published_workflows.list_versions("flaky-flow")
    assert [int(version["revision"]) for version in versions] == [2, 1]
    assert versions[0]["cause"] == "auto-pause"


def test_api_auto_pause_needs_the_store(monkeypatch):
    monkeypatch.delenv(published_workflows.TABLE_ENV, raising=False)
    assert designer_store.api_auto_pause("flaky-flow", error="boom") is None


def test_api_auto_pause_ignores_an_unknown_id(live):
    assert designer_store.api_auto_pause("no-such-flow", error="boom") is None


def test_enabling_clears_the_pause_and_the_streak(counter, live):
    designer_store.api_auto_pause("flaky-flow", error="boom")
    failure_counts.record_failure("flaky-flow", "boom", event_id="evt-9",
                                  table_ref=counter)

    status, _payload = designer_store.api_toggle("flaky-flow.yaml", {"enabled": True})

    assert status == 200
    resumed = live_workflow()
    assert resumed["enabled"] is True
    assert "auto_paused" not in resumed
    assert "auto_paused_at" not in resumed
    assert "auto_paused_reason" not in resumed
    assert failure_counts.count("flaky-flow") == 0


def test_disabling_leaves_the_pause_bookkeeping_alone(counter, live):
    designer_store.api_auto_pause("flaky-flow", error="boom")

    status, _payload = designer_store.api_toggle("flaky-flow.yaml", {"enabled": False})

    assert status == 200
    disabled = live_workflow()
    assert disabled["enabled"] is False
    assert disabled["auto_paused"] is True


def test_a_duplicate_starts_fresh(live):
    designer_store.api_auto_pause("flaky-flow", error="boom")

    status, _payload = designer_store.api_duplicate("flaky-flow.yaml", {})

    assert status == 200
    copy = published_workflows.get_item("flaky-flow-copy")["workflow"]
    assert "auto_paused" not in copy
    assert copy["enabled"] is True


def test_list_summaries_carry_the_pause_flag(live):
    _status, payload = designer_store.api_list()
    assert payload["workflows"][0]["auto_paused"] is False

    designer_store.api_auto_pause("flaky-flow", error="boom")
    _status, payload = designer_store.api_list()
    assert payload["workflows"][0]["auto_paused"] is True


# ----------------------------------------------------------------- YAML key


def test_parse_workflow_accepts_and_normalizes_auto_pause_after():
    parsed = designer_store.parse_workflow(
        WORKFLOW_YAML.format(threshold=3))
    assert parsed["auto_pause_after"] == 3

    off = designer_store.parse_workflow(
        WORKFLOW_YAML.format(threshold="false"))
    assert off["auto_pause_after"] is False

    zero = designer_store.parse_workflow(
        WORKFLOW_YAML.format(threshold=0))
    assert zero["auto_pause_after"] is False

    default = designer_store.parse_workflow(
        WORKFLOW_YAML.format(threshold="true"))
    assert "auto_pause_after" not in default  # dropped: the default applies


@pytest.mark.parametrize("threshold", ["three", -1, 2.5, [], {}])
def test_parse_workflow_rejects_a_bad_auto_pause_after(threshold):
    with pytest.raises(WorkflowError):
        designer_store.parse_workflow(WORKFLOW_YAML.format(threshold=threshold))


# ---------------------------------------------------------------- surfacing


def test_overview_view_surfaces_the_pause_and_the_streak(live):
    designer_store.api_auto_pause("flaky-flow", error="boom")

    view = overview_api._workflow_view(
        live_workflow(), "flaky-flow.yaml", published=True, failures=4)

    assert view["auto_paused"] is True
    assert view["auto_paused_reason"] == "boom"
    datetime.fromisoformat(view["auto_paused_at"])  # a real moment, not garbage
    assert view["failures"] == 4


def test_overview_view_defaults_for_an_unpaused_workflow(live):
    view = overview_api._workflow_view(live_workflow(), "flaky-flow.yaml",
                                       published=True)

    assert view["auto_paused"] is False
    assert view["auto_paused_at"] == ""
    assert view["auto_paused_reason"] == ""
    assert view["failures"] == 0
