"""invoice_parse: one invoice PDF attachment in, a normalized ledger entry
out — deterministic parsers first, gpt-4o-mini extraction for layouts no
parser claims.

The deterministic tier (``src.dapier.invoice_parsing``) answers known
vendor templates exactly; the AI tier runs only when it does not claim the
document, so its cost and its drift are limited to genuinely unknown
vendors. Both tiers emit the same entry shape with a ``source`` flag
(``deterministic``/``ai``) so the reviewer knows which rows to double-check
— nothing here writes to the bookkeeping queue; the ``bookkeeping_stage``
step does that after the workflow has had its say.
"""

from . import base
from ...invoice_parsing import pdf_text, parse_invoice
from ...invoice_parsing.parsers import _period_month

# The AI tier's contract: the same fields the deterministic parsers emit,
# in invoice currency, no invented values (``null`` over guesses). The text
# rides the prompt pre-rendered — run_ai_complete would otherwise template
# any ``{braces}`` the invoice text happens to contain.
AI_SYSTEM = (
    "You extract bookkeeping fields from vendor invoice emails. Answer with "
    "one JSON object and nothing else. Use null for anything the invoice "
    "does not state; never guess. Amounts stay in the invoice's currency."
)

AI_PROMPT_TEMPLATE = (
    "Extract the invoice fields from this invoice document text.\n\n"
    "Return a JSON object with exactly these keys:\n"
    '- "provider": the canonical vendor name, e.g. "Hetzner", '
    '"Google Workspace"\n'
    '- "what": what was purchased, from the product/description line\n'
    '- "invoice_number": the invoice or receipt number as printed\n'
    '- "date_issued": the issue date as YYYY-MM-DD, or null\n'
    '- "date_paid": the payment date as YYYY-MM-DD, or null\n'
    '- "amount": the invoice total, as a number\n'
    '- "currency": "USD", "EUR", ... \n'
    '- "vat": the VAT/tax amount as a number, or null\n'
    '- "period": the billing or subscription period as printed, or null\n'
    '- "account": the account or customer name on the invoice, or null\n\n'
    "Invoice text:\n\n{invoice_text}"
)


def _wants_fallback(action):
    value = action.get("ai_fallback")
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("true", "1", "yes", "on")


def _is_pdf(attachment):
    content_type = str(attachment.get("content_type") or "").lower()
    filename = str(attachment.get("filename") or "").lower()
    return "pdf" in content_type or filename.endswith(".pdf")


def pick_attachment(event, index=None):
    """The attachment to parse: the given index, else the first PDF-looking
    attachment, else the first attachment. Raises when the message has no
    parseable attachment — a loud miss, like any unconfigured action."""
    data = event.get("data") or {}
    attachments = [a for a in (data.get("attachments") or []) if isinstance(a, dict)]
    if not attachments:
        raise ValueError("invoice_parse: the message has no attachments")
    if index is not None:
        try:
            chosen = attachments[int(index)]
        except (TypeError, ValueError):
            raise ValueError(
                f"invoice_parse: attachment_index must be a number, got {index!r}") from None
    else:
        pdfs = [a for a in attachments if _is_pdf(a)]
        chosen = (pdfs or attachments)[0]
    ref = chosen.get("s3") or {}
    if not (ref.get("bucket") and ref.get("key")):
        raise ValueError(
            "invoice_parse: the chosen attachment has no s3 pointer to read "
            "(filename may be inline-only)")
    return chosen


def _pdf_pointer(chosen):
    return {
        "filename": chosen.get("filename"),
        "content_type": chosen.get("content_type"),
        "checksum": chosen.get("checksum"),
        "s3": chosen.get("s3"),
    }


def _normalize_ai_entry(data):
    """The AI's JSON object into the deterministic parsers' entry shape, or
    None when it did not produce the two fields every entry needs."""
    if not isinstance(data, dict):
        return None
    entry = {}
    for key in ("provider", "what", "invoice_number", "date_issued",
                "date_paid", "currency", "period", "account"):
        value = data.get(key)
        if isinstance(value, str) and value.strip() and value.strip().lower() != "null":
            entry[key] = value.strip()
    for key in ("amount", "vat"):
        value = data.get(key)
        if value is None or (isinstance(value, str) and value.strip().lower() in ("", "null")):
            continue
        try:
            entry[key] = float(value)
        except (TypeError, ValueError):
            return None
    if not entry.get("provider") or "amount" not in entry:
        return None
    entry["period_month"] = _period_month(entry.get("period"))
    if not entry.get("currency"):
        entry["currency"] = "USD"
    return entry


def run_invoice_parse(action, event, workflow_id, steps=None, transport=None):
    """Parse the message's invoice attachment; ``{ok, matched, source,
    entry, pdf, ...}``.

    ``matched: false`` with ``ok: true`` means no parser claimed the
    document and no AI fallback is configured — the run succeeds and the
    run history shows why nothing was staged."""
    chosen = pick_attachment(event, action.get("attachment_index"))
    body = base._s3_body(chosen["s3"])
    text = pdf_text(body)
    pointer = _pdf_pointer(chosen)
    entry = parse_invoice(text)
    if entry is not None:
        return {"ok": True, "matched": True, "source": "deterministic",
                "entry": entry, "pdf": pointer, "text_chars": len(text)}
    if not _wants_fallback(action):
        return {"ok": True, "matched": False, "source": None,
                "pdf": pointer, "text_chars": len(text)}
    # Lazy import: connector modules import engine actions at registration
    # time, so engine -> connectors at module level would close a cycle.
    from ...connectors.ai import run_ai_complete

    ai_action = {
        "prompt": AI_PROMPT_TEMPLATE.format(
            invoice_text=text.replace("{", "{{").replace("}", "}}")),
        "system": str(action.get("ai_system") or "").strip() or AI_SYSTEM,
        "json_mode": True,
    }
    if action.get("timeout_seconds") is not None:
        ai_action["timeout_seconds"] = action.get("timeout_seconds")
    ai = run_ai_complete(ai_action, event, steps=steps, transport=transport)
    if not ai.get("ok"):
        return {"ok": True, "matched": False, "source": "ai",
                "ai_error": ai.get("error"), "pdf": pointer,
                "text_chars": len(text)}
    entry = _normalize_ai_entry(ai.get("data"))
    if entry is None:
        return {"ok": True, "matched": False, "source": "ai",
                "ai_error": "the reply is missing provider or amount",
                "ai_reply": ai.get("data"), "pdf": pointer,
                "text_chars": len(text)}
    return {"ok": True, "matched": True, "source": "ai", "entry": entry,
            "model": ai.get("model"), "pdf": pointer, "text_chars": len(text)}
