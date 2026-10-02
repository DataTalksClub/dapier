# Invoice intake: email to reviewed bookkeeping entry

Vendor invoice emails forwarded to `invoice@dtcdev.click` are
parsed and staged for human review. The pipeline is the canonical
`workflows/invoice-intake.yaml`; the review queue is the console's
**Bookkeeping** view and the `dapier bookkeeping` noun.

## Pipeline

```
SES (invoice@) → email connector → invoice-intake workflow:
  1. invoice_parse   — deterministic parsers first (see below)
  2. bookkeeping_stage — pending entry in BookkeepingEntriesTable
  3. dropbox_upload  — original PDF filed to _dtc_paperwork/invoices/auto
```

Each email is one SQS message, so concurrent invoices parse concurrently.

## Two extraction tiers

- **Deterministic** (`src/dapier/invoice_parsing/`): AWS VAT invoices
  (anchored on the `EUINDE…` id, totals, billing period, account header
  block — stable across the 2024, 2025 and 2026 generations) and the
  Stripe receipt/invoice family OpenAI and Anthropic share (both the 2024
  "Invoice" and the 2026 "Receipt" generations). Verified against
  sanitized real-invoice text fixtures in `tests/fixtures/invoices/`;
  regenerate them from `dapier/.tmp/invoice-fixtures/` (gitignored — real
  PDFs never enter the repo) when a layout changes.
- **AI fallback**: when no parser claims the PDF and the step sets
  `ai_fallback: true`, gpt-4o-mini (`ai_complete`'s copilot config)
  extracts the same fields as JSON. A reply missing provider or amount
  stages nothing (`matched: false` in run history).

Both tiers emit the same entry shape with a `source` flag
(`deterministic` | `ai`) — the reviewer double-checks the AI ones.

## The human gate

Every parsed invoice becomes a **pending** entry. Nothing reaches the
ledger or any report until a reviewer confirms it — in the console
(fields are correctable before confirming) or with:

```
dapier bookkeeping list [--status pending]
dapier bookkeeping get <id>
dapier bookkeeping confirm <id> --edit amount=691.13 --edit what="Cloud services"
dapier bookkeeping reject <id> --note "duplicate"
```

Confirm applies the edits and finalizes the entry; reject frees the
dedup marker, so forwarding the same invoice again stages a fresh entry.
Redelivered invoices dedupe on provider + invoice number and never stage
twice.

## Live setup (one-time)

1. Deploy (the table `BookkeepingEntriesTable` ships with the stack).
2. Connect the `dropbox` connection (filing).
3. Set `COPILOT_LLM_API_KEY` on the Worker (the AI tier is a no-op
   fallback without it — deterministic vendors still parse).
4. `dapier workflows save workflows/invoice-intake.yaml && dapier
   workflows publish invoice-intake && dapier workflows on invoice-intake`.
5. Forward an AWS invoice to `invoice@dtcdev.click`, confirm the
   entry, then revoke the parsing-era `invoice-hunt` dropbox grant if
   still present.

## Vendors

AWS, OpenAI and Anthropic parse deterministically. The next candidates
for promotion from the AI tier (samples exist in the Dropbox invoice
archive): Hetzner, Google Workspace, Mailchimp. Promoting a vendor means
one parser class in `invoice_parsing/parsers.py` keyed on a stable layout
anchor, plus its sanitized text fixture.
