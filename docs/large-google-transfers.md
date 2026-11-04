# Large Gmail attachments and Drive uploads

The CLI keeps its existing commands:

```sh
dapier gmail attachment MESSAGE_ID ATTACHMENT_ID --account me@example.com --out archive.zip
dapier gmail read MESSAGE_ID --account me@example.com --save-attachments ./attachments
dapier gdrive upload archive.zip --account me@example.com --folder FOLDER_ID --json
```

Gmail downloads request at most 1 MiB decoded per API response. Each response
contains the full attachment size and SHA-256 plus its offset and next offset.
The CLI rejects changed metadata, wrong ranges, missing bytes or a bad digest;
it atomically replaces the destination only after full validation. A failure
removes the temporary file and preserves an existing destination. Each range
currently refetches the attachment from Gmail; bytes are bounded on the Dapier
response, not on the upstream Gmail request. Account authorization applies to
every range. Raw API calls without chunk_size retain the original full-response
contract up to 4 MiB. Larger legacy requests fail explicitly with 413 rather
than returning an indistinguishable partial attachment.

Drive uploads above 1 MiB initiate a resumable upload through the existing
operator-gated upload endpoint using metadata and size only. The CLI sends
1 MiB chunks directly to the Google session URI and checks the final size and
MD5 checksum, then reads metadata back through Dapier under the selected
account. Small uploads retain the original multipart API path. Preview makes
no requests.

The session URI is a scoped upload capability. Never log or share it. The API
and CLI accept only HTTPS www.googleapis.com/upload/drive/v3/files URIs and
refuse redirects. OAuth credentials remain on the server. After a network or
5xx response the CLI queries the same session before continuing; recovery is
bounded and never automatically creates another session or file. Expiry and
uncertain outcomes are explicit errors. After an uncertain final result,
inspect the selected account before starting a fresh upload to avoid duplicates.

Provider contracts: [Drive upload guide](https://developers.google.com/workspace/drive/api/guides/manage-uploads).
The [Lambda synchronous payload limit](https://docs.aws.amazon.com/lambda/latest/dg/gettingstarted-limits.html)
is 6 MiB per request/response, including the serialized wrapper. Bounded
responses and metadata-only initiation avoid placing a large base64 file in
that payload. These limits explain the old contract defect; diagnosing any
specific production 500 still requires its runtime evidence.
