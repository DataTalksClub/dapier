"""PDF text extraction for the invoice parsers.

pypdf keeps the deployment pure-Python (requirements.txt, arm64 Lambda —
no poppler binary). Its layout differs from ``pdftotext -layout``, so the
parser anchors are written against pypdf output and verified on sanitized
text fixtures (tests/fixtures/invoices) generated from real invoices.
"""

import io


def pdf_text(data: bytes) -> str:
    """The text of every page, newline-joined. Unreadable pages contribute
    nothing; a completely unreadable PDF comes back empty."""
    import pypdf

    reader = pypdf.PdfReader(io.BytesIO(data))
    parts = []
    for page in reader.pages:
        try:
            parts.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001 — a broken page must not sink the rest
            continue
    return "\n".join(parts)
