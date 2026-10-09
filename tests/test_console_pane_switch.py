"""Phone trigger tabs: activity and its sources are two panes behind a
switch at the top, so Endpoints / Polls / Addresses / Schedules are one tap
away instead of under the whole activity feed."""
import re
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "src" / "web"
HTML = (WEB / "index.html").read_text()
CSS = (WEB / "app.css").read_text()


def test_each_trigger_workspace_has_a_switch_naming_both_panes():
    switches = re.findall(r'<div class="pane-switch"[^>]*data-workspace="([^"]+)">(.*?)</div>', HTML)
    assert {ws for ws, _ in switches} == {"hooks-layout", "poll-workspace", "email-workspace", "sched-workspace"}
    for workspace, body in switches:
        assert f'id="{workspace}" class="trigger-workspace" data-pane="activity"' in HTML
        assert 'data-pane="activity" aria-pressed="true"' in body
        assert 'data-pane="sources" aria-pressed="false"' in body


def test_switch_only_hides_panes_on_narrow_screens():
    assert ".pane-switch { display: none; }" in CSS
    narrow = CSS[CSS.index("@media (max-width: 860px)", CSS.index(".pane-switch { display: none; }")):]
    assert '.trigger-workspace[data-pane="activity"] > .trigger-sources' in narrow
