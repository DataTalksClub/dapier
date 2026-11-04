# HTML Gmail drafts

Create a new draft from UTF-8 HTML, optionally including a plain text alternative:

```sh
dapier gmail draft create --account me@example.com --to peer@example.com --subject "Report" --html-file body.html --file body.txt --attach report.pdf
```

`--file` and `--text` remain plain text. `--html-file` is a separate HTML part;
with both bodies the API builds multipart/alternative, preserving attachments.
Use `--preview` to inspect the request without calling the API. Either file may
be `-` for stdin, but both cannot consume stdin in the same command.

`draft update DRAFT_ID` supports the same body flags. It replaces the body and
attachments; include every part and attachment that should remain.

Read back the HTML with `dapier gmail draft get DRAFT_ID --account me@example.com --html`.
`--json` includes both `text` and `html`. Without either flag, get prints plain text.
Existing message reads already support `dapier gmail read MESSAGE_ID --account me@example.com --html`.
Draft create/update do not send mail; sending requires the separate send command.
