"""Console Agents page: a dakit list–detail split (docs/list-detail.md).

The run list and the selected run fill the content area and scroll on their
own; the selection lives in the URL (?task=, pushState for a pick); phones
show one pane at a time and Back returns to the row it came from. The
selected run reads like an Emails message: the title in the pane head, ONE
meta line (status · workflow · when · duration), exact times, engine and ids
in a Gmail-style Details popover on the workflow name."""

import pathlib
import re

import pytest

import tests.test_console_boot as boot

WEB = pathlib.Path("src/web")
JS = (WEB / "js/views/agents.js").read_text()
CSS = (WEB / "app.css").read_text()
INDEX = (WEB / "index.html").read_text()


def _rule(selector, css=CSS):
    match = re.search(re.escape(selector) + r"(?:\s*,\s*[^{,]+)*\s*\{([^}]*)\}", css)
    assert match, selector
    return match.group(1)


def _view():
    return INDEX[INDEX.index('data-page="agents"'):INDEX.index('data-page="workers"')]


def test_agents_is_a_dakit_split_with_url_selection():
    view = _view()
    assert 'class="view dk-fill"' in INDEX[INDEX.index('data-page="agents"') - 40:INDEX.index('data-page="agents"')]
    assert '<div class="dk-split agent-split" data-dk-split="agent-runs" data-view="list">' in view
    assert 'class="dk-split__list" aria-labelledby="agent-runs-title"' in view
    assert 'class="dk-split__detail" aria-label="Selected agent run"' in view
    # The run search belongs to the list pane; the status chips stay above.
    assert view.index('id="agent-runs-search"') > view.index('class="dk-split__list"')
    assert 'class="dk-split__back agent-back" href="/agents" aria-label="Back to Runs"' in view
    assert 'class="dk-split__empty"' in view and "Select a run" in view
    assert '<script src="/assets/vendor/split.js"></script>' in INDEX
    # Rows are links to their own URL; the selection is aria-current, not a class.
    assert 'class="dk-split__item agent-run-item" href="/agents?task=' in JS
    assert "aria-current=\"true\"" in JS and "'selected'" not in JS
    assert "history.pushState(null, '', url)" in JS
    assert "dakitSplit?.show(split(), 'detail')" in JS


def test_detail_head_is_title_meta_line_with_gmail_details():
    head = JS[JS.index('<div class="agent-detail-head">'):JS.index('<section class="agent-result"')]
    assert head.count('<p class="agent-detail-meta">') == 1
    meta = head[head.index('agent-detail-meta'):head.index('agent-details-pop')]
    assert "${badge(task)}" in meta
    assert 'href="/workflows/${encodeURIComponent(task.workflow' in meta
    assert "duration(task)" in meta
    assert 'class="agent-details-toggle"' in meta
    assert 'popovertarget="agent-task-details"' in meta
    assert 'id="agent-task-details" class="agent-details-pop" popover' in head
    for label in ("Finished", "Duration", "Engine", "Task ID", "Required capabilities",
                  "Workspace", "Exit code", "Completion email sent"):
        assert f"<dt>{label}</dt>" in head, label
    ui = (WEB / "js/ui.js").read_text()
    assert ".row-menu, .agent-details-pop" in ui
    pop = _rule(".agent-details-pop")
    assert "position: fixed" in pop
    assert "var(--dk-shadow-overlay)" in pop
    assert "$('#agent-run-title').textContent" in JS
    assert 'class="agent-log-section"' in JS and "data-agent-stream" in JS


def test_old_two_pane_css_is_gone():
    for gone in (".agent-workspace", ".agent-run-sidebar", ".agent-run-list", ".agent-back",
                 'body[data-view="agents"] main', ".agent-run-item.selected"):
        assert gone not in CSS, gone
    title = _rule(".agent-run-title")
    assert "text-overflow: ellipsis" in title and "white-space: nowrap" in title


def test_toolbar_status_cannot_overlap_the_chips():
    assert re.search(r"\.agent-toolbar \{[^}]*display: grid; grid-template-columns: minmax\(0, 1fr\) auto", CSS)
    strip = _rule(".page-tools .agent-workers-strip, .agent-workers-strip")
    assert "min-width: 0" in strip and "white-space: normal" in strip


# --- in a browser ------------------------------------------------------------

TASKS = [
    {"task_id": f"agent-{900 - n}", "workflow": "email-trigger-agents",
     "status": "succeeded" if n % 3 else "failed", "created_at": 1760500000 - n * 600,
     "started_at": 1760500000 - n * 600, "finished_at": 1760500060 - n * 600,
     "email_subject": f"Task number {n}", "summary": "Line\n" * 80}
    for n in range(40)
]


def _payload(path):
    if path.startswith("/api/admin/agent-tasks"):
        return {"tasks": TASKS}
    return boot._api_payload(path)


@pytest.fixture
def browser(monkeypatch):
    pytest.importorskip("playwright.sync_api")
    from playwright.sync_api import sync_playwright

    real = boot._api_payload
    monkeypatch.setattr(boot, "_api_payload", lambda path: _payload(path) if path.startswith(
        "/api/admin/agent-tasks") else real(path))
    with sync_playwright() as playwright:
        chromium = boot._chromium(playwright)
        try:
            yield chromium
        finally:
            chromium.close()


def _open(browser, origin, path, **context):
    # bypass_csp: wait_for_function polls string predicates through eval,
    # which the console CSP (script-src 'self') forbids.
    page = browser.new_context(bypass_csp=True, **context).new_page()
    page.goto(origin + path, wait_until="domcontentloaded")
    page.locator("#app").wait_for(state="visible", timeout=10_000)
    page.locator("#agent-runs-list a[data-agent-task]").first.wait_for(timeout=10_000)
    return page


def test_desktop_panes_scroll_on_their_own_and_picks_push_history(browser):
    with boot.console_server() as origin:
        page = _open(browser, origin, "/agents", viewport={"width": 1280, "height": 720})
        # The desktop auto-pick replaces the URL; a person's pick adds history.
        page.wait_for_function("new URLSearchParams(location.search).get('task') === 'agent-900'")
        depth = page.evaluate("history.length")
        split = page.locator(".agent-split")
        box = split.bounding_box()
        assert box["y"] + box["height"] <= 720
        # Page never scrolls; each pane does.
        assert page.evaluate("document.querySelector('#main-content').scrollHeight <= document.querySelector('#main-content').clientHeight + 1")
        assert page.evaluate("(() => { const s = document.querySelector('#agent-runs-scroll'); return s.scrollHeight > s.clientHeight; })()")
        page.locator('a[data-agent-task="agent-890"]').click()
        page.wait_for_function("location.search === '?task=agent-890'")
        assert page.evaluate("history.length") == depth + 1
        selected = page.locator('#agent-runs-list [aria-current="true"]')
        assert selected.count() == 1 and selected.get_attribute("data-agent-task") == "agent-890"
        page.go_back()
        page.wait_for_function("location.search === '?task=agent-900'")
        page.wait_for_function("document.querySelector('#agent-runs-list [aria-current=\"true\"]')?.dataset.agentTask === 'agent-900'")
        # Back control is phone-only.
        assert page.locator(".agent-back").is_hidden()


def test_phone_shows_one_pane_and_back_keeps_list_position_and_focus(browser):
    with boot.console_server() as origin:
        page = _open(browser, origin, "/agents", viewport={"width": 390, "height": 844},
                     has_touch=True, is_mobile=True)
        split = page.locator(".agent-split")
        assert split.get_attribute("data-view") == "list"
        assert "task=" not in page.url  # no auto-pick on a phone
        scroller = page.locator("#agent-runs-scroll")
        page.evaluate("document.querySelector('#agent-runs-scroll').scrollTop = 600")
        row = page.locator('a[data-agent-task="agent-880"]')
        row.scroll_into_view_if_needed()
        before = page.evaluate("document.querySelector('#agent-runs-scroll').scrollTop")
        row.click()
        page.wait_for_function("document.querySelector('.agent-split').dataset.view === 'detail'")
        assert page.locator(".dk-split__list").first.is_hidden()
        assert page.evaluate("document.activeElement.id") == "agent-run-title"
        back = page.locator(".agent-back")
        assert back.is_visible() and back.get_attribute("aria-label") == "Back to Runs"
        back.click()
        page.wait_for_function("document.querySelector('.agent-split').dataset.view === 'list'")
        assert "task=" not in page.url
        assert page.evaluate("document.activeElement.dataset.agentTask") == "agent-880"
        assert abs(scroller.evaluate("el => el.scrollTop") - before) < 2
