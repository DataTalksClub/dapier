"""Console dialogs that fetch load before they open (dakit
docs/dialog-loading.md): the trigger goes busy, a slow load shows the
dakit loading overlay, the dialog opens only once its content is rendered,
and a failed load opens nothing and offers Retry in a toast."""
from __future__ import annotations

import json
import os

import pytest

from tests.test_console_boot import _chromium, console_server
import tests.test_console_boot as console_boot

SHOTS = os.environ.get("DAPIER_DIALOG_SHOTS")  # directory for manual screenshots

RUN = {
    "run": {
        "run_id": "run-1", "workflow_id": "wf-one", "status": "succeeded",
        "started_at": "2026-10-14T10:00:00Z", "duration_ms": 1200,
    },
    "steps": [],
}
WORKFLOW = {"id": "wf-one", "name": "wf-one", "source": "wf-one.yaml", "enabled": True,
            "trigger": {"connector": "manual", "event": "run"}, "actions": []}
POLL = {"poll_id": "feed", "name": "feed", "source": "http", "url": "https://example.com/feed",
        "health": "ok", "interval_seconds": 300, "enabled": True}
ITEM = {"inbox_id": "in-1", "poll": "feed", "title": "Item one", "item_id": "item-1",
        "received_at": "2026-10-14T10:00:00Z", "status": "processed"}
CONNECTION = {"connection_id": "slack-main", "provider": "slack", "status": "connected", "scopes": []}


def _shot(page, name):
    if SHOTS:
        page.screenshot(path=os.path.join(SHOTS, f"dapier-dialog-{name}.png"))


def _held(page, pattern):
    """Hold matching requests until the test fulfils them."""
    held = []
    page.route(pattern, lambda route: held.append(route))
    return held


def _wait_for(page, held, count=1):
    for _ in range(100):
        if len(held) >= count:
            return held[count - 1]
        page.wait_for_timeout(20)
    raise AssertionError("request was never made")


def _is_open(page, selector):
    return page.evaluate(f"() => document.querySelector({json.dumps(selector)}).open")


@pytest.fixture
def browser_page(monkeypatch):
    pytest.importorskip("playwright.sync_api")
    from playwright.sync_api import sync_playwright

    original = console_boot._api_payload

    def payload(path):
        if path.startswith("/api/admin/overview"):
            return {"workflows": [WORKFLOW], "connections": [CONNECTION]}
        if path == "/api/admin/poll-triggers/activity":
            return {"items": [ITEM]}
        if path == "/api/admin/poll-triggers":
            return {"polls": [POLL]}
        if path == "/api/admin/hook-triggers":
            return {"hooks": [{"hook_id": "intake", "kind": "webhook", "url": "https://example.com/hook/intake"}]}
        if path == "/api/admin/hook-triggers/deliveries":
            return {"deliveries": [{"delivery_id": "d-1", "hook": "intake", "kind": "webhook",
                                    "received_at": "2026-10-14T10:00:00Z", "response_status": 200}]}
        if path == "/api/admin/triggers/inbox":
            return {"events": [{"inbox_id": "ev-1", "connector": "slack", "event": "message",
                                "received_at": "2026-10-14T10:00:00Z", "matched": []}]}
        if path == "/api/admin/connections":
            return {"connections": [CONNECTION]}
        return original(path)

    monkeypatch.setattr(console_boot, "_api_payload", payload)
    scheme = os.environ.get("DAPIER_DIALOG_SCHEME", "light")
    errors = []
    with console_server() as origin, sync_playwright() as playwright:
        browser = _chromium(playwright)
        page = browser.new_page(viewport={"width": 1280, "height": 860}, color_scheme=scheme)
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        try:
            yield page, origin, scheme
        finally:
            browser.close()
    assert not errors, "console raised page errors:\n" + "\n".join(errors)


def test_run_deep_link_shows_the_overlay_then_the_finished_dialog(browser_page):
    from playwright.sync_api import expect

    page, origin, scheme = browser_page
    held = _held(page, "**/api/admin/runs/run-1")
    page.goto(origin + "/runs?run=run-1")
    page.locator("#app").wait_for(state="visible")
    route = _wait_for(page, held)
    overlay = page.locator("dialog.dk-loading")
    expect(overlay).to_be_visible()
    expect(overlay).to_contain_text("Loading run…")
    assert not _is_open(page, "#run-dialog"), "the run dialog opened before its data"
    _shot(page, f"run-overlay-{scheme}")
    route.fulfill(status=200, content_type="application/json", body=json.dumps(RUN))
    expect(page.locator("#run-dialog")).to_be_visible()
    expect(overlay).to_be_hidden()
    expect(page.locator("#run-title")).to_have_text("wf-one")
    expect(page.locator("#run-detail")).not_to_contain_text("Loading")
    _shot(page, f"run-open-{scheme}")


def test_failed_run_load_opens_nothing_and_retries_from_a_toast(browser_page):
    from playwright.sync_api import expect

    page, origin, _ = browser_page
    held = _held(page, "**/api/admin/runs/run-1")
    page.goto(origin + "/runs?run=run-1")
    page.locator("#app").wait_for(state="visible")
    _wait_for(page, held).fulfill(status=500, content_type="application/json",
                                  body=json.dumps({"error": "run store unavailable"}))
    toast = page.locator(".dk-toast--danger")
    expect(toast).to_be_visible()
    expect(toast).to_contain_text("run store unavailable")
    assert not _is_open(page, "#run-dialog")
    toast.get_by_role("button", name="Retry").click()
    _wait_for(page, held, 2).fulfill(status=200, content_type="application/json", body=json.dumps(RUN))
    expect(page.locator("#run-dialog")).to_be_visible()
    expect(toast).to_have_count(0)


def test_versions_from_the_row_menu_busy_the_more_button_until_loaded(browser_page):
    from playwright.sync_api import expect

    page, origin, scheme = browser_page
    held = _held(page, "**/api/admin/designer/workflows/wf-one.yaml/versions")
    page.goto(origin + "/workflows")
    page.locator("#app").wait_for(state="visible")
    more = page.locator(".workflow-list-row .row-more").first
    more.click()
    page.locator(".row-menu:popover-open .workflow-versions").click()
    route = _wait_for(page, held)
    expect(more).to_have_attribute("aria-busy", "true")
    assert not _is_open(page, "#versions-dialog")
    route.fulfill(status=200, content_type="application/json", body=json.dumps({"versions": [
        {"revision": 2, "current": True, "published_at": "2026-10-14T10:00:00Z", "cause": "save", "enabled": True},
        {"revision": 1, "published_at": "2026-10-13T10:00:00Z", "cause": "save", "enabled": True},
    ]}))
    dialog = page.locator("#versions-dialog")
    expect(dialog).to_be_visible()
    expect(dialog.locator("tbody tr")).to_have_count(2)
    expect(dialog).not_to_contain_text("Loading")
    expect(more).not_to_have_attribute("aria-busy", "true")
    _shot(page, f"versions-open-{scheme}")


def test_explore_data_keeps_manage_open_until_the_catalog_is_in(browser_page):
    from playwright.sync_api import expect

    page, origin, scheme = browser_page
    held = _held(page, "**/api/admin/connections/slack-main/discover")
    page.goto(origin + "/connections")
    page.locator("#app").wait_for(state="visible")
    page.locator(".connection-edit").first.click()
    explore = page.locator("#edit-connection-discover")
    explore.click()
    route = _wait_for(page, held)
    expect(explore).to_have_attribute("aria-busy", "true")
    expect(page.locator("#edit-connection-dialog")).to_be_visible()
    assert not _is_open(page, "#connection-discover-dialog")
    page.wait_for_timeout(250)  # past --dk-loading-delay-busy: the spinner shows
    _shot(page, f"explore-busy-{scheme}")
    route.fulfill(status=200, content_type="application/json", body=json.dumps({"resources": [
        {"name": "channels", "label": "Channels", "description": "Public channels", "params": []},
    ]}))
    dialog = page.locator("#connection-discover-dialog")
    expect(dialog).to_be_visible()
    expect(page.locator("#edit-connection-dialog")).to_be_hidden()
    expect(page.locator("#discover-resource option")).to_have_text(["Channels"])
    expect(page.locator("#discover-run")).to_be_enabled()
    _shot(page, f"explore-open-{scheme}")


def _open_poll(page, origin):
    page.goto(origin + "/workflows?tab=polls")
    page.locator("#app").wait_for(state="visible")
    page.locator(".poll-open").first.click()
    from playwright.sync_api import expect
    expect(page.locator("#poll-detail-dialog")).to_be_visible()


def test_poll_sample_opens_with_the_sample_in_it(browser_page):
    from playwright.sync_api import expect

    page, origin, scheme = browser_page
    held = _held(page, "**/api/admin/discover")
    _open_poll(page, origin)
    sample = page.locator('#poll-detail-dialog [data-poll-action="sample"]')
    sample.click()
    route = _wait_for(page, held)
    expect(sample).to_have_attribute("aria-busy", "true")
    assert not _is_open(page, "#poll-sample-dialog")
    route.fulfill(status=200, content_type="application/json", body=json.dumps({
        "connector": "poll", "source": "live", "sample": {"event": "feed", "data": {"title": "Item one"}},
    }))
    expect(page.locator("#poll-sample-dialog")).to_be_visible()
    expect(page.locator("#poll-sample-meta")).to_have_text("poll · feed · live")
    expect(page.locator("#poll-sample-data")).to_contain_text('"title": "Item one"')
    _shot(page, f"poll-sample-{scheme}")


def test_poll_item_row_is_busy_until_its_event_is_in(browser_page):
    from playwright.sync_api import expect

    page, origin, _ = browser_page
    held = _held(page, "**/api/admin/triggers/inbox/in-1")
    _open_poll(page, origin)
    row = page.locator('#poll-detail-dialog .activity-row[data-inbox="in-1"]')
    row.click()
    route = _wait_for(page, held)
    expect(row).to_have_attribute("aria-busy", "true")
    assert not _is_open(page, "#poll-item-dialog")
    route.fulfill(status=200, content_type="application/json", body=json.dumps({
        "event": {"inbox_id": "in-1", "data": {"poll": "feed", "title": "Item one"}},
    }))
    expect(page.locator("#poll-item-dialog")).to_be_visible()
    expect(page.locator("#poll-item-title")).to_have_text("Item one")


@pytest.mark.parametrize("view, reveal, row_selector, pattern, body, dialog", [
    ("/workflows?tab=hooks", None, '.activity-row[data-delivery="d-1"]', "**/api/admin/hook-triggers/deliveries/d-1",
     {"delivery": {"delivery_id": "d-1", "hook": "intake", "response_status": 200}}, "#hook-delivery-dialog"),
    ("/runs", "#runs-events-section > summary", '.inbox-open[data-inbox="ev-1"]', "**/api/admin/triggers/inbox/ev-1",
     {"event": {"inbox_id": "ev-1", "connector": "slack", "event": "message"}}, "#inbox-event-dialog"),
])
def test_rows_that_open_a_fetched_dialog_go_busy_first(browser_page, view, reveal, row_selector, pattern, body, dialog):
    from playwright.sync_api import expect

    page, origin, _ = browser_page
    held = _held(page, pattern)
    page.goto(origin + view)
    page.locator("#app").wait_for(state="visible")
    if reveal:
        page.locator(reveal).click()
    row = page.locator(row_selector)
    row.click()
    route = _wait_for(page, held)
    expect(row).to_have_attribute("aria-busy", "true")
    expect(page.locator("dialog.dk-loading")).to_be_visible()  # rows get the overlay at the busy delay
    assert not _is_open(page, dialog)
    route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
    expect(page.locator(dialog)).to_be_visible()
    expect(page.locator("dialog.dk-loading")).to_be_hidden()
    expect(row).not_to_have_attribute("aria-busy", "true")
