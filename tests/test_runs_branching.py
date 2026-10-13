"""Console and CLI run view for branching workflows (`condition`, `paths`,
`decision`, `filter`, `union`, and recursive nested branch chains)."""
import json
import re
from pathlib import Path

import pytest
from py_mini_racer import MiniRacer

from dapier_cli import commands, main

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "web"
ICONS_JS = (WEB / "js" / "icons.js").read_text(encoding="utf-8")
RUNS_JS = (WEB / "js" / "views" / "runs.js").read_text(encoding="utf-8")
APP_CSS = (WEB / "app.css").read_text(encoding="utf-8")


@pytest.fixture
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("DAPIER_API_URL", raising=False)
    return tmp_path


def _runs_ctx():
    """Evaluate runs.js helpers (`branchVerdict`, `stepCard`, `groupStepsTree`,
    `flow`) inside MiniRacer with DOM/format stubs."""
    # Extract the pure flow/branch portion of runs.js (avoiding top-level DOM
    # event listeners on `#runs-*`).
    step_icons_start = RUNS_JS.index("const STEP_ICONS =")
    verdict_end = RUNS_JS.index("function triggerLabel(")
    card_start = RUNS_JS.index("export function stepCard(")
    open_run_start = RUNS_JS.index("export async function openRun(")

    snippet = (
        RUNS_JS[step_icons_start:verdict_end]
        + "\n"
        + RUNS_JS[card_start:open_run_start]
    ).replace("export function ", "function ")

    ctx = MiniRacer()
    ctx.eval(
        """
        const escapeHtml = (v) => String(v ?? '')
          .replace(/&/g, '&amp;')
          .replace(/</g, '&lt;')
          .replace(/>/g, '&gt;')
          .replace(/"/g, '&quot;');
        const statusLine = (s) => `<span class="status">${escapeHtml(s)}</span>`;
        const formatTimestamp = (ts) => ts ? String(ts) : '';
        const formatDuration = (ms) => ms != null ? `${ms}ms` : '';
        const dataBlock = (val) => val ? `<pre class="json-block">${escapeHtml(JSON.stringify(val))}</pre>` : '';
        const triggerLabel = (run) => `${run.connector || '?'} · ${run.event_type || '?'}`;
        const sentenceCase = (s) => s ? s.charAt(0).toUpperCase() + s.slice(1) : '';
        """
        + snippet
    )
    return ctx


def test_icons_js_includes_branching_and_logic_glyphs():
    ctx = MiniRacer()
    ctx.eval(ICONS_JS.replace("export function ", "function "))
    for name in ("git-branch", "git-merge", "filter", "sparkles", "repeat",
                 "layers", "inbox", "terminal", "cpu"):
        svg = ctx.eval(f"icon({json.dumps(name)})")
        assert "viewBox='0 0 24 24'" in svg
        assert "stroke='currentColor'" in svg
        assert "stroke-width='1.8'" in svg
        # Must not fall back to the 'x' icon path.
        assert "M 18 6 6 18" not in svg, f"icon({name!r}) fell back to 'x'"


def test_step_icons_cover_logic_and_ai_steps():
    ctx = _runs_ctx()
    mapping = json.loads(ctx.eval("JSON.stringify(STEP_ICONS)"))
    assert mapping["condition"] == "git-branch"
    assert mapping["paths"] == "git-branch"
    assert mapping["decision"] == "sparkles"
    assert mapping["filter"] == "filter"
    assert mapping["union"] == "git-merge"
    assert mapping["for_each"] in ("repeat", "layers")
    assert mapping["delay"] == "clock"
    assert mapping["digest"] in ("inbox", "layers")
    assert mapping["ai_complete"] == "sparkles"
    assert mapping["agent"] in ("terminal", "cpu")


def test_branch_verdict_formats_all_branching_primitives():
    ctx = _runs_ctx()

    cond_then = json.loads(ctx.eval(
        "JSON.stringify(branchVerdict({action_type: 'condition', output: {condition: 'passed', branch: 'then'}}))"
    ))
    assert cond_then == {
        "kind": "condition",
        "branch": "then",
        "badge": "Branch → then",
        "passed": True,
    }

    cond_else = json.loads(ctx.eval(
        "JSON.stringify(branchVerdict({action_type: 'condition', output: {condition: 'failed'}}))"
    ))
    assert cond_else == {
        "kind": "condition",
        "branch": "else",
        "badge": "Branch → else",
        "passed": False,
    }

    paths_match = json.loads(ctx.eval(
        "JSON.stringify(branchVerdict({action_type: 'paths', output: {matched: 'invoices', ran: 1}}))"
    ))
    assert paths_match == {
        "kind": "paths",
        "branch": "invoices",
        "badge": "Path → invoices",
    }

    paths_else = json.loads(ctx.eval(
        "JSON.stringify(branchVerdict({action_type: 'paths', output: {matched: null, ran: 1}}))"
    ))
    assert paths_else == {
        "kind": "paths",
        "branch": "else",
        "badge": "Path → else",
    }

    paths_none = json.loads(ctx.eval(
        "JSON.stringify(branchVerdict({action_type: 'paths', output: {matched: null, ran: 0}}))"
    ))
    assert paths_none == {
        "kind": "paths",
        "branch": None,
        "badge": "No path matched",
    }

    decision = json.loads(ctx.eval(
        "JSON.stringify(branchVerdict({action_type: 'decision', output: {choice: 'one-on-one', branch: 'one-on-one', confidence: 0.98}}))"
    ))
    assert decision == {
        "kind": "decision",
        "branch": "one-on-one",
        "choice": "one-on-one",
        "confidence": 0.98,
        "badge": "Decision → one-on-one · 98%",
    }

    union = json.loads(ctx.eval(
        "JSON.stringify(branchVerdict({action_type: 'union', output: {from: 'route', branch: 'then'}}))"
    ))
    assert union == {
        "kind": "union",
        "branch": "then",
        "badge": "Union ← then",
    }

    filt = json.loads(ctx.eval(
        "JSON.stringify(branchVerdict({action_type: 'filter', output: {filter: 'passed'}}))"
    ))
    assert filt == {
        "kind": "filter",
        "branch": "pass",
        "badge": "Filter → passed",
    }


def test_flow_groups_decision_child_steps_and_normalizes_replay_order():
    ctx = _runs_ctx()
    # Simulate a replayed zoom-recording-router / aisl-recording-agent run where
    # the child step appears before the parent step in `steps`.
    run_data = {
        "run": {
            "run_id": "zoom-recording-router:evt-1",
            "workflow_id": "zoom-recording-router",
            "connector": "zoom",
            "event_type": "recording.completed",
            "status": "completed",
            "started_at": "2026-10-13T10:00:00Z",
        },
        "steps": [
            {
                "action_id": "classify-recording.one-on-one.process-call",
                "action_type": "agent",
                "status": "completed",
                "duration_ms": 183,
                "output": {"status": "queued"},
            },
            {
                "action_id": "classify-recording",
                "action_type": "decision",
                "status": "completed",
                "duration_ms": 410,
                "output": {
                    "branch": "one-on-one",
                    "choice": "one-on-one",
                    "confidence": 0.98,
                },
            },
        ],
    }
    html = ctx.eval(f"flow({json.dumps(run_data)})")

    # Parent step renders before child step.
    assert html.index("classify-recording</span>") < html.index("process-call</span>")
    # Branch outcome pill is in the header.
    assert '<span class="flow-branch-pill">Decision → one-on-one · 98%</span>' in html
    # Branch lane header names the branch taken.
    assert 'Branch taken: <strong>one-on-one</strong>' in html
    # Child step sits inside .flow-branch-steps with clean local title and full scoped subtitle.
    assert '<ol class="flow-branch-steps">' in html
    assert '<span class="flow-title mono">process-call</span>' in html
    assert '<span class="flow-sub mono">classify-recording.one-on-one.process-call · agent · 183ms</span>' in html


def test_flow_supports_recursive_nested_conditions_and_union_dividers():
    ctx = _runs_ctx()
    run_data = {
        "run": {
            "run_id": "todo-intake:evt-1",
            "workflow_id": "todo-intake",
            "connector": "telegram",
            "event_type": "message.received",
            "status": "completed",
        },
        "steps": [
            {
                "action_id": "append-task",
                "action_type": "condition",
                "status": "completed",
                "duration_ms": 12,
                "output": {"condition": "passed", "branch": "then", "steps": 1},
            },
            {
                "action_id": "append-task.then.nonempty-task",
                "action_type": "condition",
                "status": "completed",
                "duration_ms": 8,
                "output": {"condition": "passed", "branch": "then", "steps": 1},
            },
            {
                "action_id": "append-task.then.nonempty-task.then.append-row",
                "action_type": "sheets_append_row",
                "status": "completed",
                "duration_ms": 240,
                "output": {"updated_rows": 1},
            },
            {
                "action_id": "notify-done",
                "action_type": "slack",
                "status": "completed",
                "duration_ms": 95,
                "output": {"ok": True},
            },
        ],
    }
    html = ctx.eval(f"flow({json.dumps(run_data)})")

    assert html.count('class="flow-branch-lane"') == 2
    assert '<span class="flow-title mono">nonempty-task</span>' in html
    assert '<span class="flow-title mono">append-row</span>' in html
    assert 'append-task.then.nonempty-task.then.append-row · sheets_append_row · 240ms' in html
    # Implicit continuation before the next top-level step renders the union divider.
    assert '<li class="flow-union-divider" aria-label="Branches merged"><span>Union · flow continues</span></li>' in html


def test_flow_renders_empty_branch_note_and_skips_divider_before_explicit_union():
    ctx = _runs_ctx()
    run_data = {
        "run": {"run_id": "wf-union:evt-1", "status": "completed"},
        "steps": [
            {
                "action_id": "route",
                "action_type": "condition",
                "status": "completed",
                "output": {"condition": "failed", "branch": "else", "steps": 0},
            },
            {
                "action_id": "merge-doc",
                "action_type": "union",
                "status": "completed",
                "output": {"from": "route", "branch": "else"},
            },
        ],
    }
    html = ctx.eval(f"flow({json.dumps(run_data)})")

    assert "No steps in else branch — continued" in html
    assert '<span class="flow-branch-pill">Union ← else</span>' in html
    # Explicit union step renders its own card, so no extra .flow-union-divider is injected.
    assert "flow-union-divider" not in html


def test_app_css_styles_branch_lanes_with_dk_tokens_only():
    for selector in (
        ".flow-branch-pill",
        ".flow-branch-lane",
        ".flow-branch-header",
        ".flow-branch-steps",
        ".flow-branch-empty",
        ".flow-union-divider",
    ):
        assert selector in APP_CSS, f"{selector} missing from app.css"
    start = APP_CSS.index(".flow-branch-pill")
    end = APP_CSS.index(".flow-data {", start)
    block = APP_CSS[start:end]
    assert not re.search(r"#[0-9a-fA-F]{3,8}", block), "raw hex color found in branch flow CSS"
    assert "@media (max-width: 560px)" in block


def test_cli_runs_show_orders_and_indents_branch_steps(isolated_home, monkeypatch, capsys):
    response = {
        "run": {
            "run_id": "zoom-recording-router:evt-1",
            "workflow_id": "zoom-recording-router",
            "status": "completed",
            "connector": "zoom",
            "event_type": "recording.completed",
            "steps": 4,
        },
        "steps": [
            {
                "action_id": "classify-recording.one-on-one.process-call",
                "action_type": "agent",
                "status": "completed",
                "duration_ms": 183,
                "output": {"status": "queued"},
            },
            {
                "action_id": "classify-recording",
                "action_type": "decision",
                "status": "completed",
                "duration_ms": 410,
                "output": {"branch": "one-on-one", "choice": "one-on-one", "confidence": 0.98},
            },
            {
                "action_id": "route-followup",
                "action_type": "paths",
                "status": "completed",
                "duration_ms": 15,
                "output": {"matched": "vip", "ran": 0},
            },
            {
                "action_id": "merge-out",
                "action_type": "union",
                "status": "completed",
                "duration_ms": 5,
                "output": {"from": "classify-recording", "branch": "one-on-one"},
            },
        ],
    }
    monkeypatch.setattr(commands.api, "call", lambda *a, **kw: response)

    rc = main.main(["runs", "show", "zoom-recording-router:evt-1"])
    assert rc == 0
    out = capsys.readouterr().out

    # Parent step is ordered before its child step and prints the branch verdict.
    parent_pos = out.index("step[1]: classify-recording (decision)  completed  410ms")
    child_pos = out.index("  step[2]: classify-recording.one-on-one.process-call (agent)  completed  183ms")
    assert parent_pos < child_pos
    assert "  branch: one-on-one (98%)" in out
    assert "  branch: vip" in out
    assert "  branch: one-on-one (union)" in out
