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
    match = re.search(r'<div id="page-tabs".*?</nav>\s*</div>', INDEX, re.S)
    assert match, "page-tabs missing"
    return match.group(0)


def test_sidebar_lists_daily_pages_then_settings():
    sidebar = _sidebar()
    labels = re.findall(r"<span>([^<]+)</span>", sidebar)
    assert labels == [
        "Home",
        "Workflows",
        "Emails",
        "Runs",
        "Agents",
        "Workers",
        "Connections",
        "Access",
        "Costs",
        "Audit",
    ]
    assert "Settings" in sidebar
    for leftover in (
        "Monitor",
        "Automate",
        "Administration",
        "Schedules",
        "Credentials",
        "API tokens",
        "Data store",
        "Audit log",
    ):
        assert leftover not in sidebar


def _family_labels(family: str) -> list[str]:
    match = re.search(
        rf'<nav class="dk-tabs" data-family="{family}" aria-label="[^"]+">(.*?)</nav>',
        _page_tabs(),
        re.S,
    )
    assert match, f"family {family} missing"
    return re.findall(r">([^<]+)</a>", match.group(1))


def test_folded_pages_are_family_tabs_not_sidebar_items():
    sidebar = _sidebar()
    tabs = _page_tabs()
    for href in ("/schedules", "/credentials"):
        assert f'class="nav-item" href="{href}"' not in sidebar
        assert href in tabs
    assert 'href="/workflows?tab=hooks"' in tabs
    assert 'href="/workflows?tab=polls"' in tabs
    assert _family_labels("workflows") == [
        "Workflows",
        "Schedules",
        "Hooks",
        "Polls",
    ]
    assert _family_labels("connections") == ["Accounts", "App setup"]
    assert _family_labels("emails") == ["Received", "Addresses"]
    assert 'data-family="agents"' not in tabs


def test_emails_is_its_own_sidebar_page():
    """Emails is a sidebar page with its own family strip: Received /
    Addresses tabs under one header, like Workflows / Hooks / Polls."""
    sidebar = _sidebar()
    assert 'class="nav-item" href="/emails" data-view="emails"><i data-lucide="mail"></i><span>Emails</span>' in sidebar
    assert sidebar.index('href="/workflows"') < sidebar.index('href="/emails"') < sidebar.index('href="/runs"')
    tabs = _page_tabs()
    assert 'href="/emails"' in tabs
    assert 'href="/emails?tab=addresses"' in tabs
    assert _family_labels("emails") == ["Received", "Addresses"]
    assert "emails: 'emails'" in ROUTER
    assert "emails: ['Emails', 'Mail Dapier received, and which workflow ran.']" in ROUTER
    assert "/emails" in ingress.CONSOLE_VIEWS


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


def test_page_tabs_are_dakit_route_tabs():
    """Each family strip is a dakit segmented control of route links: a
    named <nav class="dk-tabs"> whose current link carries aria-current
    (set by the router). The look is dakit's; app CSS doesn't restyle it."""
    tabs = _page_tabs()
    navs = re.findall(r'<nav class="dk-tabs" data-family="[^"]+" aria-label="([^"]+)">(.*?)</nav>', tabs, re.S)
    assert len(navs) == 3
    for _label, body in navs:
        links = re.findall(r"<a [^>]*>", body)
        assert links and all('class="dk-tab view-link"' in link for link in links)
    assert "aria-current" in ROUTER
    css = pathlib.Path("src/web/app.css").read_text()
    assert ".page-tabs a" not in css
    assert ".page-tab-set" not in css


def test_family_tabs_keep_the_parent_header():
    """Switching Accounts / App setup (or Workflows / Schedules) swaps the body under the tab strip, not the title or
    primary action above it."""
    assert "FAMILY_META" in ROUTER
    assert "connections: ['Connections'" in ROUTER
    assert "workflows: ['Workflows'" in ROUTER
    assert "credentials: ['App setup'" not in ROUTER
    assert "if (TAB_FAMILY[view?.dataset.page]) return;" in ROUTER
    html = INDEX
    # Adding lives on each service group now, not in a page-level header button.
    assert 'id="add-connection"' not in html
    assert 'id="pull-trigger-sample"' not in html
    assert 'id="trigger-sample-dialog"' not in html
    assert 'class="page-tools primary-tools"' not in html.split('data-page="connections"')[1].split('data-page="credentials"')[0]


def test_parent_nav_highlights_folded_views():
    assert "schedules: 'workflows'" in ROUTER
    assert "credentials: 'connections'" in ROUTER


def test_workflows_toolbar_keeps_search_without_status_tag_folder_filters():
    """The list is searched; On/Off, tags, and folders are not extra
    dropdowns on the toolbar."""
    html = INDEX.split('data-page="workflows"', 1)[1].split('data-workflow-panel="list"', 1)[0]
    assert 'id="workflow-search"' in html
    assert 'id="workflow-filter"' not in html
    assert 'id="workflow-tag-filter"' not in html
    assert 'id="workflow-folder-filter"' not in html
    js = pathlib.Path("src/web/js/views/overview.js").read_text()
    assert "$('#workflow-tag-filter')" not in js
    assert "$('#workflow-folder-filter')" not in js
    assert "$('#workflow-filter')" not in js


def test_old_triggers_path_opens_hooks_tab():
    assert "if (name === 'triggers') return 'workflows'" in ROUTER
    assert "'/triggers'" in ROUTER
    assert "/workflows?tab=hooks" in ROUTER
    assert "/triggers" in ingress.CONSOLE_VIEWS


def test_old_data_page_redirects_to_workflows():
    """The Data page left the console: a workflow's stored keys live in its
    designer's Stored data panel. Saved /storage links still land."""
    assert 'href="/storage"' not in INDEX
    assert 'data-page="storage"' not in INDEX
    assert not pathlib.Path("src/web/js/views/storage.js").exists()
    assert "'storage'" not in ROUTER.split("const VIEWS = ", 1)[1].split("\n", 1)[0]
    assert "if (name === 'storage') return 'workflows'" in ROUTER
    assert "window.location.pathname === '/storage') history.replaceState(null, '', '/workflows')" in ROUTER
    assert "/storage" in ingress.CONSOLE_VIEWS
    assert "Path: /storage" in TEMPLATE
    assert "/assets/js/views/storage.js" not in pathlib.Path("src/dapier/api/router.py").read_text()


def test_sidebar_and_page_tab_hrefs_have_gateway_routes():
    hrefs = {
        path.split("?", 1)[0]
        for path in re.findall(r'href="([^"]+)"', _sidebar() + _page_tabs())
    }
    for href in sorted(hrefs):
        assert href in ingress.CONSOLE_VIEWS, href
        assert f"Path: {href}" in TEMPLATE, href
