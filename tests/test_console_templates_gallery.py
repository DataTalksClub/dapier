"""The console's Templates gallery and Duplicate surface stay wired to the
template/duplicate API the CLI drives.

Pins, per surface: the nav item + gallery view + fork dialog in index.html;
the endpoints templates.js calls (gallery GET, apply POST, unpublish PUT);
the Workflows list's Duplicate and Publish/Unpublish-template buttons and
the endpoints they call; router registration (client VIEWS, server
CONSOLE_VIEWS + the per-module asset map); the deep-link fetch in main.js;
and the overview payload's ``template`` flag the Workflows row buttons read.

The gap that closed: templates and duplicate were reachable only from the
designer app and the CLI — the console had no gallery and no Duplicate
button, and the overview rows did not carry the flag, so a published
template was invisible (and unpublishable) on the main surface.
"""
import re
from pathlib import Path

from src.dapier.api import overview as overview_api

WEB = Path(__file__).resolve().parents[1] / "src" / "web"
REPO = Path(__file__).resolve().parents[1]


def _read(*parts):
    return (Path(*parts)).read_text()


def test_gallery_nav_view_and_fork_dialog_exist():
    html = _read(WEB, "index.html")
    assert re.search(r'href="/templates" data-view="templates"', html), \
        "the Templates nav item is missing"
    assert '<section class="view" data-page="templates">' in html, \
        "the Templates view section is missing"
    assert 'id="template-table"' in html, "the gallery table is missing"
    # The fork dialog mirrors `dapier templates apply`: the new name is
    # optional and defaults to <template-id>-copy server-side.
    assert 'id="template-apply-dialog"' in html
    assert 'id="template-apply-form"' in html
    assert 'id="template-apply-name"' in html


def test_gallery_calls_the_templates_api():
    js = _read(WEB, "js", "views", "templates.js")
    assert "api('/api/admin/designer/templates')" in js, \
        "the gallery must load through GET /api/admin/designer/templates"
    assert "designer/templates/${encodeURIComponent(file)}/apply" in js, \
        "Use template must fork through POST .../templates/<file>/apply"
    assert "{ template: false }" in js, \
        "Unpublish must clear the flag through PUT .../workflows/<file>/template"


def test_workflows_list_duplicate_and_template_buttons():
    js = _read(WEB, "js", "views", "overview.js")
    assert 'class="button secondary workflow-duplicate"' in js, \
        "each sourced workflow row needs a Duplicate button"
    assert "designer/workflows/${encodeURIComponent(button.dataset.file)}/duplicate" in js, \
        "Duplicate must call POST .../workflows/<file>/duplicate"
    assert 'class="button secondary workflow-template"' in js, \
        "each sourced workflow row needs a Publish/Unpublish template button"
    assert "designer/workflows/${encodeURIComponent(button.dataset.file)}/template" in js, \
        "the template button must call PUT .../workflows/<file>/template"
    # Row buttons own their clicks: openRowFor must not also open the row.
    assert ".workflow-duplicate, .workflow-template" in js


def test_router_and_asset_maps_register_the_view():
    router_js = _read(WEB, "js", "router.js")
    assert re.search(r"VIEWS = \[[^\]]*['\"]templates['\"]", router_js), \
        "the client router must know the templates view"
    router_py = _read(REPO, "src", "dapier", "api", "router.py")
    assert re.search(r'CONSOLE_VIEWS = \([^\)]*"/templates"', router_py), \
        "/templates must serve the console shell"
    assert '"/assets/js/views/templates.js"' in router_py, \
        "the view module must be in the asset map or its import 404s"


def test_deep_link_fetches_the_gallery():
    main_js = _read(WEB, "js", "main.js")
    assert "fetchTemplates" in main_js, \
        "main.js must import and call fetchTemplates for the initial view"


def test_overview_row_carries_the_template_flag():
    workflow = {
        "id": "flow",
        "enabled": True,
        "trigger": {"connector": "email", "event": "message.received"},
        "actions": [{"id": "a1", "type": "webhook", "url": "https://example.test/hook"}],
    }
    view = overview_api._workflow_view(workflow, "flow.yaml", published=True)
    assert view["template"] is False, "unflagged workflows are not templates"
    flagged = overview_api._workflow_view(
        {**workflow, "template": True}, "flow.yaml", published=True)
    assert flagged["template"] is True, "flagged workflows must ride the overview row"
    # Defensive, matching designer_store._summary: only a bare True counts.
    loose = overview_api._workflow_view(
        {**workflow, "template": "yes"}, "flow.yaml", published=True)
    assert loose["template"] is False
