"""dakit "Action placement": a create rides on the collection it creates
into, right after that collection's title (or after the filters that scope
its toolbar), never in the page header's top-right corner. The buttons are
dakit's own (vendor/dakit-collection.js), and `c` presses the visible one."""
from __future__ import annotations

import os

import pytest

from tests.test_console_boot import _chromium, console_server
import tests.test_console_boot as console_boot

WORKFLOW = {"id": "wf-one", "name": "wf-one", "source": "wf-one.yaml", "enabled": True,
            "trigger": {"connector": "manual", "event": "run"}, "actions": []}
TOKEN = {"token_id": "scheduler", "token_prefix": "dap_9f2", "agent": "scheduler",
         "created_at": "2026-09-30T09:00:00Z"}

SCHEDULE = {"schedule_id": "nightly-backup", "expression": "cron(0 3 * * ? *)", "enabled": True}
DATA = {"schedules": [SCHEDULE]}


@pytest.fixture
def console(monkeypatch):
    pytest.importorskip("playwright.sync_api")
    from playwright.sync_api import sync_playwright

    original = console_boot._api_payload

    def payload(path):
        if path == "/api/admin/schedule-triggers":
            return {"schedules": list(DATA["schedules"])}
        if path.startswith("/api/admin/overview"):
            return {"workflows": [WORKFLOW], "runs": [], "api_tokens": [TOKEN]}
        return original(path)

    monkeypatch.setattr(console_boot, "_api_payload", payload)
    errors = []
    with console_server() as origin, sync_playwright() as playwright:
        browser = _chromium(playwright)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("pageerror", lambda exc: errors.append(str(exc)))

        def open_view(path):
            page.goto(origin + path)
            page.locator("#app").wait_for(state="visible")
            return page

        try:
            yield open_view
        finally:
            browser.close()
    assert not errors, "console raised page errors:\n" + "\n".join(errors)


def _lead_children(page, lead):
    return page.evaluate(
        "(lead) => [...document.querySelector(lead).children].map((el) => el.id || el.textContent.trim())",
        lead,
    )


def _assert_dakit_create(button, label):
    from playwright.sync_api import expect

    expect(button).to_be_visible()
    expect(button).to_have_class("dk-button dk-button--primary dk-button--sm dk-collection-create")
    expect(button).to_have_attribute("data-dk-create", "")
    expect(button).to_have_attribute("aria-keyshortcuts", "c")
    expect(button).to_have_accessible_name(label)


def _assert_page_header_has_no_actions(page):
    """The page header holds the title and description only."""
    from playwright.sync_api import expect

    expect(page.locator(".topbar .dk-button")).to_have_count(0)
    expect(page.locator("#topbar-tools")).to_have_count(0)


def test_home_create_follows_your_workflows_title(console):
    from playwright.sync_api import expect

    page = console("/")
    create = page.locator("#home-new-workflow")
    _assert_dakit_create(create, "New workflow")
    assert _lead_children(page, "#home-workflows-lead") == ["home-workflows-title", "home-new-workflow"]
    expect(page.locator('[aria-labelledby="home-workflows-title"] .dk-collection-header__meta')).to_have_text("1 workflow · 1 on")
    _assert_page_header_has_no_actions(page)
    expect(page.get_by_role("button", name="Create workflow")).to_have_count(0)

    page.keyboard.press("c")
    page.wait_for_url("**/workflows/new")


def test_tokens_create_follows_the_api_tokens_title(console):
    from playwright.sync_api import expect

    page = console("/tokens")
    create = page.locator("#new-token")
    _assert_dakit_create(create, "New token")
    assert _lead_children(page, "#tokens-lead") == ["tokens-title", "new-token"]
    expect(page.locator("#token-count")).to_have_text("1 token · 1 active")
    _assert_page_header_has_no_actions(page)

    page.keyboard.press("c")
    expect(page.locator("#token-dialog")).to_be_visible()


def test_workflows_create_follows_the_list_search(console):
    from playwright.sync_api import expect

    page = console("/workflows")
    create = page.locator("#new-workflow")
    _assert_dakit_create(create, "New workflow")
    assert _lead_children(page, "#workflow-toolbar-lead")[1:] == ["new-workflow"]
    expect(page.locator("#workflow-toolbar-lead > .toolbar-filters #workflow-search")).to_have_count(1)
    # Import and export are secondary and trail the toolbar.
    end = page.locator('[data-panel-tools="list"] .dk-toolbar__end')
    expect(end.locator(".dk-button")).to_have_text(["Import YAML", "Export all"])
    expect(end.locator(".dk-button--primary")).to_have_count(0)
    _assert_page_header_has_no_actions(page)

    page.keyboard.press("c")
    page.wait_for_url("**/workflows/new")


def test_schedules_create_follows_the_schedules_register_title(console):
    from playwright.sync_api import expect

    page = console("/schedules")
    create = page.locator("#new-schedule")
    _assert_dakit_create(create, "New schedule")
    assert _lead_children(page, "#sched-register-lead") == ["sched-register-title", "new-schedule"]
    expect(page.locator("#sched-summary")).to_have_text("1 schedule")
    # The fires toolbar keeps only its filters.
    expect(page.locator('[data-page="schedules"] > .dk-toolbar .dk-button')).to_have_count(0)
    _assert_page_header_has_no_actions(page)

    page.keyboard.press("c")
    expect(page.locator("#schedule-dialog")).to_be_visible()


def test_empty_schedules_keep_the_header_create_and_repeat_it(console, monkeypatch):
    from playwright.sync_api import expect

    monkeypatch.setitem(DATA, "schedules", [])
    page = console("/schedules")
    expect(page.locator("#schedule-empty")).to_be_visible()
    _assert_dakit_create(page.locator("#new-schedule"), "New schedule")
    repeat = page.locator("#schedule-empty-create")
    expect(repeat).to_have_class("dk-button dk-button--secondary")
    repeat.click()
    expect(page.locator("#schedule-dialog")).to_be_visible()
