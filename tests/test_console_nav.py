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
        "Workers",
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
    for href in ("/emails", "/schedules", "/credentials", "/workers"):
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
    assert _family_labels("agents") == ["Tasks", "Workers"]


def test_page_tabs_clip_vertical_overflow():
    """overflow-x: auto computes overflow-y to auto unless y is set, and
    the 2px tab underline then paints a thin vertical scrollbar."""
    css = pathlib.Path("src/web/app.css").read_text()
    match = re.search(r"\.page-tabs \{[^}]+\}", css)
    assert match, ".page-tabs rule missing"
    block = match.group(0)
    assert "overflow-x: auto" in block
    assert "overflow-y: hidden" in block


def test_parent_nav_highlights_folded_views():
    assert "emails: 'workflows'" in ROUTER
    assert "schedules: 'workflows'" in ROUTER
    assert "credentials: 'connections'" in ROUTER
    assert "workers: 'agents'" in ROUTER


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
