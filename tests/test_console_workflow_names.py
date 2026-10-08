"""Console surfaces title workflows by the API's human name; the id is the
secondary key (small mono text), the description sits behind a (?) tip."""
from pathlib import Path

from src.dapier.api.router import _static

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "web"


def test_names_module_is_served_and_reuses_the_help_tip():
    assert _static("/assets/js/workflow-names.js")["statusCode"] == 200
    js = (WEB / "js/workflow-names.js").read_text()
    assert 'class="help-tip"' in js and "data-tip=" in js
    # The console renders the API's name; it never derives one itself.
    assert "workflow.name" in js
    assert "workflow-id mono" in js


def test_workflow_list_and_home_lead_with_the_name():
    js = (WEB / "js/views/overview.js").read_text()
    assert "from '../workflow-names.js'" in js
    assert "escapeHtml(workflowName(workflow))" in js
    # The description rides in the name's title on the list (no 18px (?)
    # tip on phones); Home rows show it inline as their muted second line.
    assert "title=\"${about}\"" in js
    assert "workflow.description || workflowTriggerText(workflow)" in js
    assert "workflowIdLine(workflow)" in js
    assert "$('#workflow-title').textContent = workflowName(workflow)" in js
    # Recent runs on Home map workflow_id -> name from the loaded list.
    assert "workflowLabelHtml(run.workflow_id)" in js
    # The id stays the key every link carries.
    assert 'href="/workflows/${encodeURIComponent(workflow.id)}"' in js


def test_runs_and_agent_tasks_show_names():
    runs = (WEB / "js/views/runs.js").read_text()
    assert "workflowLabelHtml(run.workflow_id)" in runs
    assert "workflowName(run.workflow_id)" in runs
    assert "escapeHtml(workflowName(id))" in runs  # filter options
    agents = (WEB / "js/views/agents.js").read_text()
    assert "workflowName(task.workflow)" in agents


def test_designer_header_edits_the_name_not_the_id():
    js = (WEB / "js/views/designer.js").read_text()
    assert "type: 'designer:set-name', name: value" in js
    assert "designer:set-id" not in js
    assert "helpTip(description)" in js
    assert "input.maxLength = MAX_NAME_LENGTH" in js
    app = (ROOT / "designer/src/App.tsx").read_text()
    assert 'data?.type !== "designer:set-name"' in app
    assert 'nameSource: customName ? "custom" : "auto"' in app
    # A never-saved canvas lets the API assign the id from the name.
    assert "workflow.id === NEW_WORKFLOW_ID" in app
    bundle = (WEB / "designer.js").read_text()
    assert "designer:set-name" in bundle
