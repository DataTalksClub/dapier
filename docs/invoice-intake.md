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
  1. triage — count document attachments; email boilerplate (text/html and
     text/plain parts, e.g. stapled Terms of Service) is ignored end to end
     (triage, archive naming, the Dropbox upload via exclude_content_types,
     and the DataOps intake staging). Two or more documents fail the run.
  2. email-date — format the email date in UTC
  3. clean-subject — a Python code step removes forwarding/reply prefixes,
     "Invoice Available" and account/invoice brackets; AWS subjects become
     "Amazon Web Services". It returns a stable attachment checksum suffix.
  4. archive-attachment — archive the original PDF using date, clean subject
     and checksum suffix, so different invoices on the same day do not collide.
  5. file-to-dataops — stage the PDF into the DataOps documents bucket
     (transfer/ prefix, sha256 in object metadata), then post the intake
     envelope to DATAOPS_INTAKE_URL with x-dataops-intake-secret
```

Each email is one SQS message, so concurrent invoices file concurrently.
Dapier uses stable transfer keys for each message and document, so a retry
after a lost response submits the same source manifest. Changed document
content gets a separate source key and remains subject to the intake's
immutable-message conflict checks.
The intake is idempotent per (recipientRoute, messageId), checksum- and
size-verifies every staged object, and files each attachment as a
DataOps artifact — review and bookkeeping happen in the DataOps console
from there. The code step does not change the original email subject sent to
DataOps or parse invoice contents. DataOps handles extraction, verification and
spreadsheet publication, and uses its own stable document ID for archive names.
Nothing in Dapier keeps invoice state.

## Live setup (one-time)

1. Deploy (the Worker env already carries `DATAOPS_INTAKE_URL` and
   `DATAOPS_EMAIL_DOCUMENTS_BUCKET`; the stack grants write to the
   documents bucket and read to the Datamailer inbound bucket).
2. Store the DataOps intake token (plain string or
   `{"credential": …}` JSON) as the `dapier/dataops` secret.
3. `dapier workflows save workflows/invoice-intake.yaml && dapier
   workflows publish invoice-intake && dapier workflows on invoice-intake`.
4. Forward an AWS invoice to `invoice@dtcdev.click`; expect one
   DataOps intake artifact, not a dapier-side entry.
