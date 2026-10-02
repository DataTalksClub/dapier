# Invoice intake: email to the DataOps document intake

Vendor invoice emails forwarded to `invoice@mailer.dtcdev.click` are
filed straight into the DataOps document intake. Dapier is only the
pipe — one trigger, one action; extraction and human confirmation live
in DataOps (`POST /api/v1/intake/email-documents`, contract
`2026-07-01`). The canonical definition is
`workflows/invoice-intake.yaml`.

## Pipeline

```
SES (invoice@mailer.dtcdev.click) → email connector → invoice-intake workflow:
  1. file-to-dataops — stage the PDF into the DataOps documents bucket
     (transfer/ prefix, sha256 in object metadata), then post the intake
     envelope to DATAOPS_INTAKE_URL with x-dataops-intake-secret
```

Each email is one SQS message, so concurrent invoices file concurrently.
The intake is idempotent per (recipientRoute, messageId), checksum- and
size-verifies every staged object, and files each attachment as a
DataOps artifact — review and bookkeeping happen in the DataOps console
from there. Nothing in dapier keeps invoice state.

## Live setup (one-time)

1. Deploy (the Worker env already carries `DATAOPS_INTAKE_URL` and
   `DATAOPS_EMAIL_DOCUMENTS_BUCKET`; the stack grants write to the
   documents bucket and read to the Datamailer inbound bucket).
2. Store the DataOps intake token (plain string or
   `{"credential": …}` JSON) as the `dapier/dataops` secret.
3. `dapier workflows save workflows/invoice-intake.yaml && dapier
   workflows publish invoice-intake && dapier workflows on invoice-intake`.
4. Forward an AWS invoice to `invoice@mailer.dtcdev.click`; expect one
   DataOps intake artifact, not a dapier-side entry.
