# Zapier flow verification checklist

Tick a box only after recording evidence from the actual provider or run.
Configuration and synthetic previews have passed; real provider checks below
remain pending. Work through one flow at a time. Live tests and cutover are
separate steps; the migration drafts and new triggers are inactive.

## 1. Mailing-list backup — current flow

Source: Zap `157386985`; draft: `mailing-list-backup`.

- [x] Configure shared Drive `0AJbu0ZbG97XkUk9PVA`, folder `1-MAoAuQbny7FK9UQuk8800zT8M8TOQ59`, every 2 minutes, all file types.
- [x] Configure S3 bucket `datatalks-mailchimp-backup`, original filename as key, unchanged bytes.
- [x] User uploaded test file named `2026-10-02-audience_export_3fc301b1e9` (2026-10-02).
- [x] Confirm upload is in [the watched folder](https://drive.google.com/drive/folders/1-MAoAuQbny7FK9UQuk8800zT8M8TOQ59). Full name: `2026-10-02-audience_export_3fc301b1e9.zip`; Drive ID: `1huEwoRwTG3ojH8_737qpZYLSCy2zIui7`.
- [x] Verify Dapier's Google connection can see the file (Drive metadata GET returned the exact file, parent and size: 7,664,202 bytes).
- [ ] Confirm whether future exports create new files or replace existing ones, and whether names repeat.
- [ ] Verify S3 credential access to the destination bucket.
- [ ] Resolve Zapier's `mimetype=none` behavior: omitted Content-Type, provider default, or literal value.
- [ ] With authorization, copy one test file; verify exact S3 key and matching bytes.
- [ ] Agree on cutover, publish/enable the workflow and poll, then upload a fresh file and verify one successful run.

Evidence: a Drive metadata query with `corpora=drive`, the configured drive ID
and `includeItemsFromAllDrives=true` returned the uploaded ZIP. This proves
source visibility, not copying to S3. The earlier generic file lookup returned
zero because it omitted shared-drive items.

The user authorized activation and copying this file on 2026-10-02, and requested
IAM role assumption instead of stored AWS keys. [Infrastructure PR #68](https://github.com/DataTalksClub/aws-infra/pull/68)
defines the destination role. S3 access, copying and activation remain pending
role deployment and the live test. Do not tick backup success until the
actual S3 object and matching bytes are verified.

**Next check:** deploy the reviewed backup role, configure it through Dapier,
then copy the existing ZIP and verify the destination. The poll seeds existing
files without replay; a fresh upload will separately verify automatic delivery.

## 2. Email TODO

Source: Zap `153709869`; draft: `todo-intake`.

- [x] Configure workbook `1xdeCQOLRS4vodv3GjaXNaC6t63-qdqL3X98KqJFs0dw`, worksheet ID `0`.
- [x] Configure Task `Process email "<Subject>" from <From>`, Notes blank, Status `NEW`.
- [ ] Verify workbook access and worksheet columns/order.
- [ ] Confirm processing-time timezone; draft uses `America/Chicago`, source CST semantics remain unverified.
- [ ] Confirm email route and accepted senders.
- [ ] With authorization, send one test email; verify the exact row, timestamp and sender text, plus DataOps delivery.
- [ ] Verify existing native Telegram task creation and confirmation still work.
- [ ] Agree on cutover and verify a new email creates exactly one intended row.

## 3. Invoice email

Source: Zap `153562936`; draft: `invoice-intake`.

- [x] Configure archive `/_dtc_paperwork/invoices/<UTC email date>-<Subject>.pdf`, unchanged attachment bytes, overwrite disabled.
- [x] Forward archived invoice intake to DataOps.
- [ ] Verify Dropbox archive access and DataOps intake availability.
- [ ] Confirm email route and accepted senders.
- [ ] Confirm single/multiple/inline attachment selection and filename conflict behavior.
- [ ] With authorization, send one test invoice; verify date conversion, exact filename, matching bytes and DataOps receipt.
- [ ] Verify required bookkeeping output in DataOps (legacy sheet references are in the migration plan).
- [ ] Agree on cutover and verify one new invoice completes without duplicate archival/intake.

## 4. Telegram TODO catch hook

Source: Zap `153485577`; draft: `telegram-todo`.

- [x] Configure hook `telegram-todo` and the same TODO workbook/worksheet ID `0`.
- [x] Map sender `Date` and `Text`, Notes blank, Status `NEW`.
- [ ] Confirm the actual sender's HTTP method, content type, field capitalization and authentication.
- [ ] Agree on request-ID handling for retries versus intentional repeated tasks.
- [ ] Verify workbook access and columns/order.
- [ ] With authorization, send one request; verify the exact row and unchanged sender Date/Text.
- [ ] Agree on sender cutover, enable the hook/workflow and verify one request creates one row.

## 5. YouTube to Slack

Source: Zap `110871466`; draft: `youtube-slack`.

- [x] Configure YouTube channel `UCDvErgK0j5ur3aLgn6U-LqQ` and Slack channel `C01BQC114P2`.
- [x] Configure display name `YouTube`, unfurling/name linking enabled, reply broadcast disabled.
- [ ] Verify Slack channel access and `chat:write.customize` permission.
- [ ] Confirm exact message spacing and approve WebSub delivery as the adjustment from polling.
- [ ] With authorization, post one test message; verify channel, name, title, URL and previews.
- [ ] Agree on cutover and verify a new video notification once.

## 6. Dropbox invoice landing

Source: Zap `155120966`; draft: `dropbox_on_upload`.

- [x] Configure `/_dtc_paperwork/invoices-landing`, every 2 minutes, all file types.
- [x] Configure processing-date prefix, original extension once, move to `/_dtc_paperwork/invoices`, then DataOps intake.
- [ ] Verify Dropbox access to both folders and DataOps intake availability.
- [ ] Confirm processing-date timezone; draft currently uses UTC.
- [ ] Confirm filename collisions and already-prefixed filename behavior.
- [ ] With authorization, upload one test file; verify exact destination name, matching bytes, removal from landing and DataOps receipt.
- [ ] Verify required bookkeeping output in DataOps.
- [ ] Agree on cutover, enable the workflow/poll, upload a fresh file and verify one complete run.

For implementation details and unresolved source semantics, see
[the migration plan](zapier-migration-plan.md#adjustments-and-remaining-source-provider-checks).
