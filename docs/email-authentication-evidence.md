# Received email authentication evidence

When the inbox detail API rehydrates a stored raw email, it includes its bounded
`authentication_results`, `received_spf` and `dkim_signatures` header lists under `event.email`.
`dapier runs events show INBOX_ID` prints these alongside From, recipient and Message-ID.
This lets operators inspect received authentication evidence through Dapier's
existing authenticated API without fetching mailbox objects with AWS credentials.

These are observed header values, not an authorization decision: identify the
receiving server's Authentication-Results entry before relying on it. Arbitrary
headers are not exposed. Messages whose raw copy cannot be read retain the
existing best-effort body fallback. No routing, allowlists or delivery behavior
changes.
