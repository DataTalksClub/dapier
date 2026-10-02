"""Deterministic invoice parsers against sanitized real-invoice text.

The fixtures under tests/fixtures/invoices are pypdf extractions of real
invoices (account numbers, personal names/addresses and VAT ids replaced)
— the same bytes shape the Lambda sees, so the anchors here are the ones
production parses with. Real PDFs never enter the repo; regenerate the
text fixtures from `.tmp/invoice-fixtures/` when a layout changes.
"""

import os

from src.dapier.invoice_parsing import parse_invoice

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "invoices")


def fixture_text(name):
    with open(os.path.join(FIXTURES, f"{name}.txt")) as fh:
        return fh.read()


def test_aws_invoice_2024():
    entry = parse_invoice(fixture_text("dropbox_aws-2024-09"))
    assert entry["provider"] == "AWS"
    assert entry["invoice_number"] == "EUINDE24-1597843"
    assert entry["date_issued"] == "2024-09-02"
    assert entry["amount"] == 252.94
    assert entry["vat"] == 40.38
    assert entry["currency"] == "USD"
    assert entry["period"] == "August 1 - August 31, 2024"
    assert entry["period_month"] == "2024-08"
    assert entry["account"] == "DataTalks.Club / DTC Owner"
    assert entry["account_number"]
    assert entry["what"] == "Cloud services"


def test_aws_invoice_2025():
    entry = parse_invoice(fixture_text("dropbox_aws-2025-06"))
    assert entry["invoice_number"] == "EUINDE25-1156257"
    assert entry["date_issued"] == "2025-06-02"
    assert entry["amount"] == 253.35
    assert entry["vat"] == 40.45
    assert entry["period_month"] == "2025-05"
    assert entry["account"] == "DataTalks.Club / DTC Owner"


def test_aws_invoice_2026_experiments():
    entry = parse_invoice(fixture_text("20261002-081535-01-EUINDE26-2122792"))
    assert entry["invoice_number"] == "EUINDE26-2122792"
    assert entry["date_issued"] == "2026-10-01"
    assert entry["amount"] == 70.57
    assert entry["vat"] == 11.27
    assert entry["period"] == "September 1 - September 30, 2026"
    assert entry["period_month"] == "2026-09"
    assert entry["account"] == "experiments"


def test_aws_invoice_2026_datatalksclub():
    entry = parse_invoice(fixture_text("20261002-081535-02-EUINDE26-2122806"))
    assert entry["invoice_number"] == "EUINDE26-2122806"
    assert entry["amount"] == 691.13
    assert entry["vat"] == 110.35
    assert entry["account"] == "DataTalks.Club / DTC Owner"


def test_aws_invoice_2026_09_pair():
    first = parse_invoice(fixture_text("dropbox_aws-2026-09"))
    assert first["amount"] == 144.53
    assert first["vat"] == 23.08
    assert first["account"] == "experiments"
    second = parse_invoice(fixture_text("dropbox_aws-2026-09-2"))
    assert second["amount"] == 508.77
    assert second["vat"] == 81.23
    assert second["account"] == "DataTalks.Club / DTC Owner"


def test_openai_invoice_2024_generation():
    entry = parse_invoice(fixture_text("dropbox_openai-2024-08"))
    assert entry["provider"] == "OpenAI"
    assert entry["invoice_number"] == "113F2C7E 0019"
    assert entry["date_issued"] == "2024-08-11"
    assert entry["date_paid"] is None  # an invoice, not a receipt
    assert entry["amount"] == 23.80
    assert entry["currency"] == "USD"
    assert entry["vat"] == 3.80
    assert entry["what"] == "ChatGPT Plus Subscription"
    assert entry["period_month"] == "2024-08"


def test_openai_receipt_2026_generation():
    entry = parse_invoice(fixture_text("dropbox_openai-2026-07"))
    assert entry["provider"] == "OpenAI"
    assert entry["invoice_number"] == "113F2C7E 0042"
    assert entry["date_paid"] == "2026-06-30"
    assert entry["amount"] == 238.00
    assert entry["currency"] == "USD"
    assert entry["vat"] == 38.00
    assert entry["what"] == "ChatGPT Pro Subscription (per seat)"
    assert entry["period_month"] == "2026-06"


def test_anthropic_receipt_eur():
    entry = parse_invoice(fixture_text("dropbox_anthropic-2026-07"))
    assert entry["provider"] == "Anthropic"
    assert entry["invoice_number"] == "5B6925AC 0014"
    assert entry["date_paid"] == "2026-07-12"
    assert entry["amount"] == 180.00
    assert entry["currency"] == "EUR"
    assert entry["vat"] is None  # no VAT line on this receipt
    assert entry["what"] == "Max plan - 20x"
    assert entry["period_month"] == "2026-07"


def test_hetzner_is_not_claimed():
    # German layout, no known anchor — this is the AI tier's cue.
    assert parse_invoice(fixture_text("dropbox_hetzner-2026-07")) is None


def test_plain_text_is_not_claimed():
    assert parse_invoice("hello world, nothing here") is None


def test_nul_separator_in_stripe_ids_is_normalized():
    # pypdf renders the non-breaking space in Stripe invoice ids as NUL;
    # the parser normalizes it so the id reads like the PDF shows it.
    assert parse_invoice("Invoice number 113F2C7E\x000019\n$23.80 USD due August 11, 2024\nOpenAI, LLC")[
        "invoice_number"] == "113F2C7E 0019"


def test_nul_separator_in_stripe_ids_is_normalized():
    # pypdf renders the non-breaking space in Stripe invoice ids as NUL;
    # the parser normalizes it so the id reads like the PDF shows it.
    assert parse_invoice("Invoice number 113F2C7E\x000019\n$23.80 USD due August 11, 2024\nOpenAI, LLC")[
        "invoice_number"] == "113F2C7E 0019"
