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


def test_tab_reentry_refetch_only_fires_for_navigation_links():
    # `[data-view=…]` also matches the view <section>, so any click inside
    # the page (opening a row menu) used to refetch and re-render the list.
    for name in ("hooks.js", "emails.js", "polls.js", "schedules.js", "storage.js"):
        source = (VIEWS / name).read_text()
        for selector in re.findall(r"closest\('([^']*data-view[^']*)'\)", source):
            for part in selector.split(","):
                assert part.strip().startswith("a["), (name, selector)
