"""Console surfaces title workflows by the API's human name. The Workflows
list is Zapier-style: one line per workflow (trigger-kind icon + name), an
Apps strip of the brand marks the flow touches, Location, Latest run, State.
The raw id and the When/Then text are off the row (hover, row menu, YAML)."""
import re
from pathlib import Path

from py_mini_racer import MiniRacer

from src.dapier.api.router import _static

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "web"
OVERVIEW = (WEB / "js/views/overview.js").read_text()
NAMES = (WEB / "js/workflow-names.js").read_text()


def _list_row_template():
    start = OVERVIEW.index("export function renderWorkflows")
    end = OVERVIEW.index("function selectableFiles", start)
    return OVERVIEW[start:end]


def _names_ctx(catalog_labels=None):
    """workflow-names.js with its imports stubbed: catalog labels from the
    given dict, marks as <i data-mark="…">."""
    body = re.sub(r"^import .*?;\n", "", NAMES, flags=re.M).replace("export ", "")
    labels = catalog_labels or {}
    ctx = MiniRacer()
    ctx.eval("""
    const state = { data: { workflows: [] } };
    const escapeHtml = (v) => String(v ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
    const providerMark = (key) => `<i data-mark="${key}"></i>`;
    const icon = (name, cls) => `<i data-icon="${name}" class="${cls}"></i>`;
    const LABELS = %s;
    const eventLabel = (connector, event) => LABELS[`${connector}:${event}`] || `${connector} ${event}`;
    const actionLabel = (type) => LABELS[type] || type;
    const connectorLabel = (name) => LABELS[name] || null;
    """ % __import__("json").dumps(labels) + body)
    return ctx


def test_names_module_is_served_and_reuses_the_help_tip():
    assert _static("/assets/js/workflow-names.js")["statusCode"] == 200
    assert 'class="help-tip"' in NAMES and "data-tip=" in NAMES
    # The console renders the API's name; it never derives one itself.
    assert "workflow.name" in NAMES


def test_workflow_list_and_home_lead_with_the_name():
    assert "from '../workflow-names.js'" in OVERVIEW
    assert "escapeHtml(workflowName(workflow))" in OVERVIEW
    assert "$('#workflow-title').textContent = workflowName(workflow)" in OVERVIEW
    # Recent runs on Home map workflow_id -> name from the loaded list.
    assert "workflowLabelHtml(run.workflow_id)" in OVERVIEW
    # The id stays the key every link carries.
    assert 'href="/workflows/${encodeURIComponent(workflow.id)}"' in OVERVIEW


def test_list_rows_carry_no_raw_id_text_and_no_second_line():
    row = _list_row_template()
    # No id line, no When/Then block: the name is the only text line.
    assert "workflowIdLine" not in row and "workflow-id" not in row
    assert "workflow-flow" not in row and ">When<" not in row and ">Then<" not in row
    assert "cell-sub" not in row and "cell-tags" not in row
    # The id appears only inside attributes (data-*, href, the Copy ID
    # menu item's tooltip) — never as visible cell text.
    visible = re.sub(r'"[^"]*\$\{id\}[^"]*"', '""', row)
    assert ">${id}<" not in visible and "${id}</" not in visible
    # Copy ID sits in the row menu beside the existing actions.
    menu = row[row.index('class="workflow-menu"'):]
    for action in ("workflow-runs", "workflow-versions", "workflow-tags", "workflow-folder",
                   "workflow-duplicate", "workflow-copy-id", "workflow-delete"):
        assert action in menu, action
    # The full flow is one hover away on the name and the Apps strip.
    assert "workflowFlowText(workflow)" in row and "appStripHtml(workflow, flow)" in row
    # Search still matches ids.
    assert "${workflow.id} ${workflow.name || ''}" in OVERVIEW


def test_list_header_is_apps_location_latest_run_state():
    html = (WEB / "index.html").read_text()
    head = html[html.index('id="workflow-select-all"'):]
    head = head[:head.index("</thead>")]
    assert "<th>Apps</th>" in head and "Location</th>" in head and "<th>Flow</th>" not in head
    assert 'id="workflow-location-head" hidden' in head


def test_app_strip_follows_the_flow_and_skips_plumbing():
    ctx = _names_ctx({"dropbox": "Dropbox", "renderer": "Renderer", "google-sheets": "Google Sheets",
                      "renderer:render.finished": "Render finished", "date_time": "Date / time",
                      "code": "Code (Python)", "dropbox_upload": "Dropbox upload",
                      "filter": "Filter", "dataops": "DataOps intake"})
    workflow = {"id": "invoice-body-completion", "name": "Render finished invoice → Upload to Dropbox, DataOps",
                "trigger": {"connector": "renderer", "event": "render.finished"},
                "actions": [{"type": "date_time"}, {"type": "code"}, {"type": "dropbox_upload"},
                            {"type": "filter"}, {"type": "dataops"}]}
    ctx.eval(f"var wf = {__import__('json').dumps(workflow)};")
    apps = ctx.eval("JSON.stringify(workflowApps(wf).map((a) => a.key))")
    assert apps == '["renderer","dropbox","dataops"]'
    flow = ctx.eval("workflowFlowText(wf)")
    assert flow == "Render finished → Date / time → Code (Python) → Dropbox upload → Filter → DataOps intake"
    # The hover adds the steps the name leaves out; it is not the name again.
    assert flow != workflow["name"]


def test_app_strip_reads_nested_branches_and_collapses_the_middle():
    ctx = _names_ctx()
    ctx.eval("""var wf = {trigger: {connector: 'telegram', event: 'message.received'},
      actions: [{type: 'condition', then: [{type: 'slack'}]},
                {type: 'paths', paths: [{actions: [{type: 'sheets_append_row'}]}]},
                {type: 'dropbox_upload'}, {type: 'agent'}]};""")
    assert ctx.eval("JSON.stringify(workflowApps(wf).map((a) => a.key))") == \
        '["telegram","slack","google-sheets","dropbox","agent"]'
    strip = ctx.eval("appStripHtml(wf, 'flow text')")
    assert strip.count('class="app-tile"') == 2 and ">+3<" in strip
    assert 'data-mark="telegram"' in strip and 'data-icon="bot"' in strip
    assert 'title="flow text"' in strip


def test_plumbing_only_flow_still_gets_a_code_tile():
    ctx = _names_ctx()
    ctx.eval("var wf = {trigger: {connector: 'webhook', event: 'request.received'}, actions: [{type: 'code'}]};")
    assert ctx.eval("JSON.stringify(workflowApps(wf).map((a) => a.key))") == '["webhook","code"]'
    assert 'data-icon="code-2"' in ctx.eval("appStripHtml(wf)")


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
    # The header carries no id line under the name.
    assert "workflow-id" not in js
    app = (ROOT / "designer/src/App.tsx").read_text()
    assert 'data?.type !== "designer:set-name"' in app
    assert 'nameSource: customName ? "custom" : "auto"' in app
    # A never-saved canvas lets the API assign the id from the name.
    assert "workflow.id === NEW_WORKFLOW_ID" in app
    bundle = (WEB / "designer.js").read_text()
    assert "designer:set-name" in bundle
