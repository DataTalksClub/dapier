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
            return {"workflows": [WORKFLOW]}
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
