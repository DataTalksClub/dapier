"""Provider Test buttons show an inference reply and failures in the console."""
from __future__ import annotations

import json
import re

import pytest

from tests.test_console_boot import _chromium, console_server
import tests.test_console_boot as console_boot


@pytest.mark.parametrize("provider", ["openrouter", "llm"])
@pytest.mark.parametrize("width", [1280, 390])
def test_llm_test_buttons_run_inference_and_show_results(monkeypatch, provider, width):
    pytest.importorskip("playwright.sync_api")
    from playwright.sync_api import expect, sync_playwright

    original_payload = console_boot._api_payload
    connection = {
        "connection_id": "anthropic-oidc", "provider": "llm", "status": "connected",
        "scopes": [], "llm_config": {
            "auth_type": "anthropic_oidc", "model": "claude-haiku-4-5",
        },
    }
    credentials = [{"provider": "openrouter", "configured": True}]

    def payload(path):
        if path.startswith("/api/admin/overview"):
            return {"connections": [connection], "credentials": credentials}
        if path == "/api/admin/connections":
            return {"connections": [connection]}
        return original_payload(path)

    monkeypatch.setattr(console_boot, "_api_payload", payload)
    with console_server() as origin, sync_playwright() as playwright:
        browser = _chromium(playwright)
        page = browser.new_page(viewport={"width": width, "height": 900})
        requests = []
        routes = []
        connection_id = "openrouter" if provider == "openrouter" else "anthropic-oidc"
        endpoint = f"**/api/admin/connections/{connection_id}/test"
        page.route(endpoint, lambda route: (requests.append(route.request.method), routes.append(route)))
        try:
            page.goto(origin + ("/credentials" if provider == "openrouter" else "/connections"))
            page.locator("#app").wait_for(state="visible")
            button = page.locator(".credential-test" if provider == "openrouter" else ".connection-test")
            expect(button).to_be_visible()
            button.click()
            result = page.locator(".credential-test-result" if provider == "openrouter" else "#edit-connection-test-result")
            pending_button = button if provider == "openrouter" else page.locator("#edit-connection-test")
            expect(pending_button).to_be_disabled()
            expect(result).to_have_text("Sending a short prompt…")
            assert requests == ["POST"]
            routes.pop().fulfill(status=200, content_type="application/json", body=json.dumps({
                "ok": True, "detail": "Inference completed", "model": "test-model",
                "reply": "working <script>unsafe</script>",
            }))
            expect(result).to_contain_text("OK — Inference completed")
            expect(result).to_contain_text("Model: test-model")
            expect(result).to_contain_text("Reply: working <script>unsafe</script>")
            if provider == "openrouter":
                panel = page.locator('[aria-labelledby="service-credentials-title"]').bounding_box()
                name = page.locator('#credential-list .cell-name').bounding_box()
                controls = page.locator('#credential-list .row-actions').bounding_box()
                assert name["x"] >= panel["x"]
                assert controls["x"] + controls["width"] <= panel["x"] + panel["width"]
                if width < 560:
                    reply_box = result.bounding_box()
                    assert controls["y"] >= reply_box["y"] + reply_box["height"]
            assert result.locator("script").count() == 0
            expect(pending_button).to_be_enabled()
            pending_button.click()
            expect(pending_button).to_be_disabled()
            routes.pop().fulfill(status=200, content_type="application/json", body=json.dumps({
                "ok": False, "detail": "Authentication rejected",
            }))
            expect(result).to_have_text("Failed — Authentication rejected")
            expect(result).to_have_class(re.compile(".*form-error.*"))
            expect(pending_button).to_be_enabled()
            pending_button.click()
            expect(pending_button).to_be_disabled()
            routes.pop().fulfill(status=502, content_type="application/json", body=json.dumps({
                "error": "Provider unavailable",
            }))
            expect(result).to_contain_text("Provider unavailable")
            expect(pending_button).to_be_enabled()
        finally:
            browser.close()
