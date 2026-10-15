"""The /device page in a real browser (dakit docs/device-login.md §4):
expired-on-arrival grants, and recovery after a failed lookup or decision.
A failed decision is never re-sent behind the person's back: recovery
returns to Review for a fresh, explicit choice."""
import json
import time

import pytest

from tests.test_console_boot import _chromium, console_server


class FakeDeviceApi:
    def __init__(self):
        self.pending = []    # queued (status, body, headers) answers
        self.approve = []
        self.calls = {"pending": 0, "approve": 0}

    def grant(self, expires_in=300):
        now = int(time.time())
        return {"label": "grace@build-box", "request_ip": "203.0.113.7", "same_network": True,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now - 60)),
                "expires_at": now + expires_in, "server_now": now}

    def handle(self, route):
        path = route.request.url.split("?", 1)[0]
        if path.endswith("/api/admin/me"):
            return route.fulfill(status=200, content_type="application/json",
                                 body=json.dumps({"operator": True, "email": "grace@example.com"}))
        for name in ("pending", "approve"):
            if path.endswith(f"/api/agent/device/{name}"):
                self.calls[name] += 1
                queue = getattr(self, name)
                status, body, headers = queue.pop(0) if len(queue) > 1 else queue[0]
                return route.fulfill(status=status, content_type="application/json",
                                     headers=headers, body=json.dumps(body))
        return route.continue_()


@pytest.fixture
def device_page():
    pytest.importorskip("playwright.sync_api")
    from playwright.sync_api import sync_playwright

    with console_server() as origin, sync_playwright() as playwright:
        browser = _chromium(playwright)
        api = FakeDeviceApi()
        page = browser.new_context(bypass_csp=True).new_page()
        page.errors = []
        page.on("pageerror", lambda exc: page.errors.append(str(exc)))
        page.route("**/api/**", api.handle)

        def open_review():
            page.goto(origin + "/device", wait_until="domcontentloaded")
            page.locator("#device-code").fill("BCDF-GHJK")
            page.locator("#device-code").press("Enter")

        try:
            yield page, api, open_review
        finally:
            browser.close()


def _title(page):
    return page.locator(".dk-auth-card__title").inner_text()


def test_a_grant_expired_on_arrival_shows_expired_and_runs_no_timer(device_page):
    page, api, open_review = device_page
    api.pending = [(200, api.grant(expires_in=-5), {})]
    open_review()
    page.wait_for_function("document.querySelector('.dk-auth-card__title').textContent === 'Code expired'")
    page.wait_for_timeout(1500)
    assert _title(page) == "Code expired"
    assert page.errors == []
    assert page.locator("#approve").count() == 0


@pytest.mark.parametrize("decision", ["approve", "deny"])
@pytest.mark.parametrize("failure", [(503, {"error": "Service unavailable"}, {}),
                                     (429, {"error": "Too many"}, {"retry-after": "1"})])
def test_a_failed_decision_returns_to_review_without_resending(device_page, decision, failure):
    page, api, open_review = device_page
    api.pending = [(200, api.grant(), {})]
    api.approve = [failure, (200, {"status": "approved" if decision == "approve" else "denied"}, {})]
    open_review()
    page.locator(f"#{decision}").wait_for()
    page.locator(f"#{decision}").click()
    page.wait_for_function("['Couldn’t reach Dapier', 'Slow down'].includes(document.querySelector('.dk-auth-card__title').textContent)")
    recover = page.locator("#recover")
    if failure[0] == 429:
        page.wait_for_function("!document.querySelector('#recover').disabled")
    assert recover.inner_text().startswith("Back to review")
    recover.click()
    page.wait_for_function("document.querySelector('.dk-auth-card__title').textContent === 'Authorize this device?'")
    assert api.calls["approve"] == 1        # nothing was re-sent
    assert api.calls["pending"] == 1        # Review restored from what was shown
    assert page.locator("#approve").is_enabled() and page.locator("#deny").is_enabled()
    # The renewed, explicit choice goes through.
    page.locator(f"#{decision}").click()
    expected = "Device authorized" if decision == "approve" else "Request denied"
    page.wait_for_function(f"document.querySelector('.dk-auth-card__title').textContent.includes({expected!r})")
    assert api.calls["approve"] == 2
    assert page.errors == []


def test_a_failed_lookup_try_again_repeats_the_lookup(device_page):
    page, api, open_review = device_page
    api.pending = [(502, {"error": "Bad gateway"}, {}), (200, api.grant(), {})]
    open_review()
    page.wait_for_function("document.querySelector('.dk-auth-card__title').textContent === 'Couldn’t reach Dapier'")
    assert page.locator("#recover").inner_text() == "Try again"
    page.locator("#recover").click()
    page.wait_for_function("document.querySelector('.dk-auth-card__title').textContent === 'Authorize this device?'")
    assert api.calls["pending"] == 2
    assert page.errors == []
