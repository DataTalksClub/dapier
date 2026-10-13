"""Inbox rows for email: envelope summary, outcome words, run links.

The console's Emails page is a mailbox of what Dapier received and what
happened to each message; `dapier emails received` / `dapier inbox list`
print the same columns. Both read these fields from the inbox API.
"""
import json
from email.message import EmailMessage
from unittest.mock import MagicMock

from src.dapier.triggers import inbox
from src.dapier.triggers.intake import email_ingress
from tests.test_inbox import EVENT, seed_event, table  # noqa: F401 (fixture)


EMAIL = {
    "schema_version": "1.0",
    "id": "mail-1",
    "connector": "email",
    "event": "message.received",
    "source": "email",
    "data": {
        "route": "invoice",
        "message_id": "<m1@example.com>",
        "sender": {"addresses": ["ann@example.com"], "header": "Ann <ann@example.com>"},
        "recipients": {"addresses": ["invoice@mail.example"], "cc": "bob@example.com",
                       "to": "invoice@mail.example"},
        "subject": "March invoice",
        "date": "Mon, 05 Oct 2026 12:37:18 +0200",
        "body": {"text": {"value": "secret body text"}},
        "attachments": [{"filename": "inv.pdf", "size": 1234, "content_type": "application/pdf",
                         "s3": {"bucket": "b", "key": "k"}}],
    },
}


def test_email_rows_carry_the_envelope_summary_outcome_and_run_links(table):  # noqa: F811
    """An email row lists with sender, recipients, subject, and attachment
    names — envelope metadata only, never the body — plus the outcome in
    plain words and one run link per matched workflow."""
    inbox.record(EMAIL, table_ref=table)
    inbox.complete("mail-1", ["invoice-intake"], table_ref=table)

    _, payload = inbox.api_list("email", table_ref=table)
    row = payload["events"][0]
    assert row["outcome"] == "handled"
    assert row["runs"] == [{"workflow": "invoice-intake", "run_id": "invoice-intake:mail-1"}]
    assert row["email"] == {
        "from": "Ann <ann@example.com>",
        "from_address": "ann@example.com",
        "to": ["invoice@mail.example"],
        "cc": "bob@example.com",
        "subject": "March invoice",
        "date": "Mon, 05 Oct 2026 12:37:18 +0200",
        "message_id": "<m1@example.com>",
        "route": "invoice",
        "attachments": [{"name": "inv.pdf", "size": 1234, "content_type": "application/pdf"}],
    }
    assert "secret body text" not in json.dumps(row["email"])
    assert "secret body text" not in json.dumps(row.get("data") or {})


def test_email_summary_survives_the_storage_cap(table):  # noqa: F811
    """The summary is taken before the data is clipped, so an oversized
    message still lists with its sender and subject."""
    big = {**EMAIL["data"], "body": {"text": {"value": "x" * (inbox.DATA_LIMIT + 1)}}}
    inbox.record({**EMAIL, "data": big}, table_ref=table)
    assert table.items["mail-1"]["data"]["truncated"] is True
    _, payload = inbox.api_get("mail-1", table_ref=table)
    assert payload["event"]["email"]["subject"] == "March invoice"
    assert payload["event"]["email"]["from_address"] == "ann@example.com"


def test_legacy_truncated_rows_recover_headers_from_the_preview(table):  # noqa: F811
    """Rows stored before the summary existed keep only a clipped JSON
    preview; the header fields at its start still fill the row."""
    preview = json.dumps({"route": "agents",
                          "sender": {"addresses": ["a@x.io"], "header": "A <a@x.io>"},
                          "recipients": {"addresses": ["agents@d.io"], "to": "agents@d.io"},
                          "subject": "Fwd: \"quoted\"", "body": "y" * 300})[:250]
    table.items["old"] = {"inbox_id": "old", "connector": "email", "status": inbox.IGNORED,
                          "received_at": "2026-10-01T00:00:00+00:00", "matched": [],
                          "data": {"truncated": True, "preview": preview}}
    _, payload = inbox.api_get("old", table_ref=table)
    event = payload["event"]
    assert event["outcome"] == "refused"
    assert event["email"]["from_address"] == "a@x.io"
    assert event["email"]["to"] == ["agents@d.io"]
    assert event["email"]["subject"] == 'Fwd: "quoted"'


def test_bounce_rows_summarize_the_feedback(table):  # noqa: F811
    inbox.record({**EMAIL, "id": "fb-1", "event": "bounce.received", "data": {
        "feedback_type": "bounce", "bounce_type": "Permanent", "source": "noreply@d.io",
        "destination": ["gone@x.io"], "bounced_recipients": ["gone@x.io"],
        "message_id": "ses-1"}}, table_ref=table)
    _, payload = inbox.api_get("fb-1", table_ref=table)
    assert payload["event"]["email"] == {
        "feedback": "bounce", "bounce_type": "Permanent", "recipients": ["gone@x.io"],
        "from": "noreply@d.io", "to": ["gone@x.io"], "message_id": "ses-1"}


def test_non_email_rows_have_no_email_summary(table):  # noqa: F811
    inbox.record(EVENT, table_ref=table)
    _, payload = inbox.api_list(table_ref=table)
    assert "email" not in payload["events"][0]
    assert payload["events"][0]["outcome"] == "pending"


def test_api_list_filters_by_outcome(table):  # noqa: F811
    for index, status in enumerate((inbox.MATCHED, inbox.IGNORED, inbox.UNMATCHED,
                                    inbox.FAILED)):
        seed_event(table, f"evt-{index}", f"2026-09-27T10:00:0{index}+00:00",
                   connector="email")
        table.items[f"evt-{index}"]["status"] = status
    _, payload = inbox.api_list("email", outcome="refused", table_ref=table)
    assert [event["inbox_id"] for event in payload["events"]] == ["evt-1"]
    assert payload["paging"]["filtered"] is True
    _, payload = inbox.api_list("email", outcome="handled,failed", table_ref=table)
    assert [event["inbox_id"] for event in payload["events"]] == ["evt-3", "evt-0"]
    status, payload = inbox.api_list(outcome="nope", table_ref=table)
    assert status == 400 and "nope" in payload["error"]


def test_api_get_includes_the_decoded_body(table):  # noqa: F811
    """The reading pane (and `dapier inbox show`) load text/html on the
    single-event read; list rows stay envelope-only."""
    inbox.record(EMAIL, table_ref=table)
    _, payload = inbox.api_get("mail-1", table_ref=table)
    assert payload["event"]["email"]["text"] == "secret body text"
    assert "html" not in payload["event"]["email"]
    _, listed = inbox.api_list("email", table_ref=table)
    assert "text" not in listed["events"][0]["email"]
    assert "html" not in listed["events"][0]["email"]


def test_api_get_reads_ses_top_level_text_and_html(table):  # noqa: F811
    """The SES catch-all stores decoded parts at data.text / data.html."""
    inbox.record({
        **EMAIL, "id": "ses-1",
        "data": {**EMAIL["data"], "text": "plain invoice",
                 "html": "<p>plain invoice</p>", "body": {}},
    }, table_ref=table)
    _, payload = inbox.api_get("ses-1", table_ref=table)
    assert payload["event"]["email"]["text"] == "plain invoice"
    assert payload["event"]["email"]["html"] == "<p>plain invoice</p>"


def test_api_get_unwraps_datamailer_html_part(table):  # noqa: F811
    inbox.record({
        **EMAIL, "id": "dm-1",
        "data": {**EMAIL["data"], "body": {"html": {"value": "<p>Do this</p>"}},
                 "html": {"value": "<p>Do this</p>"}},
    }, table_ref=table)
    _, payload = inbox.api_get("dm-1", table_ref=table)
    assert payload["event"]["email"]["html"] == "<p>Do this</p>"
    assert "text" not in payload["event"]["email"]


def test_truncated_detail_flags_the_clipped_body(table):  # noqa: F811
    big = {**EMAIL["data"], "body": {"text": {"value": "x" * (inbox.DATA_LIMIT + 1)}}}
    inbox.record({**EMAIL, "id": "big-1", "data": big}, table_ref=table)
    _, payload = inbox.api_get("big-1", table_ref=table)
    assert payload["event"]["email"]["truncated"] is True
    assert payload["event"]["email"]["subject"] == "March invoice"


def _raw_message():
    message = EmailMessage()
    message["From"] = "Ann <ann@example.com>"
    message["To"] = "invoice@mail.example"
    message["Subject"] = "March invoice"
    message["Message-ID"] = "<m1@example.com>"
    message.set_content("plain body")
    message.add_alternative("<h1>March invoice</h1><p>Pay me</p>", subtype="html")
    return message.as_bytes()


def _s3_mock(raw):
    s3 = MagicMock()
    s3.get_object.return_value = {"Body": MagicMock(read=lambda: raw)}
    return s3


def test_truncated_detail_rehydrates_the_display_body_from_the_raw_copy(table, monkeypatch):  # noqa: F811
    """The detail read — and only the detail read — rebuilds the display
    body from the raw s3 copy, so the reading pane shows the full HTML
    like a mail client. Replay keeps its own intake-capped path."""
    big = {**EMAIL["data"], "s3": {"bucket": "b", "key": "k"},
           "body": {"text": {"value": "x" * (inbox.DATA_LIMIT + 1)}}}
    inbox.record({**EMAIL, "id": "big-1", "data": big}, table_ref=table)
    assert table.items["big-1"]["data"]["truncated"] is True
    assert table.items["big-1"]["data"]["s3"] == {"bucket": "b", "key": "k"}
    monkeypatch.setattr(email_ingress, "s3", _s3_mock(_raw_message()))

    _, payload = inbox.api_get("big-1", table_ref=table)

    email = payload["event"]["email"]
    assert email.get("truncated") is None
    assert email["rehydrated"] is True
    assert email["html"] == "<h1>March invoice</h1><p>Pay me</p>\n"
    assert email["text"] == "plain body\n"
    _, listed = inbox.api_list("email", table_ref=table)
    assert "html" not in listed["events"][0]["email"]


def test_truncated_detail_falls_back_when_the_raw_copy_is_gone(table, monkeypatch):  # noqa: F811
    big = {**EMAIL["data"], "s3": {"bucket": "b", "key": "k"},
           "body": {"text": {"value": "x" * (inbox.DATA_LIMIT + 1)}}}
    inbox.record({**EMAIL, "id": "big-1", "data": big}, table_ref=table)
    s3 = MagicMock()
    s3.get_object.side_effect = RuntimeError("no such key")
    monkeypatch.setattr(email_ingress, "s3", s3)

    _, payload = inbox.api_get("big-1", table_ref=table)

    assert payload["event"]["email"]["truncated"] is True
    assert "rehydrated" not in payload["event"]["email"]


def test_api_get_reports_each_matched_run_status(table):  # noqa: F811
    inbox.record(EMAIL, table_ref=table)
    inbox.complete("mail-1", ["invoice-intake"], table_ref=table)
    status, payload = inbox.api_get("mail-1", table_ref=table,
                                    run_status=lambda run_id: f"completed:{run_id}")
    assert status == 200
    assert payload["event"]["runs"] == [{"workflow": "invoice-intake",
                                         "run_id": "invoice-intake:mail-1",
                                         "status": "completed:invoice-intake:mail-1"}]

    def broken(run_id):
        raise RuntimeError("history down")

    _, payload = inbox.api_get("mail-1", table_ref=table, run_status=broken)
    assert payload["event"]["runs"][0]["status"] is None
