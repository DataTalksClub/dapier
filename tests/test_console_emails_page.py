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
