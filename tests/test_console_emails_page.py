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
    filters, then the addresses and allowed senders as compact lists."""
    html = (WEB / "index.html").read_text()
    page = html[html.index('data-page="emails"'):html.index('data-page="agents"')]
    for marker in ('id="email-mail-list"', 'id="email-filter-chips"',
                   'id="email-mail-more"', 'id="email-addresses"', 'id="email-from-list"'):
        assert marker in page
    # The outcome chips are the trigger family's (views/activity.js), rendered
    # by emails.js: All, Handled, Failed, Refused, No workflow.
    js = (WEB / "js" / "views" / "emails.js").read_text()
    for chip in ("['all', 'All']", "['handled', 'Handled']", "['failed', 'Failed']",
                 "['refused', 'Refused']", "['none', 'No workflow']"):
        assert chip in js
    assert page.index('id="email-mail-list"') < page.index('id="email-addresses"') \
        < page.index('id="email-from-list"')
    # The old three tables (addresses, broad subscriptions, watchers) are one list now.
    assert "email-subscription-table" not in html and "email-watcher-table" not in html
    assert 'id="email-dialog"' in html and 'id="email-allow-sender"' in html


def test_emails_view_uses_the_shared_inbox_and_sender_apis():
    js = (WEB / "js" / "views" / "emails.js").read_text()
    assert "/api/admin/triggers/inbox?" in js and "connector: 'email'" in js
    assert "/api/admin/triggers/inbox/${" in js  # detail read
    assert "openReplayConfirm(" in js  # replay goes through the inbox replay API
    assert "/api/admin/email-from" in js  # allow this sender
    for outcome in ("'handled'", "'failed'", "'refused'", "'unmatched'"):
        assert outcome in js
