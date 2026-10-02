"""Per-vendor deterministic parsers over extracted PDF text.

Every parser claims a document by an anchor that is part of the invoice's
machine-generated layout, then reads the ledger fields off nearby lines.
The shapes here were verified against real invoices from 2024 through 2026
(sanitized fixtures under tests/fixtures/invoices):

- **AWS** (VAT invoice, USD): ``EUINDE…`` id, ``VAT Invoice Date:``,
  ``TOTAL AMOUNT USD``, ``TOTAL VAT USD``, ``billing period``, and the
  ``Account number:``/``Address:`` header block that names the account.
  Holds unchanged across the 2024, 2025 and 2026 generations.
- **OpenAI / Anthropic** (Stripe-hosted receipt, one family): the
  ``Invoice number`` header plus the vendor's name; ``Date of issue``
  (2024-style "Invoice") or ``Date paid`` (2026-style "Receipt"), the
  ``$X due``/``$X paid on`` header line, the ``VAT - …`` line, and the
  description block with the subscription period.

Amounts stay in the invoice's currency (``$`` → USD, ``€`` → EUR); the
ledger's EUR column is filled later from the bank statement, not here.
``None`` from :func:`parse_invoice` means "no known template" — the
action tier above falls back to AI extraction.
"""

import re

from .text import pdf_text

# A ledger date is ISO; the invoices print "October 1, 2026". Subscription
# ranges print abbreviated months ("Aug 11 – Sep 11"), so both forms map.
_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

_DATE_RE = re.compile(r"(January|February|March|April|May|June|July|August|"
                      r"September|October|November|December) (\d{1,2}), (\d{4})")

_CURRENCIES = {"$": "USD", "€": "EUR"}


def _iso_date(text):
    """The first ``Month d, YYYY`` in the string as ``YYYY-MM-DD``, or None."""
    match = _DATE_RE.search(text)
    if not match:
        return None
    month, day, year = match.groups()
    return f"{int(year):04d}-{_MONTHS[month.lower()]:02d}-{int(day):02d}"


def _period_month(period_text):
    """The month a billing/subscription period starts, as ``YYYY-MM``.

    Ranges print abbreviated months ("Aug 11 – Sep 11, 2024") and pypdf
    sometimes drops the dash entirely ("Jun 30 Jul 31, 2026"), so the start
    is the first month word and the year is the range's trailing year —
    stepped back one when the range crosses a year boundary ("Dec 28 -
    Jan 3, 2027" starts in 2026)."""
    if not period_text:
        return None
    start = re.search(r"([A-Za-z]+)\.?\s+\d{1,2}", period_text)
    year = re.search(r"(\d{4})", period_text)
    if not (start and year):
        return None
    month = _MONTHS.get(start.group(1).lower())
    if not month:
        return None
    months = [_MONTHS.get(w.lower()) for w in re.findall(r"[A-Za-z]+", period_text)]
    months = [m for m in months if m]
    start_year = int(year.group(1)) - (1 if months and month > months[-1] else 0)
    return f"{start_year:04d}-{month:02d}"


def _money(value):
    return float(value) if value is not None else None


class AwsInvoice:
    """AWS EMEA VAT invoice: fixed layout, USD, one account per invoice."""

    provider = "AWS"
    what = "Cloud services"
    currency = "USD"

    @staticmethod
    def claims(text):
        return re.search(r"EUINDE\d+-\d+", text)

    @classmethod
    def parse(cls, text):
        number = cls.claims(text)
        total = re.search(r"TOTAL AMOUNT USD ([\d.]+)", text)
        vat = re.search(r"TOTAL VAT USD ([\d.]+)", text)
        period = re.search(
            r"billing period ([A-Za-z]+ \d{1,2} - [A-Za-z]+ \d{1,2}, \d{4})", text)
        issued = re.search(r"VAT Invoice Date: ([A-Za-z]+ \d{1,2}, \d{4})", text)
        account = re.search(
            r"Account number:\s*\n(\d{12})\s*\nAddress:\s*\n([^\n]+)", text)
        if not (number and total and issued):
            return None
        period_text = period.group(1) if period else None
        return {
            "provider": cls.provider,
            "what": cls.what,
            "invoice_number": number.group(0),
            "date_issued": _iso_date(issued.group(1)),
            "date_paid": None,  # charged to the card after the invoice date
            "amount": _money(total.group(1)),
            "currency": cls.currency,
            "vat": _money(vat.group(1)) if vat else None,
            "period": period_text,
            "period_month": _period_month(period_text),
            "account_number": account.group(1) if account else None,
            "account": account.group(2).strip() if account else None,
        }


class StripeReceipt:
    """The Stripe-hosted receipt/invoice layout OpenAI and Anthropic use.

    Two generations share it — the 2024 "Invoice" (``Date of issue``, ``$X
    USD due``) and the 2026 "Receipt" (``Date paid``, ``$X paid on``) — so
    one parser covers both. Vendor identity comes from the issuer name in
    the text; an unknown Stripe-layout vendor stays unclaimed and rides the
    AI tier, which also classifies the "What" a new vendor needs.
    """

    # The issuer line that claims the document, mapped to the ledger's
    # canonical provider name.
    VENDORS = {
        "OpenAI": "OpenAI",
        "Anthropic": "Anthropic",
    }

    @staticmethod
    def claims(text):
        if not re.search(r"Invoice number ", text):
            return None
        for marker in StripeReceipt.VENDORS:
            if marker in text:
                return marker
        return None

    @classmethod
    def parse(cls, text):
        marker = cls.claims(text)
        if not marker:
            return None
        # The suffix is separated by a non-breaking space in the PDF text.
        number = re.search(r"Invoice number ([A-Z0-9]+(?:\s+\d+)?)", text)
        # The header line repeats the amount: "$23.80 USD due August 11, 2024"
        # or "€180.00 paid on July 12, 2026".
        header = re.search(r"([$€])([\d.]+) (?:USD )?(?:due|paid on) ", text)
        issued = re.search(r"Date of issue ([A-Za-z]+ \d{1,2}, \d{4})", text)
        paid = re.search(r"Date paid ([A-Za-z]+ \d{1,2}, \d{4})", text)
        if not (number and header):
            return None
        # The "VAT - Germany 19% on $200.00 $38.00" line carries the tax in
        # the invoice currency as its last money figure; Anthropic has none.
        vat = None
        vat_line = re.search(r"^VAT - .*$", text, re.M)
        if vat_line:
            figures = re.findall(r"[$€]([\d.]+)", vat_line.group(0))
            if figures:
                vat = _money(figures[-1])
        # The description block: the product line and the subscription range
        # right under the "Description Qty …" header.
        what = None
        period_text = None
        block = re.search(r"Description Qty[^\n]*\n+([^\n]+)\n+([^\n]+)", text)
        if block:
            what = block.group(1).strip()
            candidate = block.group(2).strip()
            if re.match(r"^[A-Za-z]{3} \d{1,2}[ –—-]*[A-Za-z]{3} \d{1,2}, \d{4}$",
                        candidate):
                period_text = candidate
            else:
                what = f"{what} {candidate}".strip()
        return {
            "provider": cls.VENDORS[marker],
            "what": what,
            "invoice_number": number.group(1),
            "date_issued": _iso_date(issued.group(1)) if issued else None,
            "date_paid": _iso_date(paid.group(1)) if paid else None,
            "amount": _money(header.group(2)),
            "currency": _CURRENCIES[header.group(1)],
            "vat": vat,
            "period": period_text,
            "period_month": _period_month(period_text),
            "account_number": None,
            "account": None,
        }


PARSERS = (AwsInvoice, StripeReceipt)


def parse_invoice(text):
    """The first parser that claims the text, as a normalized ledger entry;
    ``None`` when no known template matches (the AI tier's cue)."""
    # pypdf renders the non-breaking space some PDFs embed (Stripe invoice
    # ids) as NUL; no invoice grammar wants a NUL, the ids want a space.
    text = text.replace("\x00", " ")
    for parser in PARSERS:
        if parser.claims(text):
            return parser.parse(text)
    return None


def parse_pdf(data):
    """Extract the text and parse it; the result carries no ``text`` — the
    full extraction can be large and the entry is what the ledger needs."""
    return parse_invoice(pdf_text(data))
