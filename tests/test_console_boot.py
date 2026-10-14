"""The operator console must parse and leave the loading screen.

A template in emails.js opened with a backtick and closed with a quote.
The browser then reported `Uncaught SyntaxError: Unexpected identifier
'$'` and stayed on "Loading console…". `node --check` of every module is
the cheap pin; loading the Lambda-served HTML in Chromium is the one
that the site actually boots.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import pytest

from src.dapier.api.router import _static

WEB = Path(__file__).resolve().parents[1] / "src" / "web"
CLASSIC_SCRIPTS = {"theme-init.js", "device.js"}


def test_console_javascript_modules_parse():
    """Parse every ES module the browser will import, plus the classic
    scripts the shell loads before the module graph."""
    if not shutil.which("node"):
        pytest.skip("node is needed to parse console JavaScript")
    modules = [WEB / "app.js"] + sorted(
        path for path in (WEB / "js").rglob("*.js") if path.name not in CLASSIC_SCRIPTS
    )
    failed = []
    for path in modules:
        proc = subprocess.run(
            ["node", "--input-type=module", "--check"],
            input=path.read_text(),
            text=True,
            capture_output=True,
        )
        if proc.returncode != 0:
            failed.append(f"{path.relative_to(WEB.parents[1])}: {proc.stderr.strip()}")
    for path in (WEB / "js" / "theme-init.js", WEB / "vendor" / "dakit-dialogs.js", WEB / "vendor" / "dakit-tabs.js"):
        proc = subprocess.run(
            ["node", "--check"],
            input=path.read_text(),
            text=True,
            capture_output=True,
        )
        if proc.returncode != 0:
            failed.append(f"{path.relative_to(WEB.parents[1])}: {proc.stderr.strip()}")
    assert not failed, "\n".join(failed)


def _api_payload(path):
    if path == "/api/admin/me":
        return {"operator": True, "username": "tester@example.com", "name": "Tester"}
    if path == "/api/admin/workers":
        return {"workers": []}
    if path.startswith("/api/admin/agent-tasks"):
        return {"tasks": []}
    if path.startswith("/api/admin/triggers/inbox"):
        return {"events": [], "next": None}
    if path.startswith("/api/admin/email-from"):
        return {"default": {"mode": "allowlist", "addresses": []}, "routes": {}, "addresses": []}
    if path.startswith("/api/admin/designer/catalog"):
        return {"connectors": [], "actions": []}
    if path.startswith("/api/admin/errors"):
        return {"workflows": [], "total_failed_runs": 0}
    if path.startswith("/api/admin/overview"):
        return {}
    return {}


@contextmanager
def console_server():
    """Serve the Lambda static map plus stub operator APIs on localhost."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return

        def _send(self, status, body, headers):
            payload = body if isinstance(body, (bytes, bytearray)) else body.encode("utf-8")
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path.startswith("/api/"):
                body = json.dumps(_api_payload(parsed.path)).encode()
                self._send(200, body, {"content-type": "application/json"})
                return
            response = _static(parsed.path)
            if response is None:
                self._send(404, b"not found", {"content-type": "text/plain"})
                return
            raw = response["body"]
            if response.get("isBase64Encoded"):
                raw = base64.b64decode(raw)
            elif isinstance(raw, str):
                raw = raw.encode("utf-8")
            self._send(response["statusCode"], raw, response.get("headers") or {})

        def do_POST(self):
            length = int(self.headers.get("content-length") or 0)
            if length:
                self.rfile.read(length)
            parsed = urlparse(self.path)
            if parsed.path.startswith("/api/"):
                body = json.dumps(_api_payload(parsed.path)).encode()
                self._send(200, body, {"content-type": "application/json"})
                return
            self._send(404, b"not found", {"content-type": "text/plain"})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _chromium(playwright):
    try:
        return playwright.chromium.launch()
    except Exception as error:
        if os.environ.get("CI"):
            raise
        pytest.skip(
            f"Chromium is not installed ({error}). "
            "Run: uv run --with playwright python -m playwright install chromium"
        )


def test_console_leaves_the_loading_screen():
    """Load Home and Emails the way a browser does: the Lambda static
    files, then the module graph. A parse error in emails.js used to
    freeze the shell on Loading; this fails if that happens again."""
    pytest.importorskip("playwright.sync_api")
    from playwright.sync_api import sync_playwright

    page_errors = []
    console_errors = []
    with console_server() as origin, sync_playwright() as playwright:
        browser = _chromium(playwright)
        page = browser.new_page()
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
        try:
            page.goto(origin + "/", wait_until="domcontentloaded")
            page.locator("#app").wait_for(state="visible", timeout=10_000)
            assert page.locator("#startup-loading").is_hidden()
            assert page.locator("#startup-error").is_hidden()
            assert page.locator("#forbidden-view").is_hidden()
            assert page.locator("#view-title").inner_text() == "Home"

            page.goto(origin + "/emails", wait_until="domcontentloaded")
            page.locator("#app").wait_for(state="visible", timeout=10_000)
            assert page.locator("#view-title").inner_text() == "Emails"
            assert page.locator("#email-mail-list").count() == 1
        finally:
            browser.close()

    syntax = [message for message in page_errors if "SyntaxError" in message or "Unexpected identifier" in message]
    assert not syntax, "console JavaScript failed to parse:\n" + "\n".join(syntax)
    assert not page_errors, "console raised page errors:\n" + "\n".join(page_errors)
