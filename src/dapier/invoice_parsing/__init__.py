"""Deterministic invoice parsing: PDF text in, normalized ledger entry out.

One entry point, :func:`parse_pdf`: extract the text and hand it to the
per-vendor parsers. A parser claims a document by a stable layout anchor
(AWS by the ``EUINDE…`` invoice id, OpenAI/Anthropic by the Stripe-receipt
``Invoice number`` header) and returns the fields the bookkeeping ledger
needs. ``None`` means no known template matched — the caller (the
``invoice_parse`` action) falls back to AI extraction for those.
"""

from .parsers import parse_invoice, parse_pdf
from .text import pdf_text

__all__ = ["parse_invoice", "parse_pdf", "pdf_text"]
