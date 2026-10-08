"""Inbox rows for email: envelope summary, outcome words, run links.

The console's Emails page is a mailbox of what Dapier received and what
happened to each message; `dapier emails received` / `dapier inbox list`
print the same columns. Both read these fields from the inbox API.
"""
import json

from src.dapier.triggers import inbox
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
