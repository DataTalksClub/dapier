# Zapier flow verification checklist

Tick a box only after recording evidence from the actual provider or run.
Configuration and synthetic previews have passed; real provider checks below
remain pending. Work through one flow at a time. Live tests and cutover are
separate steps. Activation and live-test results are recorded per flow below;
unreviewed migration drafts and triggers remain inactive.

## 1. Mailing-list backup — current flow

Source: Zap `157386985`; draft: `mailing-list-backup`.

- [x] Configure shared Drive `0AJbu0ZbG97XkUk9PVA`, folder `1-MAoAuQbny7FK9UQuk8800zT8M8TOQ59`, every 2 minutes, all file types.
- [x] Configure S3 bucket `datatalks-mailchimp-backup`, original filename as key, unchanged bytes.
- [x] User uploaded test file named `2026-10-02-audience_export_3fc301b1e9` (2026-10-02).
- [x] Confirm upload is in [the watched folder](https://drive.google.com/drive/folders/1-MAoAuQbny7FK9UQuk8800zT8M8TOQ59). Full name: `2026-10-02-audience_export_3fc301b1e9.zip`; Drive ID: `1huEwoRwTG3ojH8_737qpZYLSCy2zIui7`.
- [x] Verify Dapier's Google connection can see the file (Drive metadata GET returned the exact file, parent and size: 7,664,202 bytes).
- [ ] Confirm whether future exports create new files or replace existing ones, and whether names repeat.
- [x] Configure role `arn:aws:iam::387546586013:role/dapier-mailchimp-backup`; verify assumed-role identity and destination bucket access.
- [ ] Resolve Zapier's `mimetype=none` behavior: omitted Content-Type, provider default, or literal value.
- [x] Copy the uploaded ZIP to `s3://datatalks-mailchimp-backup/2026-10-02-audience_export_3fc301b1e9.zip`; verify 7,664,202 bytes and MD5 `d0f8c566634fc7faa32e602ab30b31c3`, matching Drive.
- [x] With user authorization, publish/enable `mailing-list-backup` and enable `mailchimp-drive-backup` every 2 minutes.
- [x] Fresh upload `test.pdf` triggered one completed automatic run: `mailing-list-backup:mailchimp-drive-backup-88258f905214a58f`, 2026-10-02 16:02:38–16:02:41 UTC. Both download and S3 upload completed; destination is `s3://datatalks-mailchimp-backup/test.pdf`, 233,187 bytes, MD5 `b212c0f26d090eb0971753163839c685` matching Drive.

Evidence: a Drive metadata query with `corpora=drive`, the configured drive ID
and `includeItemsFromAllDrives=true` returned the uploaded ZIP. This proves
source visibility, not copying to S3. The earlier generic file lookup returned
zero because it omitted shared-drive items.

The user authorized activation and copying this file on 2026-10-02, and requested
IAM role assumption instead of stored AWS keys. [Infrastructure PR #68](https://github.com/DataTalksClub/aws-infra/pull/68)
defines the destination role and has been merged. The role became usable
while testing. Dapier's role configuration was saved through the CLI, its
connection test reported the expected assumed role, and the ZIP was downloaded
and uploaded through the workflow's two actual provider steps. An independent
S3 lookup found the exact object, and a signed GET downloaded its bytes for the
checksum comparison. The observed S3 Content-Type is the configured literal
`none`; original Zapier MIME semantics still need comparison.

The workflow and poll are now enabled. The deployment adding role support and
API file staging passed, as did 3,798 tests. The default role/session
configuration contains no stored access keys.

Automatic delivery is verified. `test.pdf` had a new Drive creation time even
though its modification time was older; the poll correctly treated it as a new
upload. A destination lookup and signed GET independently verified the object
and matching bytes. The original ZIP was processed manually because the poll
seeds existing files without replay.

**Next flow:** email TODO. Remaining source-parity questions above concern
future export naming and Zapier's MIME behavior, not the verified delivery.

## 2. Email TODO

Source: Zap `153709869`; workflow: `todo-intake` (v5 published and enabled after the first live test).

- [x] Configure workbook `1xdeCQOLRS4vodv3GjaXNaC6t63-qdqL3X98KqJFs0dw`, worksheet ID `0`.
- [x] Configure Task `Process email "<Subject>" from <From>`, Notes blank, Status `NEW`.
- [x] Verify workbook access and columns `Date`, `Task`, `Notes`, `Status`.
- [ ] Confirm source processing-time timezone; live v5 uses `America/Chicago`, while original Zapier CST semantics remain unverified.
- [x] Verify route `todo@dtcdev.click` accepts `alexey@datatalks.club` (live receipt). Other sender policies remain outside this test.
- [x] Retest verified one correctly populated row and DataOps receipt; initial failed test and successful v5 evidence are recorded below.
- [ ] Verify existing native Telegram task creation and confirmation still work.
- [x] Publish the prepared sender/timestamp fix as v5 and confirm the workflow is On.
- [x] Retest unique subject `Dapier TODO retest 2026-10-02 Chrome v5`: one row, correct sender/subject/Notes/Status, processing timestamp and accepted DataOps receipt.


First live test (2026-10-02): source email Date was 19:32:01 UTC, Dapier
received it at 19:32:15.479192 UTC and completed run
`todo-intake:0c9b0e82a04900efeeccc65b856003d9da8496adb042e513c1492efedc2a921e`
at 19:32:21 UTC. A direct Sheets API read found exactly one matching row,
`todo!A104:D104`:

- Date: `2026-10-02` — fails the full processing-timestamp requirement.
- Task: `Process email "Dapier TODO test" from ` — sender missing; fails.
- Notes: blank — passes.
- Status: `NEW` — passes.

The actual DataOps intake response was `status: accepted`, item
`email-63f0da2c166fb52b1b815e3cfe2d87e7`. This proves receipt, not downstream
bookkeeping completion. The browser tester corrected the first send time to 19:32 UTC, matching the
provider timestamps.

V5 reads the sender header from the structured email event, writes an ISO
processing timestamp and selects worksheet ID `0`. The draft's disabled flag
was briefly published and then immediately corrected to On. The unique Chrome v5 retest below verified the updated mapping against Sheets
and DataOps.
The first test row was retained; it was not replayed or edited.


Successful v5 retest: subject `Dapier TODO retest 2026-10-02 Chrome v5`, from
`alexey@datatalks.club`, produced exactly one matching row `todo!A105:D105` in
a direct Sheets API read:

- Date: `2026-10-02T14:35:56-05:00` (19:35:56 UTC, processing time).
- Task: `Process email "Dapier TODO retest 2026-10-02 Chrome v5" from Alexey Grigorev <alexey@datatalks.club>`.
- Notes: blank.
- Status: `NEW`.

Run `todo-intake:1774d0306c420e34c55a0f9adcea64b643c47e838542fb3b6b614546925b520d`
completed all 7 steps at 19:35:59 UTC. The DataOps API returned
`status: accepted`, item `email-d0de9a5657f075cfb7cb697f8010f8cd`.
Email mapping and intake receipt pass; native Telegram and original Zapier CST
semantics remain separate checks.

## 3. Invoice email

Source: Zap `153562936`; draft: `invoice-intake`.

- [x] Configure archive `/_dtc_paperwork/invoices/<UTC email date>-<Subject>.pdf`, unchanged attachment bytes, overwrite disabled.
- [x] Forward archived invoice intake to DataOps.
- [x] Verify Dropbox account and archive folder access; DataOps intake previously accepted the TODO test. Invoice-specific receipt remains pending.
- [x] Confirm invoice route `invoice@dtcdev.click`; actual sender acceptance remains pending the email test.
- [ ] Confirm single/multiple/inline attachment selection and filename conflict behavior.
- [ ] With authorization, send one test invoice; verify date conversion, exact filename, matching bytes and DataOps receipt.
- [ ] Verify required bookkeeping output in DataOps (legacy sheet references are in the migration plan).
- [ ] Agree on cutover and verify one new invoice completes without duplicate archival/intake.

Invoice preparation (2026-10-02): Dropbox account verification passed for Alexey Grigorev. Provider metadata confirmed `/_dtc_paperwork/invoices` exists with `read_only: false` and `no_access: false`. Published invoice v5 On: UTC email date, exactly one attachment, overwrite/autorename disabled, then DataOps. No invoice email has been sent and no archive file has been created by this test. Chrome draft subject is `Dapier synthetic invoice Chrome 2026-10-02 01`; synthetic PDF is 1,855 bytes, SHA256 `a81cc4eced5b087376cd1ccc8da187b55bc72cf063bf84471d9be647769e7388`. Browser attachment upload is pending.

## 4. Telegram TODO catch hook

Source: Zap `153485577`; draft: `telegram-todo`.

- [x] Configure hook `telegram-todo` and the same TODO workbook/worksheet ID `0`.
- [x] Map sender `Date` and `Text`, Notes blank, Status `NEW`.
- [ ] Confirm the actual sender's HTTP method, content type, field capitalization and authentication.
- [ ] Agree on request-ID handling for retries versus intentional repeated tasks.
- [x] Verify workbook access and columns/order through a direct Sheets read.
- [x] Send one authenticated synthetic request; verify exactly one row and unchanged sender Date/Text.
- [ ] Agree on sender cutover, enable the hook/workflow and verify one request creates one row.

Catch-hook test (2026-10-02): published `telegram-todo` v1 On and enabled its existing bearer-authenticated hook. POST JSON returned HTTP 202 accepted. Run `telegram-todo:b7f95eb2-2ec7-4d68-97b0-f4163cd11943` completed at 19:45:44 UTC. A direct Sheets API read found exactly one matching row, `todo!A106:D106`: `2026-10-02T19:50:00Z`, `SYNTHETIC TEST Dapier Telegram catch hook 2026-10-02 01`, blank Notes, `NEW`. Date is supplied test data, not processing time. Actual upstream sender contract, retry deduplication and sender cutover remain pending.

## 5. YouTube to Slack

Source: Zap `110871466`; draft: `youtube-slack`.

- [x] Configure YouTube channel `UCDvErgK0j5ur3aLgn6U-LqQ` and Slack channel `C01BQC114P2`.
- [x] Configure display name `YouTube`, unfurling/name linking enabled, reply broadcast disabled.
- [ ] Verify Slack channel access and `chat:write.customize` permission.
- [ ] Confirm exact message spacing and approve WebSub delivery as the adjustment from polling.
- [ ] With authorization, post one test message; verify channel, name, title, URL and previews.
- [ ] Agree on cutover and verify a new video notification once.

Provider checks (2026-10-02): Slack token verification passed for DataTalks.Club (`T01ATQK62F8`); real message/channel/custom-name checks are pending the Chrome action test. YouTube live identity verification passed for DataTalksClub (`UCDvErgK0j5ur3aLgn6U-LqQ`) using the stored readonly connection. Chrome renewal outcome is unconfirmed: the UI still requests reconnection and no recent callback audit record was returned. No account binding was changed.

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
