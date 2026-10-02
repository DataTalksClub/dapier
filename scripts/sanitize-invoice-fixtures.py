#!/usr/bin/env python3
"""Regenerate tests/fixtures/invoices/*.txt from local real-invoice PDFs.

Real invoices never enter the repo; the parser tests run against their
pypdf text with the personal material replaced. Point this at a directory
of invoice PDFs (the working copies live in dapier/.tmp/invoice-fixtures/,
gitignored) and commit only what lands in tests/fixtures/invoices/.

    uv run --with pypdf python scripts/sanitize-invoice-fixtures.py \
        .tmp/invoice-fixtures
"""
import glob
import os
import re
import sys

import pypdf

# Replaced wherever they appear; kept in one place so the audit is easy.
CUSTOMER_VAT = "DE343190995"
PERSONAL = (
    ("alexey.s.grigoriev@gmail.com", "owner@example.com"),
    ("Alexey Grigorev", "DTC Owner"),
    ("GRIGOREV", "OWNER"),
    ("Langhansstr 70", "Hauptstr 1"),
    ("13086 Berlin", "10115 Berlin"),
    ("Berlin, Berlin, 13086, DE", "Berlin, Berlin, 10115, DE"),
    ("Schonensche Str. 13", "Hauptstr 1"),
    ("10439 Berlin", "10115 Berlin"),
)


def sanitize(text):
    """Structure-preserving scrub: real 12-digit account numbers become
    stable fakes (same shape, so the parser's digit-count anchors still
    exercise), personal names/addresses/emails and the customer VAT id
    are replaced. Vendor-identifying business data (issuer names, their
    public VAT numbers, amounts, dates) stays — the parsers read it."""
    seen = {}

    def sub_acct(match):
        value = match.group(0)
        if value not in seen:
            seen[value] = "199%09d" % (len(seen) * 7919)
        return seen[value]

    text = re.sub(r"(?<![\d.])(\d{12})(?![\d])", sub_acct, text)
    for original, replacement in PERSONAL:
        text = text.replace(original, replacement)
    text = text.replace(CUSTOMER_VAT, "DE000000000")
    text = re.sub(r"DE ?VAT ?DE\d+", "DE VAT DE000000000", text)
    # Keep the fixtures NUL-free so git stays text (the parser normalizes
    # NUL itself — see parse_invoice; pypdf renders some non-breaking
    # spaces as NUL).
    return text.replace("\x00", " ")


def main(source_dir):
    out_dir = os.path.join(os.path.dirname(__file__), "..", "tests",
                           "fixtures", "invoices")
    for pdf in sorted(glob.glob(os.path.join(source_dir, "**", "*.pdf"),
                                recursive=True)):
        name = os.path.relpath(pdf, source_dir).replace(os.sep, "_")[:-len(".pdf")]
        text = "\n".join(page.extract_text() or ""
                         for page in pypdf.PdfReader(pdf).pages)
        with open(os.path.join(out_dir, f"{name}.txt"), "w") as fh:
            fh.write(sanitize(text))
        print(f"{name}.txt")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    main(sys.argv[1])
