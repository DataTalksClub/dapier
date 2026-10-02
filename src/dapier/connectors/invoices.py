"""Invoice intake actions: parse an attached invoice PDF, stage it for
review.

The catalog entries for the engine-side runners (``engine/actions/
invoice.py`` and ``engine/actions/bookkeeping.py``). The parse step never
writes; the stage step reads the parse step's output and files the pending
bookkeeping entry a reviewer confirms. Registered like the storage
actions — built-ins, no connection needed.
"""
from ..engine.actions.bookkeeping import run_bookkeeping_stage
from ..engine.actions.invoice import run_invoice_parse
from .registry import Action, register

register(Action(
    type="invoice_parse",
    label="Invoices: parse PDF",
    icon="receipt",
    description=(
        "Parse the message's invoice PDF attachment into bookkeeping fields. "
        "Known vendor templates (AWS, OpenAI, Anthropic) parse "
        "deterministically; other layouts go to the AI (gpt-4o-mini) when "
        "ai_fallback is on. Output: {ok, matched, source: "
        "deterministic|ai, entry, pdf} — matched: false stages nothing."),
    run=lambda action, event, workflow_id, steps=None: run_invoice_parse(
        action, event, workflow_id, steps=steps),
    required=frozenset(),
    optional=frozenset({"attachment_index", "ai_fallback", "ai_system",
                        "timeout_seconds"}),
    fields=(
        {"key": "attachment_index", "label": "Attachment index", "type": "number",
         "help": "0-based index into the message's attachments; omitted "
                 "parses the first PDF attachment"},
        {"key": "ai_fallback", "label": "AI fallback", "type": "boolean",
         "default": "false",
         "help": "Extract unknown vendors with gpt-4o-mini (ai_complete) "
                 "when no deterministic parser claims the PDF"},
        {"key": "ai_system", "label": "AI system message", "type": "textarea",
         "help": "Optional override of the extraction prompt's system half"},
        {"key": "timeout_seconds", "label": "AI timeout (s)", "type": "number"},
    ),
))

register(Action(
    type="bookkeeping_stage",
    label="Bookkeeping: stage for review",
    icon="receipt",
    description=(
        "File a parsed invoice as a pending bookkeeping entry. Nothing "
        "reaches the ledger until a human confirms the entry in the review "
        "queue; a redelivered invoice dedupes into the existing entry. "
        "Reads the entry from the parse step (entry_from). Output: "
        "{entry_id, status, deduped, provider, invoice_number}."),
    run=lambda action, event, workflow_id, steps=None: run_bookkeeping_stage(
        action, event, workflow_id, steps=steps),
    required=frozenset(),
    optional=frozenset({"entry_from"}),
    fields=(
        {"key": "entry_from", "label": "Parse step id", "type": "text",
         "placeholder": "parse",
         "help": "The invoice_parse step whose output.entry to stage"},
    ),
))
