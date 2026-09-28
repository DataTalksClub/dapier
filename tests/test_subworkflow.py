"""The run_workflow action: in-process sub-workflow execution.

A parent workflow can call another published workflow ("run another zap"):
the target's chain runs in-process through the real engine with matching
forced (the dryrun.execute_once machinery), its outputs feed the parent's
``{steps.<id>.output...}`` templating, nesting caps at depth 2 (A→B→C runs,
A→B→C→D fails), and a workflow that is already running up the chain is
rejected as a cycle before the target executes. Dry-runs report the step
supported and never touch the target.
"""

from contextlib import contextmanager
from dataclasses import replace

import pytest

from conftest import stubbed_action

from src.dapier.connectors import registry
from src.dapier.engine import execute as engine_execute
from src.dapier.engine.actions import subworkflow
from src.dapier.engine.actions.templating import render
from src.dapier.engine.dryrun import dry_run
from src.dapier.triggers import published_workflows


@contextmanager
def rendering_slack(texts):
    """Stub the slack runner the way the real one behaves: it templates its
    text against the event and the steps captured so far, and records the
    rendered result."""
    original = registry.ACTIONS["slack"]

    def run(action, event, workflow_id, steps=None):
        texts.append(render(action["text"], event, steps))
        return {"ok": True}

    registry.ACTIONS["slack"] = replace(original, run=run)
    try:
        yield
    finally:
        registry.ACTIONS["slack"] = original


class StubPublishedTable:
    """The publish table, stubbed (mirrors test_published_workflows)."""

    def __init__(self):
        self.items = {}

    def put_item(self, Item):
        self.items[Item["workflow_id"]] = Item

    def get_item(self, Key):
        item = self.items.get(Key["workflow_id"])
        return {"Item": item} if item else {}

    def scan(self, **_):
        return {"Items": list(self.items.values())}


@pytest.fixture
def published(monkeypatch):
    """The publish table configured and stubbed; the real one never touched."""
    monkeypatch.setenv(published_workflows.TABLE_ENV, "published-test")
    table = StubPublishedTable()
    monkeypatch.setattr(published_workflows, "get_table", lambda table_ref=None: table)
    return table


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    """Managed workflow tests do not require a deployed file catalog."""
    yield tmp_path


def publish_workflow(table, workflow_id, actions, *, enabled=True,
                     trigger=None):
    workflow = {
        "id": workflow_id,
        "enabled": enabled,
        "trigger": trigger or {"connector": "email", "event": "message.received"},
        "actions": actions,
    }
    published_workflows.publish(workflow)
    return workflow


def run_parent(directory, actions, *, workflow_id="parent-wf",
               data=None):
    """Publish and fire a parent workflow whose trigger matches EVENT."""
    published_workflows.publish({
        "id": workflow_id,
        "enabled": True,
        "trigger": {"connector": "youtube", "event": "video.published"},
        "actions": actions,
    })
    return fire_youtube_event(data)


def fire_youtube_event(data=None):
    return engine_execute({
        "id": "evt-1",
        "connector": "youtube",
        "event": "video.published",
        "data": data if data is not None else {"title": "Hello"},
    })


# ---- happy path: outputs land in the parent's steps context ----

def test_subworkflow_runs_the_target_and_feeds_parent_templating(bundle, published):
    publish_workflow(published, "child-wf",
                     [{"id": "echo", "type": "dataops", "operation": "noop"}])
    seen = {}
    texts = []

    def fake_dataops(action, event):
        seen["child_data"] = event["data"]
        return {"echoed": f"echo of {event['data']['title']}"}

    with stubbed_action("dataops", fake_dataops), \
         rendering_slack(texts):
        matched = run_parent(bundle, [
            {"id": "call", "type": "run_workflow", "workflow_id": "child-wf",
             "payload": {"title": "{title}"}},
            {"id": "tell", "type": "slack", "channel": "#x",
             "text": "child said {steps.call.output.steps.echo.echoed}"},
        ])

    assert matched == ["parent-wf"]
    assert seen["child_data"] == {"title": "Hello"}  # the rendered payload is the sub-run's data
    assert texts == ["child said echo of Hello"]  # child outputs feed parent templating


def test_output_field_exposes_one_child_step(bundle, published):
    publish_workflow(published, "child-wf",
                     [{"id": "echo", "type": "dataops", "operation": "noop"}])
    texts = []
    with stubbed_action("dataops", lambda action, event: {"row": {"id": 7}}), \
         rendering_slack(texts):
        run_parent(bundle, [
            {"id": "call", "type": "run_workflow", "workflow_id": "child-wf",
             "output_field": "echo"},
            {"id": "tell", "type": "slack", "channel": "#x",
             "text": "row {steps.call.output.output.row.id}"},
        ])
    assert texts == ["row 7"]


def test_unknown_output_field_fails_with_the_childs_steps(bundle, published):
    publish_workflow(published, "child-wf",
                     [{"id": "echo", "type": "dataops", "operation": "noop"}])
    with stubbed_action("dataops", lambda action, event: {"ok": True}):
        with pytest.raises(ValueError, match="no step 'missing'"):
            run_parent(bundle, [
                {"id": "call", "type": "run_workflow", "workflow_id": "child-wf",
                 "output_field": "missing"},
            ])


# ---- payload shaping ----

def test_payload_defaults_to_the_parent_event_data(bundle, published):
    publish_workflow(published, "child-wf",
                     [{"id": "echo", "type": "dataops", "operation": "noop"}])
    seen = {}
    with stubbed_action("dataops", lambda action, event: seen.update(child=event["data"]) or {"ok": True}):
        result = subworkflow.run_workflow(
            {"id": "call", "type": "run_workflow", "workflow_id": "child-wf"},
            {"id": "evt-1", "connector": "email", "event": "message.received",
             "correlation_id": "corr-1", "data": {"subject": "hi"}},
            workflow_id="parent-wf")
    assert seen["child"] == {"subject": "hi"}
    assert result["workflow"] == "child-wf"
    assert result["steps"] == {"echo": {"ok": True}}


def test_payload_json_string_is_parsed_and_rendered(bundle, published):
    publish_workflow(published, "child-wf",
                     [{"id": "echo", "type": "dataops", "operation": "noop"}])
    seen = {}
    with stubbed_action("dataops", lambda action, event: seen.update(child=event["data"]) or {"ok": True}):
        subworkflow.run_workflow(
            {"id": "call", "type": "run_workflow", "workflow_id": "child-wf",
             "payload": "{\"subject\": \"{subject}\", \"copies\": 2}"},
            {"id": "evt-1", "connector": "email", "event": "message.received",
             "data": {"subject": "hi"}},
            workflow_id="parent-wf")
    assert seen["child"] == {"subject": "hi", "copies": 2}


def test_payload_plain_text_wraps_as_payload(bundle, published):
    publish_workflow(published, "child-wf",
                     [{"id": "echo", "type": "dataops", "operation": "noop"}])
    seen = {}
    with stubbed_action("dataops", lambda action, event: seen.update(child=event["data"]) or {"ok": True}):
        subworkflow.run_workflow(
            {"id": "call", "type": "run_workflow", "workflow_id": "child-wf",
             "payload": "plain note about {subject}"},
            {"id": "evt-1", "connector": "email", "event": "message.received",
             "data": {"subject": "hi"}},
            workflow_id="parent-wf")
    assert seen["child"] == {"payload": "plain note about hi"}


# ---- depth cap and cycles ----

def test_depth_cap_allows_three_workflows_and_blocks_the_fourth(bundle, published):
    publish_workflow(published, "wf-d", [{"id": "d-step", "type": "dataops", "operation": "noop"}])
    publish_workflow(published, "wf-c", [
        {"id": "c-step", "type": "dataops", "operation": "noop"},
        {"id": "to-d", "type": "run_workflow", "workflow_id": "wf-d"},
    ])
    publish_workflow(published, "wf-b", [
        {"id": "to-c", "type": "run_workflow", "workflow_id": "wf-c"},
    ])
    ran = []
    with stubbed_action("dataops", lambda action, event: ran.append(action["id"]) or {}):
        with pytest.raises(ValueError, match="depth cap"):
            run_parent(bundle, [
                {"id": "to-b", "type": "run_workflow", "workflow_id": "wf-b"},
            ])
    assert ran == ["c-step"]  # A→B→C executed; the fourth hop never started


def test_self_recursion_is_blocked_before_the_target_reruns(bundle, published):
    publish_workflow(published, "selfish", [
        {"id": "once", "type": "dataops", "operation": "noop"},
        {"id": "again", "type": "run_workflow", "workflow_id": "selfish"},
    ], trigger={"connector": "youtube", "event": "video.published"})
    ran = []
    with stubbed_action("dataops", lambda action, event: ran.append(action["id"]) or {}):
        with pytest.raises(ValueError, match="cannot call itself"):
            fire_youtube_event()
    assert ran == ["once"]  # the chain ran once; the self-call never re-entered it


def test_transitive_cycle_is_blocked(bundle, published):
    publish_workflow(published, "wf-b", [
        {"id": "back", "type": "run_workflow", "workflow_id": "wf-a"},
    ])
    ran = []
    with stubbed_action("dataops", lambda action, event: ran.append(action["id"]) or {}):
        with pytest.raises(ValueError, match="cannot call itself"):
            run_parent(bundle, [
                {"id": "to-b", "type": "run_workflow", "workflow_id": "wf-b"},
            ], workflow_id="wf-a")
    assert ran == []


# ---- target resolution ----

def test_missing_target_fails_with_a_clear_error(bundle, published):
    with stubbed_action("dataops", lambda action, event: {"ok": True}):
        with pytest.raises(ValueError,
                           match="no published or deployed workflow named 'ghost'"):
            run_parent(bundle, [
                {"id": "call", "type": "run_workflow", "workflow_id": "ghost"},
            ])


def test_disabled_target_is_rejected(bundle, published):
    publish_workflow(published, "child-wf",
                     [{"id": "echo", "type": "dataops", "operation": "noop"}],
                     enabled=False)
    with stubbed_action("dataops", lambda action, event: {"ok": True}):
        with pytest.raises(ValueError, match="is disabled"):
            run_parent(bundle, [
                {"id": "call", "type": "run_workflow", "workflow_id": "child-wf"},
            ])


def test_blank_workflow_id_is_rejected():
    with pytest.raises(ValueError, match="needs a workflow_id"):
        subworkflow.run_workflow(
            {"id": "call", "type": "run_workflow"},
            {"id": "evt-1", "data": {}}, workflow_id="parent-wf")


# ---- dry-run: supported, never executed ----

def test_dry_run_reports_run_workflow_supported_without_executing(bundle, published):
    publish_workflow(published, "child-wf",
                     [{"id": "echo", "type": "dataops", "operation": "noop"}])
    parent = {
        "id": "parent-wf",
        "enabled": True,
        "trigger": {"connector": "youtube", "event": "video.published"},
        "actions": [
            {"id": "call", "type": "run_workflow", "workflow_id": "child-wf",
             "payload": {"title": "{title}"}},
            {"id": "tell", "type": "slack", "channel": "#x", "text": "hi"},
        ],
    }
    ran = []
    with stubbed_action("dataops", lambda action, event: ran.append(action["id"]) or {}):
        report = dry_run(parent, {"title": "Hello"})
    assert report["mode"] == "dry-run"
    assert report["ok"] is True
    step = report["steps"][0]
    assert step["action_type"] == "run_workflow"
    assert step["ok"] is True  # registry-derived support
    assert step["rendered_input"]["workflow_id"] == "child-wf"
    assert step["rendered_input"]["payload"] == {"title": "Hello"}
    assert ran == []  # the target's runners never ran
