"""Tests for unified branch outputs, union convergence, fallback aliases,
hierarchical run step ordering, and dry-run branch previews."""
from pathlib import Path
from unittest import mock

import pytest
import yaml

from src.dapier.api import designer_store, runs as runs_api
from src.dapier.connectors import registry
from src.dapier.engine import dryrun, logic, naming


EVENT = {
    "id": "evt-branch-1",
    "connector": "email",
    "event": "message.received",
    "data": {"route": "invoice", "subject": "Invoice 99"},
}


def test_lookup_resolves_dotted_branch_step_ids_even_when_parent_exists():
    context = {
        "steps": {
            "route.then.make-document": {
                "status": "completed",
                "output": {"url": "https://example.test/doc.pdf"},
            },
            "route": {
                "status": "completed",
                "output": {
                    "branch": "then",
                    "last_step_id": "route.then.make-document",
                },
            },
        }
    }
    val, found = logic._lookup(context, "steps.route.then.make-document.output.url")
    assert found is True
    assert val == "https://example.test/doc.pdf"

    branch_val, branch_found = logic._lookup(context, "steps.route.output.branch")
    assert branch_found is True
    assert branch_val == "then"


def test_condition_attaches_unified_branch_summary_and_supports_dotted_when():
    seen_steps = {}

    def runner(action, event, workflow_id, steps=None):
        seen_steps.update(steps or {})
        if action["id"] == "make-document":
            return {"url": "https://example.test/inv.pdf"}
        return {"ok": True}

    chain = [
        {
            "id": "route",
            "type": "condition",
            "when": {"route": {"equals": "invoice"}},
            "then": [{"id": "make-document", "type": "webhook", "url": "https://example.test"}],
            "else": [{"id": "render-pdf", "type": "webhook", "url": "https://example.test"}],
        },
        {
            "id": "gate-after",
            "type": "filter",
            "when": {"steps.route.then.make-document.output.url": {"contains": "inv.pdf"}},
        },
        {
            "id": "final",
            "type": "webhook",
            "url": "https://example.test/final",
        },
    ]
    stop = logic.run_chain("wf-1", chain, EVENT, runner)
    assert stop is None
    route_out = seen_steps["route"]["output"]
    assert route_out["branch"] == "then"
    assert route_out["last_step_id"] == "route.then.make-document"
    assert route_out["result"] == {"url": "https://example.test/inv.pdf"}
    assert route_out["outputs"] == {"make-document": {"url": "https://example.test/inv.pdf"}}


def test_paths_supports_else_alias_and_unified_branch_summary():
    step_outputs = {}

    def runner(action, event, workflow_id, steps=None):
        return {"handled_by": action["id"]}

    chain = [
        {
            "id": "route",
            "type": "paths",
            "paths": [
                {
                    "label": "invoices",
                    "when": {"route": {"equals": "invoice"}},
                    "actions": [{"id": "inv-step", "type": "webhook", "url": "https://example.test"}],
                }
            ],
            "else": [{"id": "fallback-step", "type": "webhook", "url": "https://example.test"}],
        }
    ]
    logic.run_chain("wf-1", chain, {**EVENT, "data": {"route": "other"}}, runner,
                    step_outputs=step_outputs)
    out = step_outputs["route"]["output"]
    assert out["matched"] is None
    assert out["branch"] == "default"
    assert out["ran"] == 1
    assert out["last_step_id"] == "route.default.fallback-step"
    assert out["result"] == {"handled_by": "fallback-step"}
    assert out["outputs"] == {"fallback-step": {"handled_by": "fallback-step"}}


def test_union_coalesces_branch_outputs_and_infers_preceding_branch():
    step_outputs = {}

    def runner(action, event, workflow_id, steps=None):
        if action["id"] == "make-document":
            return {"url": "https://example.test/then-doc.pdf"}
        if action["id"] == "render-pdf":
            return {"url": "https://example.test/else-doc.pdf"}
        return {"sent": action.get("url")}

    chain = [
        {
            "id": "route",
            "type": "condition",
            "when": {"route": {"equals": "receipt"}},
            "then": [{"id": "make-document", "type": "webhook", "url": "https://example.test"}],
            "else": [{"id": "render-pdf", "type": "webhook", "url": "https://example.test"}],
        },
        {
            "id": "merge-doc",
            "type": "union",
            "fields": {
                "document_url": [
                    "{steps.route.then.make-document.output.url}",
                    "{steps.route.else.render-pdf.output.url}",
                ],
                "branch_taken": "{steps.route.output.branch}",
            },
        },
    ]
    stop = logic.run_chain("wf-1", chain, EVENT, runner, step_outputs=step_outputs)
    assert stop is None
    union_out = step_outputs["merge-doc"]["output"]
    assert union_out["from"] == "route"
    assert union_out["branch"] == "else"
    assert union_out["last_step_id"] == "route.else.render-pdf"
    assert union_out["result"] == {"url": "https://example.test/else-doc.pdf"}
    assert union_out["values"] == {
        "document_url": "https://example.test/else-doc.pdf",
        "branch_taken": "else",
    }
    assert union_out["document_url"] == "https://example.test/else-doc.pdf"
    assert union_out["branch_taken"] == "else"


def test_union_registered_and_validated_in_designer_store():
    assert "union" in registry.LOGIC
    yaml_text = """
id: union-flow
trigger:
  connector: email
  event: message.received
actions:
  - id: route
    type: paths
    paths:
      - label: invoices
        when: {route: {equals: invoice}}
        actions:
          - id: a
            type: webhook
            url: https://example.test/a
    else:
      - id: b
        type: webhook
        url: https://example.test/b
  - id: merge
    type: union
    from: route
    fields:
      target_url:
        - "{steps.route.invoices.a.output.url}"
        - "{steps.route.default.b.output.url}"
"""
    parsed = designer_store.parse_workflow(yaml_text)
    assert parsed["actions"][1]["type"] == "union"

    with pytest.raises(designer_store.WorkflowError, match="union 'from'"):
        designer_store.parse_workflow(yaml_text.replace("from: route", "from: '   '"))

    with pytest.raises(designer_store.WorkflowError, match="union 'fields'"):
        designer_store.parse_workflow(
            "id: union-flow\n"
            "trigger: {connector: email, event: message.received}\n"
            "actions:\n"
            "  - {id: merge, type: union, from: route, fields: [a, b]}\n"
        )


def test_naming_flattens_decision_cases_and_ignores_union_plumbing():
    workflow = {
        "id": "aisl-recording-agent",
        "trigger": {"connector": "zoom", "event": "recording.completed"},
        "actions": [
            {
                "id": "classify",
                "type": "decision",
                "mode": "switch",
                "prompt": "Classify {topic}",
                "choices": ["one-on-one", "ai-shipping-labs"],
                "cases": [
                    {"choice": "one-on-one", "actions": [{"id": "a1", "type": "agent"}]},
                    {"choice": "ai-shipping-labs", "actions": [{"id": "a2", "type": "agent"}]},
                ],
            },
            {"id": "join", "type": "union", "from": "classify"},
        ],
    }
    assert naming.generated_name(workflow) == "Zoom recording completed → Agent"


def test_dry_run_and_test_step_preview_branches_and_union():
    workflow = {
        "id": "branch-dry-run",
        "trigger": {"connector": "email", "event": "message.received"},
        "actions": [
            {
                "id": "route",
                "type": "condition",
                "when": {"route": {"equals": "invoice"}},
                "then": [{"id": "stamp", "type": "date_time", "operation": "now"}],
                "else": [{"id": "other", "type": "webhook", "url": "https://example.test"}],
            },
            {
                "id": "merge",
                "type": "union",
                "from": "route",
                "fields": {
                    "timestamp": "{steps.route.then.stamp.output.iso}",
                    "branch_name": "{steps.route.output.branch}",
                },
            },
        ],
    }
    report = dryrun.dry_run(workflow, {"route": "invoice"})
    assert report["ok"] is True
    by_id = {s["action_id"]: s for s in report["steps"]}
    assert "route.then.stamp" in by_id
    assert by_id["route"]["output"]["branch"] == "then"
    assert by_id["merge"]["output"]["branch"] == "then"
    assert by_id["merge"]["output"]["branch_name"] == "then"
    assert by_id["merge"]["output"]["timestamp"]

    step_report = dryrun.test_step(
        workflow,
        "merge",
        {"route": "invoice"},
        step_outputs={
            "route.then.stamp": {"status": "completed", "output": {"iso": "2026-10-13T12:00:00Z"}},
        },
    )
    assert step_report["ok"] is True
    assert step_report["steps"][0]["output"]["timestamp"] == "2026-10-13T12:00:00Z"


def test_order_steps_hierarchically_places_parent_before_retried_children():
    items = [
        {
            "execution_id": "wf:classify-recording.one-on-one.process-call:evt",
            "action_id": "classify-recording.one-on-one.process-call",
            "started_at": "2026-10-12T17:19:48+00:00",
            "status": "completed",
        },
        {
            "execution_id": "wf:classify-recording:evt",
            "action_id": "classify-recording",
            "started_at": "2026-10-12T17:26:52+00:00",
            "status": "completed",
        },
        {
            "execution_id": "wf:after-merge:evt",
            "action_id": "after-merge",
            "started_at": "2026-10-12T17:26:55+00:00",
            "status": "completed",
        },
    ]
    ordered = runs_api._order_steps_hierarchically(items)
    assert [item["action_id"] for item in ordered] == [
        "classify-recording",
        "classify-recording.one-on-one.process-call",
        "after-merge",
    ]


def test_aisl_recording_agent_workflow_files_are_valid_and_in_sync():
    root = Path(__file__).resolve().parents[1]
    docs_path = root / "docs" / "aisl-recording-agent-workflow.yaml"
    wf_path = root / "workflows" / "aisl-recording-agent.yaml"
    assert docs_path.exists() and wf_path.exists()
    docs_text = docs_path.read_text(encoding="utf-8")
    wf_text = wf_path.read_text(encoding="utf-8")
    assert docs_text == wf_text
    parsed = designer_store.parse_workflow(wf_text)
    decision = parsed["actions"][0]
    assert decision["type"] == "decision"
    descriptions = {case["choice"]: case.get("description") for case in decision["cases"]}
    assert descriptions["one-on-one"] and "one other person" in descriptions["one-on-one"]
    assert descriptions["ai-shipping-labs"] and "AI Shipping Labs" in descriptions["ai-shipping-labs"]
