"""The Emails page lists email triggers; it has no create button of its own.

Addresses come from workflow email triggers, so a "New email" button that
opened a blank workflow was a confusing detour. The page links to
Workflows instead.
"""
from pathlib import Path

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


def test_emails_reading_pane_matches_the_agent_runs_layout():
    """List on the left, selected message (body + which workflow ran) on
    the right; phone shows one pane at a time with Back."""
    css = (WEB / "app.css").read_text()
    assert ".email-mail-workspace { display: grid;" in css
    assert "body[data-view=\"emails\"] .pane-switch { display: grid;" in css
    assert ".email-mail-workspace.has-selection #email-read { display: block; }" in css
    assert ".email-mail-workspace.has-selection .email-mail-sidebar { display: none; }" in css
    assert ".email-mail-sidebar .activity-row { grid-template-columns: minmax(0, 1fr) auto;" in css
    assert ".activity-row .activity-outcome { display: contents; }" in css
    js = (WEB / "js" / "views" / "emails.js").read_text()
    assert "email-outcome-heading" in js
    assert "email-body-heading" in js
    assert "searchParams.set('mail'" in js


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
