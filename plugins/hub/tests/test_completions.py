import copy
from datetime import datetime, timezone
from types import SimpleNamespace

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

import src.dapier.connectors  # noqa: F401
from plugins.hub import action, completions, notifications
from src.dapier import engine
from src.dapier.triggers import inbox, poll_triggers, seen

TASK = "10000000-0000-4000-8000-000000000001"
SESSION = "20000000-0000-4000-8000-000000000002"
LAUNCH = "30000000-0000-4000-8000-000000000003"
META = {"hub_task_id": TASK, "hub_session_id": SESSION,
        "hub_link": f"https://hub.dtcdev.click/#/intake/{TASK}",
        "notify_to": "recipient@example.test", "notify_from": "agents@dtcdev.click",
        "title": "Task", "source_workflow_id": "source-flow", "source_event_id": "email-id", "step_id": "delegate"}


def hub_task(state="stopped", *, report=True, launch_id=LAUNCH):
    history = []
    if report:
        history.append({"kind": "report", "actor": "hub:service:fleet-finish",
                        "authority": "hub_launch_verified", "worker_report_received": True,
                        "source": {"reference": f"fleet-finish:{launch_id}"},
                        "text": f"Hub session {SESSION} finished (exited/done, launch {launch_id}). Worker report: delivered."})
    return {"id": TASK, "assigned_sessions": [{"hub_session_id": SESSION, "state": state,
                                               "linkage": "hub_session", "fleet_launch_id": launch_id, "updated_at": datetime.now(timezone.utc).isoformat()}],
            "payload_data": {"history": history}}


@pytest.fixture
def tables(monkeypatch):
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="eu-west-1")

        def table(name, keys):
            return ddb.create_table(TableName=name, BillingMode="PAY_PER_REQUEST",
                KeySchema=[{"AttributeName": k, "KeyType": "HASH" if i == 0 else "RANGE"} for i, k in enumerate(keys)],
                AttributeDefinitions=[{"AttributeName": k, "AttributeType": "S"} for k in keys])

        storage = table("hub-completion-storage", ["scope", "key"])
        cursors = table("hub-completion-cursors", ["cursor_id"])
        polls = table("hub-completion-polls", ["poll_id"])
        for env, value in {"STORAGE_TABLE": storage.name, "CURSORS_TABLE": cursors.name,
                           "POLL_TRIGGERS_TABLE": polls.name}.items():
            monkeypatch.setenv(env, value)
        yield storage, cursors, polls


@pytest.fixture
def flow(tables, monkeypatch):
    storage, cursors, polls = tables
    item = poll_triggers.build_item({"name": completions.POLL_ID, "source": "hub.sessions",
                                     "expression": "rate(1 minute)"}, "operator")
    polls.put_item(Item=item)
    store = completions.CompletionStore(storage)
    store.enqueue(copy.deepcopy(META))
    recorded, executed, sent = [], [], []
    monkeypatch.setattr(action, "get_hub_task", lambda task_id: hub_task())
    monkeypatch.setattr(inbox, "record", lambda event: recorded.append(copy.deepcopy(event)) or event["id"])
    monkeypatch.setattr(inbox, "complete", lambda *args, **kwargs: None)
    monkeypatch.setattr("src.dapier.engine.notify.notify_failure", lambda *args, **kwargs: None)
    monkeypatch.setattr(engine, "execute", lambda event, **kwargs: executed.append(copy.deepcopy(event)) or [])
    monkeypatch.setattr(completions, "send_notification", lambda metadata, result: sent.append(copy.deepcopy(result)) or "ses-id")
    return SimpleNamespace(store=store, item=item, recorded=recorded, executed=executed, sent=sent, cursors=cursors)


@pytest.mark.parametrize("state", ["held", "uncertain", "running", "queued", "starting"])
def test_nonterminal_states_never_complete(state):
    assert completions.completion_for(META, hub_task(state)) is None


def test_report_must_match_canonical_session_and_fleet():
    task = hub_task()
    valid = copy.deepcopy(task["payload_data"]["history"][0])
    task["assigned_sessions"].insert(0, {"hub_session_id": "other", "state": "failed"})
    task["payload_data"]["history"].extend([
        {**valid, "actor": "human:operator", "text": "DONE"},
        {**valid, "source": {"reference": "fleet-finish:other"}},
        {**valid, "authority": "untrusted_task_data"},
    ])
    result = completions.completion_for(META, task)
    assert result["finish_report"] == valid
    assert result["state"] == "stopped"
    assert result["worker_report_status"] == "done"
    assert result["fleet_state"] == "exited"
    assert "success" not in result


def test_terminal_state_waits_for_correlated_report():
    assert completions.completion_for(META, hub_task(report=False)) is None
    result = completions.completion_for(META, hub_task("refused", report=False, launch_id=None))
    assert result["state"] == "refused"
    assert result["finish_report"] is None
    assert result["worker_report_status"] is None


def test_worker_prose_cannot_claim_structured_success():
    task = hub_task()
    task["payload_data"]["history"][0]["text"] = "Arbitrary worker prose: done, succeeded!"
    result = completions.completion_for(META, task)
    assert result["worker_report_status"] is None
    assert result["fleet_state"] is None


def test_visibility_and_wrong_task_are_errors():
    task = hub_task()
    task["assigned_sessions"][0] = {"hub_session_id": SESSION, "linkage": "not_visible"}
    with pytest.raises(RuntimeError, match="not visible"):
        completions.completion_for(META, task)
    with pytest.raises(RuntimeError, match="different task"):
        completions.completion_for(META, {**task, "id": "other"})


def test_pure_fetch_and_cursor_reset_do_not_send_or_mark_delivery(flow, monkeypatch):
    def no_effect(*args, **kwargs):
        pytest.fail("read-only fetch invoked an execution hook")
    source = poll_triggers.poll_sources.resolve("hub.sessions")
    monkeypatch.setattr(source, "prepare", no_effect)
    monkeypatch.setattr(source, "ack", no_effect)
    rows_before = flow.store.table.scan()["Items"]
    items, cursor = poll_triggers.fetch_page_response(flow.item)
    assert len(items) == 1
    assert cursor == ""
    assert flow.sent == []
    assert flow.store.table.scan()["Items"] == rows_before


def test_event_and_email_deliver_once_then_replay_cannot_requeue(flow):
    first = poll_triggers.fire(completions.POLL_ID)
    assert first["fired"] == 1
    assert len(flow.sent) == len(flow.executed) == len(flow.recorded) == 1
    event = flow.recorded[0]
    assert event["connector"] == "hub"
    assert event["event"] == "session.finished"
    assert event["data"]["finish_report"]["actor"] == "hub:service:fleet-finish"
    assert "notify_to" not in event["data"]
    assert flow.store.pending(SESSION) is None
    flow.store.enqueue(copy.deepcopy(META))
    assert poll_triggers.fire(completions.POLL_ID)["fired"] == 0
    assert len(flow.sent) == 1


def test_event_failure_does_not_drop_or_repeat_successful_email(flow, monkeypatch):
    attempts = []

    def execute(event, **kwargs):
        attempts.append(event["id"])
        if len(attempts) == 1:
            raise RuntimeError("fixture follow-up failure")
        return []
    monkeypatch.setattr(engine, "execute", execute)
    assert poll_triggers.fire(completions.POLL_ID)["fired"] == 0
    assert len(flow.sent) == 1
    assert not flow.store.event_delivered(SESSION)
    monkeypatch.setattr(action, "get_hub_task", lambda task_id: pytest.fail("persisted result must survive Hub outage"))
    assert poll_triggers.fire(completions.POLL_ID)["fired"] == 1
    assert attempts[0] == attempts[1]
    assert len(flow.sent) == 1
    assert flow.store.pending(SESSION) is None


def test_email_failure_does_not_drop_or_repeat_event_even_after_seen_reset(flow, monkeypatch):
    clock, sends = [1000], []
    monkeypatch.setattr(completions, "time", SimpleNamespace(time=lambda: clock[0]))

    def send(metadata, result):
        sends.append(result["hub_session_id"])
        if len(sends) == 1:
            raise TimeoutError("fixture SES timeout")
        return "ses-retry-id"
    monkeypatch.setattr(completions, "send_notification", send)
    assert poll_triggers.fire(completions.POLL_ID)["fired"] == 1
    assert flow.store.event_delivered(SESSION)
    assert flow.store.pending(SESSION) is not None
    # A lease prevents rapid duplicate sends after an ambiguous timeout.
    assert poll_triggers.fire(completions.POLL_ID)["fired"] == 0
    assert len(sends) == 1
    seen.drop(f"poll#{completions.POLL_ID}")
    clock[0] = 1201
    monkeypatch.setattr(action, "get_hub_task", lambda task_id: pytest.fail("email retry must not depend on Hub"))
    assert poll_triggers.fire(completions.POLL_ID)["fired"] == 0
    assert sends == [SESSION, SESSION]
    assert len(flow.executed) == 1
    assert flow.store.pending(SESSION) is None


def test_no_recipient_completes_without_email(flow, monkeypatch):
    # A separate job is used because pending metadata is intentionally immutable.
    flow.store.table.delete_item(Key={"scope": completions.SYSTEM_SCOPE, "key": completions.PENDING_PREFIX + SESSION})
    flow.store.enqueue({**META, "notify_to": ""})
    monkeypatch.setattr(completions, "send_notification", lambda *args: pytest.fail("no recipient configured"))
    assert poll_triggers.fire(completions.POLL_ID)["fired"] == 1
    assert flow.store.pending(SESSION) is None


def test_held_and_missing_report_keep_pending_until_real_finish(flow, monkeypatch):
    current = [hub_task("held", report=False)]
    monkeypatch.setattr(action, "get_hub_task", lambda task_id: current[0])
    assert poll_triggers.fire(completions.POLL_ID)["fired"] == 0
    current[0] = hub_task(report=False)
    assert poll_triggers.fire(completions.POLL_ID)["fired"] == 0
    assert flow.sent == []
    current[0] = hub_task()
    assert poll_triggers.fire(completions.POLL_ID)["fired"] == 1


def test_pagination_cycles_to_first_page_and_rejects_other_partitions(tables):
    store = completions.CompletionStore(tables[0])
    for i in range(7):
        store.enqueue({**META, "hub_session_id": f"20000000-0000-4000-8000-{i:012d}"})
    first, cursor = store.page()
    second, end = store.page(cursor)
    assert len(first) == 5 and len(second) == 2 and end == ""
    assert store.page(end)[0] == first
    with pytest.raises(ValueError, match="cursor"):
        store.page('{"scope":"other-workflow","key":"hub-session/pending/x"}')


def test_notification_lease_fences_competing_senders(tables):
    store = completions.CompletionStore(tables[0])
    old = store.claim_notification(SESSION, now=1000)
    assert old
    assert store.claim_notification(SESSION, now=1001) is None
    new = store.claim_notification(SESSION, now=1120)
    assert new and new != old
    with pytest.raises(ClientError):
        store.mark_notified(SESSION, old, "old-message")
    store.mark_notified(SESSION, new, "message")
    assert store.claim_notification(SESSION, now=9999) is None


@pytest.mark.parametrize("change", [{"name": "another-poll"}, {"cursor_mode": "watermark"}, {"id_path": "source_workflow_id"}])
def test_source_save_enforces_single_stream_and_stable_session_identity(change):
    with pytest.raises(poll_triggers.TriggerError):
        poll_triggers.build_item({"name": completions.POLL_ID, "source": "hub.sessions",
                                 "expression": "rate(1 minute)", **change}, "operator")


def test_sender_default_and_notification_payload(monkeypatch):
    metadata = notifications.configuration({"id": "start"},
        {"id": "email", "connector": "email", "data": {"sender": "Sender <recipient@example.test>"}}, "flow", title="Title\ncontinued")
    assert metadata["notify_to"] == "recipient@example.test"
    assert metadata["notify_from"] == "agents@dtcdev.click"
    calls = []

    class SES:
        def send_email(self, **kwargs):
            calls.append(kwargs)
            return {"MessageId": "ses-fixture"}

    def client(name, *, config):
        assert name == "ses"
        assert config.read_timeout == 3
        assert config.retries["total_max_attempts"] == 1
        return SES()
    monkeypatch.setattr(boto3, "client", client)
    result = completions.completion_for({**META, **metadata}, hub_task())
    assert notifications.send_notification(metadata, result) == "ses-fixture"
    assert calls[0]["Destination"] == {"ToAddresses": ["recipient@example.test"]}
    assert "\n" not in calls[0]["Message"]["Subject"]["Data"]
    assert result["finish_report"]["text"] in calls[0]["Message"]["Body"]["Text"]["Data"]

@pytest.mark.parametrize("state", ["stopped", "failed", "cancelled"])
def test_missing_report_after_grace_is_unknown(state):
    task = hub_task(state, report=False)
    task["assigned_sessions"][0]["updated_at"] = "1970-01-01T00:16:40.000Z"
    assert completions.completion_for(META, task, now=1299) is None
    result = completions.completion_for(META, task, now=1300)
    assert result["state"] == state
    assert result["finish_report_status"] == "unavailable"
    assert result["worker_report_status"] is result["fleet_state"] is result["finish_report"] is None


def test_inaccessible_task_does_not_block_healthy_or_cached_jobs(flow, monkeypatch):
    bad = {**META, "hub_session_id": "00000000-0000-4000-8000-000000000001", "hub_task_id": "missing"}
    cached = {**META, "hub_session_id": "00000000-0000-4000-8000-000000000002", "hub_task_id": "cached"}
    flow.store.enqueue(bad)
    flow.store.enqueue(cached)
    snapshot = {**completions.completion_for(META, hub_task()), "id": cached["hub_session_id"], "hub_session_id": cached["hub_session_id"]}
    flow.store.journal.once("result/" + cached["hub_session_id"], snapshot)
    def get_task(task_id):
        if task_id == TASK:
            return hub_task()
        if task_id == "cached":
            pytest.fail("durable result should not query Hub")
        raise RuntimeError("not visible")
    monkeypatch.setattr(action, "get_hub_task", get_task)
    assert poll_triggers.fire(completions.POLL_ID)["fired"] == 2
    assert flow.store.pending(bad["hub_session_id"]) is not None
    assert len(flow.sent) == 2


def test_snapshot_failure_blocks_publication_until_recovery(flow, monkeypatch):
    original = completions.DynamoJournal.once
    def fail_snapshot(self, key, value):
        if key.startswith("result/"):
            raise RuntimeError("storage unavailable")
        return original(self, key, value)
    monkeypatch.setattr(completions.DynamoJournal, "once", fail_snapshot)
    assert poll_triggers.fire(completions.POLL_ID)["fired"] == 0
    assert flow.sent == flow.executed == []
    monkeypatch.setattr(completions.DynamoJournal, "once", original)
    assert poll_triggers.fire(completions.POLL_ID)["fired"] == 1


def test_accepted_email_failed_receipt_can_duplicate_after_lease(flow, monkeypatch):
    clock = [1000]
    monkeypatch.setattr(completions, "time", SimpleNamespace(time=lambda: clock[0]))
    original = completions.CompletionStore.mark_notified
    def fail_receipt(*args):
        raise RuntimeError("receipt failed")
    monkeypatch.setattr(completions.CompletionStore, "mark_notified", fail_receipt)
    assert poll_triggers.fire(completions.POLL_ID)["fired"] == 1
    assert len(flow.sent) == 1
    monkeypatch.setattr(completions.CompletionStore, "mark_notified", original)
    assert poll_triggers.fire(completions.POLL_ID)["fired"] == 0
    assert len(flow.sent) == 1
    clock[0] += 121
    assert poll_triggers.fire(completions.POLL_ID)["fired"] == 0
    assert len(flow.sent) == 2  # SES has no caller-provided idempotency key.
    assert flow.store.pending(SESSION) is None


def test_designer_sample_has_honest_completion_contract(monkeypatch):
    from plugins.hub.plugin import _completion_sample
    from src.dapier.connectors import trigger_discovery
    monkeypatch.setattr(trigger_discovery, "history_sample", lambda *args: None)
    result = _completion_sample()
    assert result["source"] == "synthetic"
    assert result["sample"]["connector"] == "hub"
    assert result["sample"]["event"] == "session.finished"
    assert result["sample"]["data"]["finish_report_status"] == "available"


def test_worker_notification_lease_permission_is_scoped():
    from pathlib import Path
    import yaml
    class CfnLoader(yaml.SafeLoader):
        pass
    def intrinsic(loader, tag, node):
        value = (loader.construct_scalar(node) if isinstance(node, yaml.ScalarNode)
                 else loader.construct_sequence(node, deep=True))
        return {tag: value}
    CfnLoader.add_multi_constructor("!", intrinsic)
    template = yaml.load(Path("template.yaml").read_text(), Loader=CfnLoader)
    statements = [s for p in template["Resources"]["WorkerFunction"]["Properties"]["Policies"]
                  for s in (p.get("Statement", []) if isinstance(p.get("Statement", []), list) else [p["Statement"]])]
    lease = next(s for s in statements if s.get("Sid") == "HubCompletionNotificationLease")
    assert lease["Action"] == ["dynamodb:UpdateItem"]
    assert lease["Resource"] == {"GetAtt": "StorageTable.Arn"}
    assert lease["Condition"] == {"ForAllValues:StringEquals": {"dynamodb:LeadingKeys": ["system:hub-sessions"]}}


def test_nonemail_event_does_not_inherit_recipient_from_arbitrary_sender_field():
    metadata = notifications.configuration({"id": "start"},
        {"id": "event", "connector": "webhook", "data": {"sender": "arbitrary@example.test"}}, "flow", title="Task")
    assert metadata["notify_to"] == ""
