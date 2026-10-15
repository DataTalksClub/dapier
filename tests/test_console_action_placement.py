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


@pytest.fixture
def console(monkeypatch):
    pytest.importorskip("playwright.sync_api")
    from playwright.sync_api import sync_playwright

    original = console_boot._api_payload

    def payload(path):
        if path.startswith("/api/admin/overview"):
            return {"workflows": [WORKFLOW], "runs": []}
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
