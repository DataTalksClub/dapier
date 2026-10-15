"""The console runs in dakit's app shell (docs/shell.md), driven in a real
browser: the account disclosure (keyboard, Esc, focus return), the 56px
icon rail with its tooltips, and the phone drawer (modal, Esc, focus
return, a destination closes it)."""
import pytest

from tests.test_console_boot import _chromium, console_server


@pytest.fixture
def browser():
    pytest.importorskip("playwright.sync_api")
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        chromium = _chromium(playwright)
        try:
            yield chromium
        finally:
            chromium.close()


def _open(browser, origin, path="/", **context):
    # bypass_csp: wait_for_function polls string predicates through eval,
    # which the console CSP (script-src 'self') forbids.
    page = browser.new_context(bypass_csp=True, **context).new_page()
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.goto(origin + path, wait_until="domcontentloaded")
    page.locator("#app").wait_for(state="visible", timeout=10_000)
    page.errors = errors
    return page


def _focused_id(page):
    return page.evaluate("document.activeElement && document.activeElement.id")


def test_account_menu_opens_from_the_keyboard_and_closes_on_escape(browser):
    with console_server() as origin:
        page = _open(browser, origin, viewport={"width": 1280, "height": 800})
        trigger = page.locator("#account-button")
        menu = page.locator("#account-menu")
        assert trigger.get_attribute("aria-label") == "Account, Tester"
        assert menu.is_hidden()

        trigger.focus()
        page.keyboard.press("Enter")
        assert menu.is_visible()
        assert trigger.get_attribute("aria-expanded") == "true"
        # Keyboard opening lands on the first row; arrows move through rows.
        assert _focused_id(page) == "account-theme-toggle"
        page.keyboard.press("ArrowDown")
        assert _focused_id(page) == "account-sign-out"

        page.keyboard.press("Escape")
        assert menu.is_hidden()
        assert trigger.get_attribute("aria-expanded") == "false"
        assert _focused_id(page) == "account-button"

        # The menu flies out beside the sidebar, never over it.
        trigger.click()
        sidebar = page.locator("#main-sidebar").bounding_box()
        box = menu.bounding_box()
        assert box["x"] >= sidebar["x"] + sidebar["width"]
        # An outside click closes it.
        page.mouse.click(900, 400)
        assert menu.is_hidden()
        assert page.errors == []


def test_dark_mode_switch_lives_in_the_account_menu(browser):
    with console_server() as origin:
        page = _open(browser, origin, viewport={"width": 1280, "height": 800}, color_scheme="light")
        page.locator("#account-button").click()
        switch = page.locator("#account-theme-toggle")
        assert switch.get_attribute("aria-checked") == "false"
        switch.click()
        assert page.evaluate("document.documentElement.dataset.theme") == "dark"
        assert switch.get_attribute("aria-checked") == "true"
        assert page.evaluate("localStorage.getItem('dakit-theme')") == "dark"


def test_collapsed_rail_keeps_every_destination_and_shows_tooltips(browser):
    with console_server() as origin:
        page = _open(browser, origin, viewport={"width": 1280, "height": 800})
        page.locator("#sidebar-toggle").click()
        shell = page.locator(".dk-shell")
        assert shell.get_attribute("data-sidebar") == "collapsed"
        assert page.locator("#sidebar-toggle").get_attribute("aria-label") == "Expand sidebar"
        assert round(page.locator("#main-sidebar").bounding_box()["width"]) == 56
        items = page.locator(".dk-sidebar .dk-nav-item")
        assert items.count() == 10
        for index in range(items.count()):
            assert items.nth(index).is_visible()
        # Labels stay in the accessibility tree; the tooltip repeats one.
        assert page.get_by_role("link", name="Emails").count() == 1
        page.locator('.dk-sidebar .dk-nav-item[data-view="emails"]').hover()
        tooltip = page.locator(".dk-tooltip")
        tooltip.wait_for(state="visible")
        assert tooltip.inner_text() == "Emails"
        # The state persists per app.
        assert page.evaluate("localStorage.getItem('dk-sidebar:dapier')") == "collapsed"
        page.reload(wait_until="domcontentloaded")
        assert page.locator(".dk-shell").get_attribute("data-sidebar") == "collapsed"


def test_phone_drawer_is_modal_and_a_destination_closes_it(browser):
    with console_server() as origin:
        page = _open(browser, origin, "/runs", viewport={"width": 390, "height": 844},
                     has_touch=True, is_mobile=True)
        toggle = page.locator("#menu-toggle")
        sidebar = page.locator("#main-sidebar")
        # The account trigger rides in the phone bar, not the drawer.
        assert page.locator(".dk-topbar__end #account-button").count() == 1

        toggle.click()
        page.wait_for_function("document.activeElement && document.activeElement.closest('#main-sidebar')")
        assert sidebar.get_attribute("role") == "dialog"
        assert sidebar.get_attribute("aria-modal") == "true"
        assert toggle.get_attribute("aria-expanded") == "true"
        assert page.evaluate("document.querySelector('.dk-main').inert") is True
        page.keyboard.press("Escape")
        assert toggle.get_attribute("aria-expanded") == "false"
        assert sidebar.get_attribute("role") is None
        assert _focused_id(page) == "menu-toggle"

        toggle.click()
        page.locator('.dk-sidebar .dk-nav-item[data-view="agents"]').click()
        page.wait_for_function("location.pathname === '/agents'")
        assert toggle.get_attribute("aria-expanded") == "false"
        assert page.evaluate("document.querySelector('.dk-main').inert") is False
        assert page.locator("#view-title").inner_text() == "Agents"
        assert page.errors == []


def test_navigation_marks_the_current_page_and_resets_the_scroller(browser):
    with console_server() as origin:
        page = _open(browser, origin, viewport={"width": 1280, "height": 800})
        current = page.locator('.dk-sidebar .dk-nav-item[aria-current="page"]')
        assert current.get_attribute("data-view") == "overview"
        page.evaluate("document.querySelector('#main-content').scrollTop = 200")
        page.locator('.dk-sidebar .dk-nav-item[data-view="audit"]').click()
        page.wait_for_function("location.pathname === '/audit'")
        assert current.get_attribute("data-view") == "audit"
        assert page.evaluate("document.querySelector('#main-content').scrollTop") == 0
        # The page itself never scrolls; .dk-main is the only scroller.
        assert page.evaluate("document.scrollingElement.scrollHeight <= innerHeight")
