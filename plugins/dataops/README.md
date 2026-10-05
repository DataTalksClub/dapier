# DataOps connection

Connect a DataOps invoice reader service credential under Connections, or use
`dapier connections import --provider dataops --token-file <private-file>`.
Both paths verify the credential against DataOps before storing it. The
connection has `invoices:read` access and no automatic expiry. Invoice parsing,
verification and bookkeeping stay in DataOps.

Manage replaces or removes Dapier's stored credential. To invalidate every copy,
rotate or clear DataOps's deployment-managed `INVOICE_READER_TOKEN_SHA256` digest
and deploy; the raw service token is never deployed to DataOps.
