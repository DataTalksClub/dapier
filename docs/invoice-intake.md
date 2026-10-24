# Invoice intake: email to the DataOps document intake

Vendor invoice emails forwarded to `invoice@dtcdev.click` are
filed straight into the DataOps document intake. Dapier is only the
pipe — transport, subject formatting and attachment archiving; extraction and field verification live
in DataOps (`POST /api/v1/intake/email-documents`, contract
`2026-07-01`). The canonical definition is
`workflows/invoice-intake.yaml`.

## Pipeline

```
SES (invoice@dtcdev.click) → email connector → invoice-intake workflow:
  1. triage — decide the one document this email is about. Only PDFs count
     (content type application/pdf, or a .pdf name on an unnamed or
     octet-stream part); everything else — vendor HTML boilerplate, images,
     calendar parts — is dropped end to end.
       - no PDF left → the body path: strip the forwarded header and render
         the body to PDF (the else branch, completed by
         invoice-body-completion).
       - one PDF → the archive path with it.
       - several PDFs → a lone receipt wins over its paired invoice (Stripe
         sends both; the receipt is the keeper). When the keeper is
         ambiguous — no receipt-named PDF, or several — all the PDFs merge
         into one document instead of picking arbitrarily. The document key
         for archive naming is the chosen checksum's prefix, or for merges
         a hash over the sorted checksums.
  2. email-date — format the email date in UTC
  3. clean-subject — a Python code step removes forwarding/reply prefixes,
     "Invoice Available" and account/invoice brackets; AWS subjects become
     "Amazon Web Services".
  4. make-document — merge_pdfs turns triage's plan into exactly one
     document: a single attachment passes through untouched (bytes and
     checksum stay the original's), several merge page-wise in email order
     into `invoice-document/{event_id}.pdf` in the artifacts bucket.
  5. file-to-dataops — document_step points at make-document, so the intake
     stages exactly the selected document (transfer/ prefix, sha256 in
     object metadata) and posts the envelope to the DataOps connection's
     origin (`/api/v1/intake/email-documents`) with x-dataops-intake-secret.
     DataOps goes first so its invoice preview — `invoices[0]` with
     counterparty/transactionDate/invoiceNumber — can name the file. An exact duplicate
     returns its existing preview (idempotent replay). A 409 means the
     immutable source changed and remains an error.
  6. build-filename — a Python code step reads prior step outputs via
     `steps` and builds the Dropbox name: the preview's transaction date,
     counterparty and invoice number when present, else the email date;
     a missing company produces an explicit held_for_review result, never
     a subject-based filename. The checksum document key rides along so names
     stay unique. Body-only mail renders to PDF first (the else branch,
     completed by invoice-body-completion, which runs the same
     intake → build-filename → archive order).
  7. archive-if-company — archives only when build-filename produced a
     filename. Otherwise manual-review records the missing_company reason
     and DataOps intake/invoice references in the run history (Console and
     `dapier runs show`). The document stays in DataOps Finance review; no
     Dropbox upload occurs and this metadata gap does not retry into SQS.
     archive-document — dropbox_upload (source: step) archives that one
     document under the built filename with skip_existing, so replays of
     an already-archived path complete instead of failing.
```

Each email is one SQS message, so concurrent invoices file concurrently.
Dapier uses stable transfer keys for each message and document, so a retry
after a lost response submits the same source manifest. Changed document
content gets a separate source key and remains subject to the intake's
immutable-message conflict checks.
The intake is idempotent per (recipientRoute, messageId), checksum- and
size-verifies every staged object, and files each document as a
DataOps artifact — review and bookkeeping happen in the DataOps console
from there. The code steps do not change the original email subject sent to
DataOps or parse invoice contents. DataOps handles extraction, verification and
spreadsheet publication, and uses its own stable document ID for archive names.
Nothing in Dapier keeps invoice state.

Filename preview contract (DataOps intake response): when the intake
returns `invoices: [{invoiceId, counterparty, transactionDate (YYYY-MM-DD),
invoiceNumber}]` alongside `status/intakeItemId/artifacts`, build-filename
uses the first entry. Without its company, archival is held for review in
DataOps Finance. The email subject is never used as a company name.
Review state belongs to DataOps; Dapier records only the handoff outcome.

## Live setup (one-time)

1. Deploy (the Worker env already carries
   `DATAOPS_EMAIL_DOCUMENTS_BUCKET`; the stack grants write to the
   documents bucket and read to the Datamailer inbound bucket). Set the
   DataOps API origin on the connection:
   `dapier connections edit dataops --url https://ops.dtcdev.click`.
2. Store the DataOps intake token (plain string or
   `{"credential": …}` JSON) as the `dapier/dataops` secret.
3. `dapier workflows save workflows/invoice-intake.yaml && dapier
   workflows publish invoice-intake && dapier workflows on invoice-intake`.
4. Forward an AWS invoice to `invoice@dtcdev.click`; expect one
   DataOps intake artifact, not a dapier-side entry.
