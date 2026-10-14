"""Phone trigger tabs: activity and its sources are two panes behind a
switch at the top, so Endpoints / Polls / Addresses / Schedules are one tap
away instead of under the whole activity feed."""
import re
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "src" / "web"
HTML = (WEB / "index.html").read_text()
CSS = (WEB / "app.css").read_text()


def test_each_trigger_workspace_has_a_switch_naming_both_panes():
    switches = re.findall(r'<div class="dk-tabs pane-switch" role="tablist" aria-label="[^"]+" data-workspace="([^"]+)">(.*?)</div>', HTML)
    assert {ws for ws, _ in switches} == {"hooks-layout", "poll-workspace", "sched-panes"}
    for workspace, body in switches:
        assert f'id="{workspace}" class="trigger-workspace" data-pane="activity"' in HTML
        assert 'role="tab" data-pane="activity"' in body and 'aria-selected="true" tabindex="0"' in body
        assert 'role="tab" data-pane="sources"' in body and 'aria-selected="false" tabindex="-1"' in body
        for pane in re.findall(r'aria-controls="([^"]+)"', body):
            assert f'id="{pane}"' in HTML


def test_emails_uses_family_tabs_not_a_pane_switch():
    """Received / Addresses live in the shared #page-tabs strip (the
    Workflows pattern), not in an in-content pane switch. The router swaps
    the two panels under the strip."""
    page = HTML[HTML.index('data-page="emails"'):HTML.index('data-page="agents"')]
    assert "pane-switch" not in page
    assert 'data-family="emails"' in HTML
    assert 'data-email-panel="received"' in page
    assert 'data-email-panel="addresses"' in page
    router = (WEB / "js" / "router.js").read_text()
    assert "emails: 'emails'" in router
    assert "applyEmailTab" in router


def test_switch_only_hides_panes_on_narrow_screens():
    assert ".pane-switch { display: none; }" in CSS
    narrow = CSS[CSS.index("@media (max-width: 860px)", CSS.index(".pane-switch { display: none; }")):]
    assert '.trigger-workspace[data-pane="activity"] > .trigger-sources' in narrow
