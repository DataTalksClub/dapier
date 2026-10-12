# DataOps connection

Connect a DataOps invoice reader service credential under Connections, or use
`dapier connections import dataops --provider dataops --token-file <private-file> --url https://ops.dtcdev.click`.
Both paths verify the credential against DataOps before storing it. The
connection holds the DataOps API origin (`url`); dataops steps append
`/api/v1/intake/leads` or `/api/v1/intake/email-documents`. The connection
has `invoices:read` access and no automatic expiry. Invoice parsing,
verification and bookkeeping stay in DataOps.
Set or change the origin later with `dapier connections edit dataops --url https://ops.dtcdev.click`.

Manage replaces or removes Dapier's stored credential. To invalidate every copy,
rotate or clear DataOps's deployment-managed `INVOICE_READER_TOKEN_SHA256` digest
and deploy; the raw service token is never deployed to DataOps.
