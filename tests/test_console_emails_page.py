"""The Emails page lists email triggers; it has no create button of its own.

Addresses come from workflow email triggers, so a "New email" button that
opened a blank workflow was a confusing detour. The page links to
Workflows instead.
"""
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "src" / "web"


def test_emails_page_has_no_new_email_button():
    assert 'id="new-email"' not in (WEB / "index.html").read_text()
    assert "#new-email" not in (WEB / "js" / "views" / "emails.js").read_text()


def test_emails_page_is_a_mailbox_of_received_mail():
    """The page lists received email from the inbox API with outcome
    filters, then the addresses and a per-address sender policy panel."""
    html = (WEB / "index.html").read_text()
    page = html[html.index('data-page="emails"'):html.index('data-page="agents"')]
    for marker in ('id="email-mail-list"', 'id="email-filter-chips"',
                   'id="email-mail-more"', 'id="email-addresses"', 'id="email-from-list"',
                   'id="email-from-mode"', 'id="email-from-anyone"', 'id="email-from-title"',
                   'id="email-read"'):
        assert marker in page
    assert "Who can write" in page
    assert 'data-from-mode="anyone"' in page and 'data-from-mode="allowlist"' in page
    assert "for every address" not in page
    # The outcome chips are the trigger family's (views/activity.js), rendered
    # by emails.js: All, Handled, Failed, Refused, No workflow.
    js = (WEB / "js" / "views" / "emails.js").read_text()
    for chip in ("['all', 'All']", "['handled', 'Handled']", "['failed', 'Failed']",
                 "['refused', 'Refused']", "['none', 'No workflow']"):
        assert chip in js
    assert page.index('id="email-mail-list"') < page.index('id="email-read"') \
        < page.index('id="email-addresses"') < page.index('id="email-from-list"')
    # The old three tables (addresses, broad subscriptions, watchers) are one list now.
    assert "email-subscription-table" not in html and "email-watcher-table" not in html
    assert 'id="email-dialog"' not in html
    assert "email-allow-sender" in js and "email-replay" in js
    assert "email-body-text" in js and "htmlToText" in js
    assert "bodyHtml(" in js


def test_emails_reading_pane_renders_html_sandboxed():
    """HTML bodies render like a mail client: a sandboxed iframe whose
    srcdoc is assigned as a property after innerHTML (never interpolated
    into the page string), scripts blocked, links forced to a new tab,
    plain text kept as the collapsed fallback."""
    js = (WEB / "js" / "views" / "emails.js").read_text()
    assert "email-body-frame" in js
    assert "allow-same-origin" in js
    assert "EMAIL_FRAME_SANDBOX" in js
    assert "'allow-scripts'" not in js and '"allow-scripts"' not in js
    assert "frame.srcdoc" in js or ".srcdoc =" in js
    assert "wireEmailFrame(" in js
    assert 'target", "_blank"' in js or "target', '_blank'" in js
    assert "prepareSrcdoc(" in js
    # The old escaped-source dump is gone: HTML renders, it is not printed.
    assert "Original HTML" not in js
    assert "email-body-html" not in js
    css = (WEB / "app.css").read_text()
    assert ".email-body-frame" in css
    # Section anchors parallel tasks rely on stay stable.
    assert "email-body-heading" in js
    assert ".email-body" in css


def test_emails_reading_pane_matches_the_agent_runs_layout():
    """List on the left, selected message (body + which workflow ran) on
    the right; phone shows one pane at a time with Back. Received /
    Addresses are family tabs in the shared strip (the Workflows
    pattern), not an in-content switch."""
    html = (WEB / "index.html").read_text()
    page = html[html.index('data-page="emails"'):html.index('data-page="agents"')]
    assert "pane-switch" not in page
    assert 'data-family="emails"' in html
    assert 'data-email-panel="received"' in page
    assert 'data-email-panel="addresses"' in page
    assert 'data-email-tools="received"' in page
    router = (WEB / "js" / "router.js").read_text()
    assert "EMAIL_TABS" in router and "applyEmailTab" in router
    assert 'href="/emails?tab=addresses"' in html
    # Received is a dakit list–detail split (docs/list-detail.md).
    assert '<div class="dk-split email-split" data-dk-split="emails-received" data-email-panel="received" data-view="list">' in page
    assert 'class="dk-split__back email-back" href="/emails" aria-label="Back to Received"' in page
    assert 'id="email-read-empty" class="dk-split__empty"' in page
    css = (WEB / "app.css").read_text()
    for gone in (".email-mail-workspace", ".email-mail-sidebar", ".activity-row.is-selected",
                 'body[data-view="emails"] main', ".email-back", ".email-subject-row"):
        assert gone not in css, gone
    assert ".email-split { --dk-split-list: 384px; }" in css
    js = (WEB / "js" / "views" / "emails.js").read_text()
    assert "email-outcome-heading" in js
    assert "email-body-heading" in js
    assert "searchParams.set('mail'" in js
    # Rows are links; a pick pushes history; Back clears the selection.
    assert 'class="dk-split__item email-item" href="/emails?mail=' in js
    assert "history.pushState(null, '', url)" in js
    assert "dakitSplit?.show(split(), 'detail')" in js
    # split.js owns row keyboard; the page-level Enter/Space handler is gone.
    assert "event.key !== 'Enter' && event.key !== ' '" not in js


def test_emails_view_uses_the_shared_inbox_and_sender_apis():
    js = (WEB / "js" / "views" / "emails.js").read_text()
    assert "/api/admin/triggers/inbox?" in js and "connector: 'email'" in js
    assert "/api/admin/triggers/inbox/${" in js  # detail read
    assert "openReplayConfirm(" in js  # replay goes through the inbox replay API
    assert "/api/admin/email-from" in js  # allow this sender
    assert "data-email-select" in js
    assert "email.route" in js
    assert "data-from-mode" in js
    assert "selectAddress(" in js
    for outcome in ("'handled'", "'failed'", "'refused'", "'unmatched'"):
        assert outcome in js


def test_phone_back_returns_to_the_row_and_list_position():
    import pytest
    pytest.importorskip("playwright.sync_api")
    from playwright.sync_api import sync_playwright
    import tests.test_console_boot as boot

    events = [{"inbox_id": f"email#{n}", "received_at": "2026-10-15T08:00:00Z", "outcome": "handled",
               "runs": [], "email": {"from": f"Sender {n} <s{n}@example.com>", "from_address": f"s{n}@example.com",
                                     "to": ["inbox@example.com"], "subject": f"Subject {n}", "text": "Body"}}
              for n in range(30)]
    real = boot._api_payload

    def payload(path):
        if path == "/api/admin/triggers/inbox":
            return {"events": events, "paging": {}}
        if path.startswith("/api/admin/triggers/inbox/"):
            key = path.rsplit("/", 1)[1].replace("%23", "#")
            return {"event": next(e for e in events if e["inbox_id"] == key)}
        return real(path)

    boot._api_payload = payload
    try:
        with boot.console_server() as origin, sync_playwright() as playwright:
            browser = boot._chromium(playwright)
            page = browser.new_context(viewport={"width": 390, "height": 844}, has_touch=True,
                                       is_mobile=True, bypass_csp=True).new_page()
            page.goto(origin + "/emails", wait_until="domcontentloaded")
            row = page.locator('a[data-inbox="email#20"]')
            row.wait_for(timeout=10_000)
            assert page.locator(".email-split").get_attribute("data-view") == "list"
            row.scroll_into_view_if_needed()
            before = page.evaluate("document.querySelector('#email-mail-scroll').scrollTop")
            assert before > 0
            row.click()
            page.wait_for_function("document.querySelector('.email-split').dataset.view === 'detail'")
            assert "mail=email%2320" in page.url
            assert page.evaluate("document.activeElement.id") == "email-read-title"
            assert page.locator("#email-read-title").inner_text() == "Subject 20"
            page.locator(".email-back").click()
            page.wait_for_function("document.querySelector('.email-split').dataset.view === 'list'")
            assert "mail=" not in page.url
            assert page.evaluate("document.activeElement.dataset.inbox") == "email#20"
            assert abs(page.evaluate("document.querySelector('#email-mail-scroll').scrollTop") - before) < 2
            browser.close()
    finally:
        boot._api_payload = real


@pytest.mark.parametrize("width,height,mobile", [(1440, 900, False), (390, 844, True)])
def test_deep_links_beyond_the_first_page_resolve_through_the_detail_endpoint(width, height, mobile):
    """Review M5: ?mail= for a message the first page does not hold opens
    that message (detail API), keeps the URL, and survives Back/Forward."""
    pytest.importorskip("playwright.sync_api")
    from playwright.sync_api import sync_playwright
    import tests.test_console_boot as boot

    events = [{"inbox_id": f"email#{n}", "received_at": "2026-10-15T08:00:00Z", "outcome": "handled",
               "runs": [], "email": {"from": f"Sender {n} <s{n}@example.com>", "from_address": f"s{n}@example.com",
                                     "to": ["inbox@example.com"], "subject": f"Subject {n}", "text": "Body"}}
              for n in range(30)]
    real = boot._api_payload
    requested = []

    def payload(path):
        if path == "/api/admin/triggers/inbox":
            return {"events": events[:25], "paging": {"next": "page-2"}}
        if path.startswith("/api/admin/triggers/inbox/"):
            key = path.rsplit("/", 1)[1].replace("%23", "#")
            requested.append(key)
            return {"event": next(e for e in events if e["inbox_id"] == key)}
        return real(path)

    boot._api_payload = payload
    try:
        with boot.console_server() as origin, sync_playwright() as playwright:
            browser = boot._chromium(playwright)
            page = browser.new_context(viewport={"width": width, "height": height}, has_touch=mobile,
                                       is_mobile=mobile, bypass_csp=True).new_page()
            page.goto(origin + "/emails?mail=email%2329", wait_until="domcontentloaded")
            page.wait_for_function("document.querySelector('#email-read-title')?.textContent === 'Subject 29'")
            assert "email#29" in requested
            assert "mail=email%2329" in page.url
            assert page.locator(".email-split").get_attribute("data-view") == "detail"
            # Pick a listed message, then go Back to the deep-linked one.
            if mobile:
                page.locator(".email-back").click()
                page.wait_for_function("document.querySelector('.email-split').dataset.view === 'list'")
            page.locator('a[data-inbox="email#3"]').click()
            page.wait_for_function("document.querySelector('#email-read-title')?.textContent === 'Subject 3'")
            page.go_back()
            if mobile:
                page.wait_for_function("document.querySelector('.email-split').dataset.view === 'list'")
                page.go_back()
            page.wait_for_function("document.querySelector('#email-read-title')?.textContent === 'Subject 29'")
            assert "mail=email%2329" in page.url
            page.go_forward()
            if mobile:
                page.go_forward()
            page.wait_for_function("document.querySelector('#email-read-title')?.textContent === 'Subject 3'")
            browser.close()
    finally:
        boot._api_payload = real
