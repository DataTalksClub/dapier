"""Console information architecture: sidebar families and page tabs."""

import pathlib
import re

from src.dapier.api import router as ingress


INDEX = pathlib.Path("src/web/index.html").read_text()
ROUTER = pathlib.Path("src/web/js/router.js").read_text()
TEMPLATE = pathlib.Path("template.yaml").read_text()


def _sidebar() -> str:
    match = re.search(r'<nav aria-label="Main navigation">(.*?)</nav>', INDEX, re.S)
    assert match, "sidebar nav missing"
    return match.group(1)


def _page_tabs() -> str:
    match = re.search(r'<nav id="page-tabs".*?</nav>', INDEX, re.S)
    assert match, "page-tabs missing"
    return match.group(0)


def test_sidebar_lists_daily_pages_then_settings():
    sidebar = _sidebar()
    labels = re.findall(r"<span>([^<]+)</span>", sidebar)
    assert labels == [
        "Home",
        "Workflows",
        "Runs",
        "Agents",
        "Workers",
        "Connections",
        "Access",
        "Data",
        "Audit",
    ]
    assert "Settings" in sidebar
    for leftover in (
        "Monitor",
        "Automate",
        "Administration",
        "Emails",
        "Schedules",
        "Credentials",
        "API tokens",
        "Data store",
        "Audit log",
    ):
        assert leftover not in sidebar


def _family_labels(family: str) -> list[str]:
    match = re.search(
        rf'<div class="page-tab-set" data-family="{family}">(.*?)</div>',
        _page_tabs(),
        re.S,
    )
    assert match, f"family {family} missing"
    return re.findall(r">([^<]+)</a>", match.group(1))


def test_folded_pages_are_family_tabs_not_sidebar_items():
    sidebar = _sidebar()
    tabs = _page_tabs()
    for href in ("/emails", "/schedules", "/credentials"):
        assert f'class="nav-item" href="{href}"' not in sidebar
        assert href in tabs
    assert 'href="/workflows?tab=hooks"' in tabs
    assert 'href="/workflows?tab=polls"' in tabs
    assert _family_labels("workflows") == [
        "Workflows",
        "Emails",
        "Schedules",
        "Hooks",
        "Polls",
    ]
    assert _family_labels("connections") == ["Accounts", "App setup"]
    assert 'data-family="agents"' not in tabs


def test_workers_is_its_own_page_next_to_agents():
    """Workers left the Agents tab strip for its own sidebar entry. The
    route stays /workers, so old links to the former tab still land."""
    sidebar = _sidebar()
    assert 'class="nav-item" href="/workers" data-view="workers"><i data-lucide="server"></i><span>Workers</span>' in sidebar
    assert sidebar.index('href="/agents"') < sidebar.index('href="/workers"') < sidebar.index('href="/connections"')
    assert "workers: 'agents'" not in ROUTER
    assert "agents: 'agents'" not in ROUTER
    assert "workers: ['Workers', 'Machines that run agent tasks." in ROUTER
    assert "agents: ['Agents'" in ROUTER
    assert "/workers" in ingress.CONSOLE_VIEWS
    assert "Path: /workers" in TEMPLATE


def test_page_tabs_clip_vertical_overflow():
    """overflow-x: auto computes overflow-y to auto unless y is set, and
    the 2px tab underline then paints a thin vertical scrollbar."""
    css = pathlib.Path("src/web/app.css").read_text()
    match = re.search(r"\.page-tabs \{[^}]+\}", css)
    assert match, ".page-tabs rule missing"
    block = match.group(0)
    assert "overflow-x: auto" in block
    assert "overflow-y: hidden" in block


def test_family_tabs_keep_the_parent_header():
    """Switching Accounts / App setup (or Workflows / Emails) swaps the body under the tab strip, not the title or
    primary action above it."""
    assert "FAMILY_META" in ROUTER
    assert "connections: ['Connections'" in ROUTER
    assert "workflows: ['Workflows'" in ROUTER
    assert "credentials: ['App setup'" not in ROUTER
    assert "emails: ['Emails'" not in ROUTER
    assert "if (TAB_FAMILY[view?.dataset.page]) return;" in ROUTER
    html = INDEX
    # Adding lives on each service group now, not in a page-level header button.
    assert 'id="add-connection"' not in html
    assert 'id="pull-trigger-sample"' not in html
    assert 'id="trigger-sample-dialog"' not in html
    assert 'class="page-tools primary-tools"' not in html.split('data-page="connections"')[1].split('data-page="credentials"')[0]


def test_parent_nav_highlights_folded_views():
    assert "emails: 'workflows'" in ROUTER
    assert "schedules: 'workflows'" in ROUTER
    assert "credentials: 'connections'" in ROUTER


def test_old_triggers_path_opens_hooks_tab():
    assert "if (name === 'triggers') return 'workflows'" in ROUTER
    assert "'/triggers'" in ROUTER
    assert "/workflows?tab=hooks" in ROUTER
    assert "/triggers" in ingress.CONSOLE_VIEWS


def test_sidebar_and_page_tab_hrefs_have_gateway_routes():
    hrefs = {
        path.split("?", 1)[0]
        for path in re.findall(r'href="([^"]+)"', _sidebar() + _page_tabs())
    }
    for href in sorted(hrefs):
        assert href in ingress.CONSOLE_VIEWS, href
        assert f"Path: {href}" in TEMPLATE, href
