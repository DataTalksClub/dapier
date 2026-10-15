"""View switchers and single-choice filters use dakit's one segmented
control (.dk-tabs, dakit docs/segmented.md): radiogroups for filters,
tablists for in-page views, route <nav>s for routes."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "web"
HTML = (WEB / "index.html").read_text()


def _app_sources():
    for path in [WEB / "index.html", WEB / "app.css", *sorted((WEB / "js").rglob("*.js")),
                 *sorted((ROOT / "designer" / "src").rglob("*.ts*")), ROOT / "designer" / "src" / "styles.css"]:
        yield path, path.read_text()


def test_no_removed_filter_chip_or_app_restyled_segments():
    for path, text in _app_sources():
        assert "dk-filter-chip" not in text, path
        if path.suffix == ".css":
            assert not re.search(r"\.dk-tab\b(?!s)", text), f"{path} restyles .dk-tab"


def test_single_choice_filters_are_radiogroups():
    for track in ("hooks-outcome-chips", "poll-outcome-chips", "sched-outcome-chips",
                  "sched-window-chips", "email-filter-chips"):
        match = re.search(rf'<div id="{track}" class="dk-tabs[^"]*" role="radiogroup" aria-label="[^"]+">', HTML)
        assert match, track
    assert '<div class="dk-tabs agent-filters" role="radiogroup"' in HTML
    assert '<div class="dk-tabs dk-tabs--sm" role="radiogroup" aria-label="Who can write">' in HTML
    activity = (WEB / "js" / "views" / "activity.js").read_text()
    assert 'role="radio"' in activity and "aria-checked" in activity


def test_agent_filter_counts_ride_inside_the_segment():
    agents = HTML[HTML.index('class="dk-tabs agent-filters"'):]
    agents = agents[:agents.index("</div>")]
    assert agents.count('<span class="dk-count">') == 4


def test_tabs_helper_loads_once_per_page():
    assert HTML.count('src="/assets/vendor/tabs.js"') == 1
    app = (ROOT / "designer" / "src" / "App.tsx").read_text()
    assert app.count('vendor/tabs.js"') == 1
