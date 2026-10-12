"""Row "…" menus survive list refreshes: a click inside a menu, a tab's
re-entry handler, or a background poll must not re-render the rows (and so
close the open popover) before the operator picks Edit, Delete, or Send test."""
import re
from pathlib import Path

from py_mini_racer import MiniRacer

WEB = Path(__file__).resolve().parents[1] / "src" / "web"
UI_JS = (WEB / "js" / "ui.js").read_text()
VIEWS = WEB / "js" / "views"


def _when_menus_closed():
    start = UI_JS.index("export function whenMenusClosed")
    end = UI_JS.index("\n}\n", start) + 3
    return UI_JS[start:end].replace("export ", "")


def test_refresh_waits_for_the_open_menu_then_paints_once():
    ctx = MiniRacer()
    ctx.eval(_when_menus_closed() + """
    let painted = 0, listener = null;
    const menu = { addEventListener: (type, fn, opts) => { listener = fn; } };
    const root = { querySelector: (sel) => sel === '.row-menu:popover-open' ? menu : null };
    whenMenusClosed(root, () => { painted += 1; });
    """)
    assert ctx.eval("painted") == 0  # menu open: the rows (and the menu) stay put
    ctx.eval("listener({ newState: 'closed' })")  # operator picked Edit / closed it
    assert ctx.eval("painted") == 1


def test_refresh_paints_immediately_without_an_open_menu():
    ctx = MiniRacer()
    ctx.eval(_when_menus_closed() + """
    let painted = 0;
    whenMenusClosed({ querySelector: () => null }, () => { painted += 1; });
    """)
    assert ctx.eval("painted") == 1


def test_hooks_endpoints_render_through_the_guard_and_keep_edit_in_the_menu():
    hooks = (VIEWS / "hooks.js").read_text()
    assert "whenMenusClosed($('[data-workflow-panel=\"hooks\"]'), renderNow)" in hooks
    row = hooks[hooks.index("function endpointRow"):]
    menu = row[row.index('class="row-menu"'):]
    assert "hook-edit" in menu and "hook-delete" in menu and "hook-test" in menu


def test_workflow_list_uses_the_shared_row_menu():
    overview = (VIEWS / "overview.js").read_text()
    assert "whenMenusClosed($('.view[data-page=\"workflows\"]'), paintWorkflows)" in overview
    assert "rowActions(" in overview
    assert 'class="workflow-menu"' not in overview
    assert "row-menu-sep" in overview and "row-menu-danger" in overview
    css = (WEB / "app.css").read_text()
    match = re.search(r"\.row-menu \{[^}]+\}", css)
    assert match, ".row-menu rule missing"
    block = match.group(0)
    assert "overflow-x: hidden" in block
    assert "overflow-y: auto" in block
    assert "min-width:" in block
    assert re.search(r"\.workflow-menu \{", css) is None
    assert ".row-menu-sep" in css


def test_workflow_select_column_fits_the_checkbox():
    """The checkbox column is the 16px box plus the table's outer inset,
    not a leftover 44px touch slot. Left padding matches the footer band."""
    css = (WEB / "app.css").read_text()
    select = re.search(
        r"#workflow-table-wrap th\.select-col, #workflow-table-wrap td\.select-col \{[^}]+\}",
        css,
    )
    assert select, "select-col padding rule missing"
    assert "padding-left: var(--dk-space-4)" in select.group(0)
    assert "padding-right: 0" in select.group(0)
    assert ".workflow-col-select { width: calc(var(--dk-space-4) + 16px); }" in css
    assert ".workflow-check { width: 16px; height: 16px; }" in css
    assert ".workflow-col-select { width: 44px; }" not in css


def test_tab_reentry_refetch_only_fires_for_navigation_links():
    # `[data-view=…]` also matches the view <section>, so any click inside
    # the page (opening a row menu) used to refetch and re-render the list.
    for name in ("hooks.js", "emails.js", "polls.js", "schedules.js"):
        source = (VIEWS / name).read_text()
        for selector in re.findall(r"closest\('([^']*data-view[^']*)'\)", source):
            for part in selector.split(","):
                assert part.strip().startswith("a["), (name, selector)
