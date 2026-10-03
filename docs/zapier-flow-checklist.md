# Zapier flow verification checklist

Tick a box only after recording provider or execution evidence. Current acceptance
results are in [the October 3 completion report](zapier-integration-completion-20261003.md).
Executable checks within the synthetic-only scope passed, including the selected
seventh body-to-PDF path. Required final DataOps publication, genuine YouTube
delivery, production cutover and source-equivalence checks remain explicitly
unverified below; their acceptance criteria have not been narrowed. Earlier dated evidence below is retained as history.

## 1. Mailing-list backup — current flow

Source: Zap `157386985`; draft: `mailing-list-backup`.

- [x] Configure shared Drive `0AJbu0ZbG97XkUk9PVA`, folder `1-MAoAuQbny7FK9UQuk8800zT8M8TOQ59`, every 2 minutes, all file types.
- [x] Configure S3 bucket `datatalks-mailchimp-backup`, original filename as key, unchanged bytes.
- [x] User uploaded test file named `2026-10-02-audience_export_3fc301b1e9` (2026-10-02).
- [x] Confirm upload is in [the watched folder](https://drive.google.com/drive/folders/1-MAoAuQbny7FK9UQuk8800zT8M8TOQ59). Full name: `2026-10-02-audience_export_3fc301b1e9.zip`; Drive ID: `1huEwoRwTG3ojH8_737qpZYLSCy2zIui7`.
- [x] Verify Dapier's Google connection can see the file (Drive metadata GET returned the exact file, parent and size: 7,664,202 bytes).
- [ ] Confirm whether future exports create new files or replace existing ones, and whether names repeat; original exporter behavior unavailable.
- [x] Separately test intended Drive create/replacement/repeated-name/idle-poll semantics against providers.
- [x] Configure role `arn:aws:iam::387546586013:role/dapier-mailchimp-backup`; verify assumed-role identity and destination bucket access.
- [ ] Resolve original Zapier `mimetype=none` adapter behavior; original HTTP evidence unavailable.
- [x] Separately verify selected literal `none` Content-Type through S3 provider GET.
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
- [ ] Confirm source processing-time timezone; original Zapier CST metadata unavailable.
- [x] Separately verify intended Chicago timezone across midnight and DST boundaries.
- [x] Verify route `todo@dtcdev.click` accepts `alexey@datatalks.club` (live receipt). Other sender policies remain outside this test.
- [x] Retest verified one correctly populated row and DataOps receipt; initial failed test and successful v5 evidence are recorded below.
- [x] Verify existing native Telegram task creation and confirmation through the real private bot.
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

Native Telegram evidence (2026-10-02): Alexey sent `/todo SYNTHETIC TEST Dapier Chrome native Telegram 2026-10-02 01` privately to `@dtc_todo_bot`. Run `todo-intake:todo-446915fcc743d237` completed at 19:57:38 UTC. Direct Sheets read found exactly one row, `todo!A107:D107`: `2026-10-02`, command-stripped task text, blank Notes, `NEW`. Telegram returned confirmation message ID `7212`; the browser tester confirmed receipt. This is real Telegram delivery, separate from the catch-hook test.

A subsequent private voice message was not our test. The user screenshot showed a blank-task confirmation. No audio was fetched, transcribed or replayed. Published TODO v7 On with a guard shared by native append and confirmation: missing, empty, whitespace or command-only text creates no task and sends no success confirmation. Voice/document deliveries remain available to other handlers; this flow does not add media processing. A synthetic missing-text CLI execution produced zero sheet/intake/send actions, and regression tests preserve the existing text and email mappings.

Real v7 text retest: Chrome privately sent `/todo SYNTHETIC TEST Dapier Chrome text guard v7 2026-10-02 02`. Run `todo-intake:todo-2fbf739c077c1977` completed at 20:06:35 UTC. Direct Sheets read found exactly one matching row, `todo!A109:D109`, with `2026-10-02`, the command-stripped task, blank Notes and `NEW`. Telegram returned confirmation message `7216`. This verifies the guard preserves the real native text path; no private audio was opened.

## 3. Invoice email

Source: Zap `153562936`; draft: `invoice-intake`.

- [x] Configure archive `/_dtc_paperwork/invoices/<UTC email date>-<Subject>.pdf`, unchanged attachment bytes, overwrite disabled.
- [x] Forward archived invoice intake to DataOps.
- [x] Verify Dropbox account and archive folder access; DataOps intake previously accepted the TODO test. Invoice-specific receipt remains pending.
- [x] Verify `alexey@datatalks.club` sender acceptance through actual Gmail delivery to `invoice@dtcdev.click`.
- [ ] Confirm original source single/multiple/inline selection and filename-conflict defaults; not exported.
- [x] Separately verify intended single selection and strict same-byte/changed-byte collision behavior.
- [x] Send an actual synthetic invoice email; verify receipt, UTC date conversion, exact filename, matching bytes and DataOps intake.
- [x] Verify the clearly labelled synthetic CLI fallback: exact archive bytes and accepted DataOps receipt, without claiming email delivery.
- [x] Verify required final bookkeeping publication/output for invoice email through the actual DataOps worker/provider factory: isolated live Sheet/Dropbox destinations, local DynamoDB ledger, exact bytes/rows and retry dedupe. Production ledger publication was not performed.
- [x] Verify canonical dedupe, pending draft save/reject audit and publication gate separately; no synthetic ledger publication.
- [x] Verify one actual Gmail invoice completes without duplicate archival/intake; one matching run and provider receipt were observed.

Invoice preparation (2026-10-02): Dropbox account verification passed for Alexey Grigorev. Provider metadata confirmed `/_dtc_paperwork/invoices` exists with `read_only: false` and `no_access: false`. Published invoice v5 On: UTC email date, exactly one attachment, overwrite/autorename disabled, then DataOps. Actual invoice email remains unsent; the CLI fallback archive and receipt evidence are recorded below. Chrome draft subject is `Dapier synthetic invoice Chrome 2026-10-02 01`; synthetic PDF is 1,855 bytes, SHA256 `a81cc4eced5b087376cd1ccc8da187b55bc72cf063bf84471d9be647769e7388`. Browser attachment upload is pending.

Invoice fallback evidence: the original Chrome PDF was copied locally, verified as 1,855 bytes with SHA256 `a81cc4eced5b087376cd1ccc8da187b55bc72cf063bf84471d9be647769e7388`, and staged under `/dapier-integration-tests/`. Dapier SES sending failed before delivery with `AccessDenied ses:SendRawEmail`. The Gmail draft remains unsent; no inbound invoice email is claimed. A synthetic CLI email-shaped event archived exactly one file at `/_dtc_paperwork/invoices/2026-10-02-Dapier synthetic invoice fallback 2026-10-02 01.pdf`. Direct Dropbox download matched every original byte. API execution then failed on missing `secretsmanager:GetSecretValue` permission. An isolated worker fallback forwarded the same attachment and DataOps accepted intake `email-e572719789afd8f9e5f9b29d559f6a90`, artifact `email-document-4d519a4bce17ab855060e39e17417b9e`, status `needs-review`. This is CLI/archive/worker evidence, not Chrome email delivery or completed bookkeeping.

After deployment, an API/CLI intake-only retry returned `status: duplicate` with the same `email-e572719789afd8f9e5f9b29d559f6a90` and artifact; no archive action ran again and no second intake was created. The missing API permissions were fixed with `ses:SendRawEmail`, read access only to `dapier/dataops`, and writes only to the configured documents bucket `transfer/*` prefix. Actual email delivery remains pending; the original failed subject was not retried. At the user's request after deployment, exactly one fresh-subject attempt (`Dapier synthetic invoice end-to-end 2026-10-02 02`) was made with the already staged fixture, without re-uploading it. SES rejected it before delivery with `MessageRejected`: both `no-reply@dtcdev.click` and `invoice@dtcdev.click` are unverified in `EU-WEST-1`. No new matching invoice email event or worker run appeared. There was no retry. Verify those SES identities before another Dapier-send attempt, or complete attachment upload and send the retained external Gmail draft.

Actual Gmail integration test (2026-10-02): Chrome sent from `alexey@datatalks.club` to `invoice@dtcdev.click`, subject `Dapier synthetic invoice Chrome 2026-10-02 01`. The email Date was `2026-10-02T20:27:51Z`; ingress received it at 20:28:06 UTC and matched only `invoice-intake`. Run `invoice-intake:6e92007774448da16bd89f84b51740a60f1ff0859da873fffaaac40bb8d41b94` completed all three steps at 20:28:12 UTC. It archived `/_dtc_paperwork/invoices/2026-10-02-Dapier synthetic invoice Chrome 2026-10-02 01.pdf`. A direct Dropbox download matched all 1,855 bytes and SHA256 `a81cc4eced5b087376cd1ccc8da187b55bc72cf063bf84471d9be647769e7388`; the incoming attachment metadata carried the same checksum. DataOps accepted intake `email-98789095e7d530f15a054227e36e428d`, artifact `email-document-b138c9ca3dec87b5d2ef6430ef164c25`, status `needs-review`. Exactly one matching completed run was found. This completes actual Gmail ingress verification; the SES outbound identity limitation remains separate. No SES send was retried during this test.

## 4. Telegram TODO catch hook

Source: Zap `153485577`; draft: `telegram-todo`.

- [x] Configure hook `telegram-todo` and the same TODO workbook/worksheet ID `0`.
- [x] Map sender `Date` and `Text`, Notes blank, Status `NEW`.
- Compatibility limitation: original sender unknown; native Telegram is primary. Sender discovery is conditional, not an acceptance requirement.
- Conditional sender migration: if recovered, configure a stable ID. Isolated JSON/form stable-ID retries already passed; compatibility configuration remains unchanged.
- [x] Verify workbook access and columns/order through a direct Sheets read.
- [x] Send one authenticated synthetic request; verify exactly one row and unchanged sender Date/Text.
- [x] Verify isolated JSON/form authentication, stable-ID retry suppression, distinct-ID repeats and missing-ID behavior without external writes.
- Conditional cutover: existing compatibility hook retained unchanged; no sender cutover was performed or claimed.

Catch-hook test (2026-10-02): published `telegram-todo` v1 On and enabled its existing bearer-authenticated hook. POST JSON returned HTTP 202 accepted. Run `telegram-todo:b7f95eb2-2ec7-4d68-97b0-f4163cd11943` completed at 19:45:44 UTC. A direct Sheets API read found exactly one matching row, `todo!A106:D106`: `2026-10-02T19:50:00Z`, `SYNTHETIC TEST Dapier Telegram catch hook 2026-10-02 01`, blank Notes, `NEW`. Date is supplied test data, not processing time. Actual upstream sender contract, retry deduplication and sender cutover remain pending.

## 5. YouTube to Slack

Source: Zap `110871466`; draft: `youtube-slack`.

- [x] Configure YouTube channel `UCDvErgK0j5ur3aLgn6U-LqQ` and Slack channel `C01BQC114P2`.
- [x] Configure display name `YouTube`, unfurling/name linking enabled, reply broadcast disabled.
- [x] Verify Slack channel access and `chat:write.customize` permission through read-only provider requests.
- [ ] Confirm original exact message spacing; exported literal spacing unavailable.
- [x] Verify selected spacing/username through signed real-callback ingress and provider receipt; WebSub is the documented adjustment.
- [x] Post exactly one synthetic message; verify channel, title, URL and link preview against Slack history.
- [x] Verify a synthetic message displays `YouTube` in the explicitly authorized `#integration_test` channel; production destination unchanged.
- [x] Verify signed synthetic WebSub initial delivery/retry produces exactly one Slack action in #integration_test.
- [ ] Verify a future genuine new upload through publisher/hub delivery; requires a legitimate owner upload. Latest upload predates the test window; no public upload was created for testing.

Provider checks (2026-10-02): Slack token verification passed for DataTalks.Club (`T01ATQK62F8`); posting and preview were verified by the CLI fallback below; custom-name verification remains failed. YouTube live identity verification passed for DataTalksClub (`UCDvErgK0j5ur3aLgn6U-LqQ`) using the stored readonly connection. Chrome renewal outcome is unconfirmed: the UI still requests reconnection and no recent callback audit record was returned. No account binding was changed.

Slack action evidence: Chrome strict dry-run passed, but its confirmation dialog timed out. Audit showed only the 19:46 UTC dry-run, and a direct channel-history read found no browser synthetic message. The authorized CLI fallback posted exactly one `SYNTHETIC TEST — Dapier CLI Slack 2026-10-02 01` message to `C01BQC114P2`, Slack timestamp `1790970539.954079`. Independent channel-history reads confirmed the text, channel URL and a YouTube preview attachment. The custom display-name check failed: no message `username` override was present and bot profile name remained `Au-Tomator`. No retry was sent. Alexey subsequently deleted the test message; the collected provider evidence and browser screenshot remain the historical evidence. No further public-channel posts are authorized for these tests. Slack response headers show `chat:write` but no `chat:write.customize`, explaining the missing display-name override. Reinstalling the Slack app with that permission and renewing the stored token is the required user action; do not send another public test message to verify it. The migrated action was tested independently; its managed draft remains unpublished and disabled, while the earlier live workflow remains On.

## 6. Dropbox invoice landing

Source: Zap `155120966`; draft: `dropbox_on_upload`.

- [x] Configure `/_dtc_paperwork/invoices-landing`, every 2 minutes, all file types.
- [x] Configure processing-date prefix, original extension once, move to `/_dtc_paperwork/invoices`, then DataOps intake.
- [x] Verify Dropbox access to both folders and accepted DataOps intake via the isolated test.
- [ ] Confirm original processing-date timezone; source metadata unavailable.
- [x] Separately verify intended UTC processing date and midnight conversion.
- [ ] Confirm original adapter collision/already-prefixed defaults where not exported.
- [x] Separately verify intended rename/move conflicts and already-prefixed/multi-extension byte/name preservation.
- [x] Upload one test-only file; verify exact destination name, matching bytes, removal from landing and DataOps receipt using the isolated worker/CLI fallback.
- [x] Verify required final bookkeeping publication/output for Dropbox intake through the actual DataOps worker/provider factory: canonical Gmail/Dropbox source dedupe, isolated live Sheet/Dropbox readbacks and retries, local ledger only. Production ledger publication was not performed.
- [x] Verify Dropbox/Gmail canonical draft dedupe and safe review/reject publication gate separately; no ledger publication.
- [x] Verify a fresh synthetic file through an isolated automatic two-minute poll and one complete run.
- [x] Verify isolated pause/resume cursor retention and exact workflow rollback; concrete non-overlap cutover/rollback plan prepared.
- [ ] Agree on production cutover and replace the legacy live consumer before enabling the production landing poll. Governance remains unperformed: landing poll disabled and legacy consumer unchanged.

Dropbox landing fallback (2026-10-02): uploaded only `dapier-synthetic-invoice-landing-20261002-01.pdf` using the same 1,855-byte fixture. An isolated worker workflow restricted to that exact test path performed both moves, ending at `/_dtc_paperwork/invoices/2026-10-02-dapier-synthetic-invoice-landing-20261002-01.pdf`. Provider metadata confirmed both original and intermediate landing paths absent; a direct archived download matched the original SHA256 above. DataOps initially returned HTTP 400 because Dapier used unsupported route `dropbox-upload` and document kind `dropbox-file`. The adapter fix uses a configurable `recipient_route` (default `invoice`) and supported kind `attachment`. After deployment, an intake-only API/CLI execution against the already archived path returned `accepted`, item `email-12d01cd72b823cd547ceaf5bc4de4e05`, artifact `email-document-303c85e3150060b66c543afe1eb0f191`, status `needs-review`. No rename/move was replayed. The actual production landing workflow and disabled poll were retained; automatic two-minute polling is not claimed as tested.

Automatic polling test (2026-10-02): inventory confirmed production `invoice-landing` is disabled, source `dropbox.files`, connection `dropbox`, folder `/_dtc_paperwork/invoices-landing`, expression `rate(2 minutes)`. The only live Dropbox consumer, `dropbox_on_upload`, still monitors `/_dtc_paperwork/income-invoices/` and performs intake/delete; its pending migration draft is not live. There was no overlapping live consumer for the landing path. Created temporary poll `invoice-landing-test-20261002` on the intended landing folder, every two minutes, and a workflow filtered to both that poll ID and the exact new synthetic path. Waited for seed cursor `0000-01-01T00:00:00Z` before upload. New file `dapier-synthetic-poll-20261002-202635.pdf` was uploaded at 20:27:27 UTC, file ID `id:nn8KyArhBWgAAAAAAACGLw`. Automatic run `invoice-landing-poll-test:invoice-landing-test-20261002-f59ab3ebaad6be3d` completed all four actions at 20:29:06 UTC. Destination `/_dtc_paperwork/invoices/2026-10-02-dapier-synthetic-poll-20261002-202635.pdf` matched the fixture bytes/SHA256; original and intermediate landing paths were absent. DataOps accepted item `email-9464938f04aa4335fe840aec55760086`, artifact `email-document-6d42bb570e1291c7a21487e21d9c6596`, status `needs-review`. The poll cursor advanced to `2026-10-02T20:27:27Z`. At 20:32:22 UTC, after another two-minute interval, exactly one matching four-step run still existed. The Gmail subject likewise had exactly one matching completed run. Poll deletion initially returned HTTP 500 while clearing its cursor: the API role had read-only cursor-table access. The test workflow was deleted, the temporary `integration-poll-test` grant revoked, and the poll was recreated disabled so cleanup could be retried after the scoped permission fix. Production polling stayed disabled and the existing Dropbox grant was retained.

Slack permission investigation: a read-only `bots.info` request confirmed `Au-Tomator`, app `A01S395330A`, and response-header scopes lacking `chat:write.customize`. No message was sent. [Slack documents that scope](https://docs.slack.dev/reference/scopes/chat.write.customize/) for custom usernames. Existing app settings: <https://api.slack.com/apps/A01S395330A/oauth>, redirected for this workspace to <https://app.slack.com/app-settings/T01ATQK62F8/A01S395330A/oauth>. Add the Bot Token Scope, reinstall to DataTalks.Club with user consent, and replace the token on the existing Dapier Slack connection. Dapier stores a bot token and has no Slack OAuth reconnect URL. Token scopes can then be checked read-only without another public post. After reloading settings, the browser tester added only `chat:write.customize` and authorized reinstallation. A subsequent read-only `bots.info` request using the existing Dapier stored token confirmed that scope, app `A01S395330A`, and the same bot/workspace. No token was printed or requested and no replacement was needed. No new message was posted; custom-name rendering remains pending a safe future message.

Cleanup: the isolated `invoice-receipt-integration-test` hook and the two managed test workflows were deleted after runs finished. The temporary Dropbox grant for the current operator and agent `integration-test` was revoked. The existing `dataops-invoice-publication` grant remains. Existing production hooks were retained. Synthetic archived files remain as test evidence; no real invoice was overwritten or deleted.

Integration fix deployment: [CI/deploy run 37057902392](https://github.com/DataTalksClub/dapier/actions/runs/37057902392), commit `5218b08`, passed tests, designer checks and deployed before the successful intake-only verifications above. Domain bookkeeping/review remains in DataOps.

Remaining checks: production Dropbox cutover follows the migration plan and has not been activated; actual new-video delivery awaits a future event; the authorized isolated custom-name smoke passed. Slack permission readiness is verified. Native Telegram is the supported primary path; the unknown original catch-hook sender remains a compatibility limitation. Original Zapier timezone, attachment/collision and MIME semantics remain separate comparisons.

For implementation details and unresolved source semantics, see
[the migration plan](zapier-migration-plan.md#adjustments-and-remaining-source-provider-checks).


Final cleanup permission verification: deployment
[37062138765](https://github.com/DataTalksClub/dapier/actions/runs/37062138765)
(commit `5974a08`) succeeded. CLI deletion of the owned disabled
`invoice-landing-test-20261002` then succeeded, including its EventBridge rule
and cursor/retry state. The production `invoice-landing` poll remains disabled.
Permissions allow writes only to poll and hook retry-state key prefixes, and a
failed poll cleanup retains its configuration for retry. No real invoice was
moved or deleted during cleanup; synthetic archives are retained as evidence.


Isolated catch-hook retest after deployment
[37063196165](https://github.com/DataTalksClub/dapier/actions/runs/37063196165)
(commit `8936c18`, 3,761 tests and 42 subtests passed):

- Unauthenticated POST returned 401; no workflow was admitted.
- JSON request `synthetic-form-fixed-01` completed one echo-only run,
  `webhook-trigger-catch-retry-test-20261002:catch-retry-test-20261002-dd2d867fc1f01859`,
  at 20:57:30 UTC. JSON and URL-encoded form retries both returned HTTP 202
  with `duplicate: true` and the same event ID; no extra run was created.
- A form request with new ID `synthetic-form-fixed-02` completed separately,
  run suffix `catch-retry-test-20261002-0928afbed8987df2`, at 20:57:31 UTC.
  Both valid runs preserved `Date: 2026-10-02T20:40:00Z` and
  `Text: SYNTHETIC TEST Dapier form-fixed retry 2026-10-02` exactly.
- Lowercase `date`/`text` was acknowledged but intentionally failed the
  case-sensitive contract check. HTTP 202 means accepted intake, not business
  completion. These negative test events had no external-write actions.
- Two identical requests without `request_id` completed two separate runs,
  suffixes `02d365b6-a2e0-47e9-9f3a-2f938e18d18b` and
  `cc4836a3-200f-4ba5-8bf7-98f00263a610`. Missing IDs do not deduplicate.

The first live form test exposed raw-body fallback instead of parsed fields;
that failure was fixed in shared webhook intake without changing JSON behavior.
Tests cover cross-encoding identity, case, blank fields, encoded characters and
repeated keys. All tests used an isolated echo-only hook, not the production
TODO workbook. The original sender is unknown: native Telegram remains primary,
and the existing `telegram-todo` compatibility hook/configuration is unchanged.

The isolated `catch-retry-test-20261002` hook was then deleted successfully,
including its retry state; the owner-only local bearer file was removed.
Final inventory contains only existing `automator-telegram`, `telegram-todo`
and `todo` hooks; polls are the disabled production `invoice-landing` and enabled
`mailchimp-drive-backup`. Only the pre-existing Dropbox grant for
`dataops-invoice-publication` remains. Test archives and run evidence are retained.
No public Slack post or additional invoice archive/move was performed.

Current outcome: mailing backup, email TODO, native Telegram text/empty-text
handling, actual Gmail invoice ingress and isolated automatic Dropbox polling
have provider evidence. Catch-hook compatibility and retry mechanics were
verified synthetically. Slack custom-name permission is present; custom-name rendering passed in the authorized test channel, while the actual
new-video trigger remains pending. Production Dropbox migration
and source-semantic comparisons remain unchecked above. DataOps accepted the
invoice receipts as `needs-review`; bookkeeping completion is not claimed.


Authorized Slack display-name smoke (2026-10-02): read-only provider calls
resolved `#integration_test` to `C01S3EGKMJP`, confirmed the existing bot is a
member and the channel is active. The tester used an unpublished, unsaved
workflow with the same YouTube Slack action and `username: YouTube`, changing
only the synthetic workflow identity and channel destination. Strict dry-run
passed. Chrome's Run for real confirmation timed out during focus emulation;
the browser did not provide an execution result. Before fallback, provider
history contained zero matches for the exact unique title and the run query
was empty. The initial small-page audit query missed entries; a subsequent
200-row scan recovered the test entries, so that initial empty audit result
is not treated as proof of no browser execution.

One explicitly authorized CLI fallback returned Slack timestamp
`1790975250.233089`. Repeated read-only history checks found exactly one message
with title `SYNTHETIC TEST Dapier YouTube display name 2026-10-02 02`, username
`YouTube`, bot `B01S9G1UZJQ`, app `A01S395330A`, channel `C01S3EGKMJP`.
[Provider permalink](https://datatalks-club.slack.com/archives/C01S3EGKMJP/p1790975250233089).
Chrome also confirmed the visible YouTube sender, title, link and attachment.
The audit action in this deployment is `workflow.test` (not `designer_test`);
its 21:07:30 UTC entry matches the CLI provider receipt. Earlier 21:04:05 and
21:05:45 entries correspond to dry-run preparation; audit rows do not expose
request surface or execute mode, so CLI attribution rests on the exact returned
provider timestamp as well as audit timing. No additional send occurred.
Production `youtube-slack.yaml` still targets `C01BQC114P2`; no workflow was saved
or published for the test, and no test grant or managed resource was created.
Actual new-video-trigger verification remains pending.

## 7. Selected invoice email body → PDF

Original inactive source: Zap `153699998`; selected disabled migration definitions
`invoice-body-render` and `invoice-body-completion`. No exported source steps
exist for this inactive Zap, so this verifies documented intended behavior.

- [x] Send actual synthetic no-attachment rich-text Gmail email to isolated route.
- [x] Verify real renderer completion and exact archived PDF bytes/marker.
- [x] Forward the actual completed artifact through CLI intake-only action; DataOps accepted/needs-review receipt verified.
- [x] Preserve distinction between isolated automatic archive and explicit DataOps forwarding; no production renderer migration activated.
- [x] Restore original broad completion route and remove owned test consumers.
- [x] Validate disabled paired renderer migration definitions and non-overlap cutover plan.

See the completion report for ingress/run IDs, 19,007-byte checksum, artifact ID,
review audit, deployment evidence, cleanup and exact external dependency.

## Isolated live DataOps publication — 2026-10-03

Actual `confirmInvoice` / `publishInvoice` used `officialProviders(testConfig)`
and DynamoDB Local. The new private spreadsheet
`1Wso806eRDQY9w9-jMITx6AcpLTGwn1KlMfgvZ8JpEW8`, tab `Sheet1`, contained
exactly two data rows (2–3); Chrome independently confirmed them and blank row 4.
The canonical Gmail/Dropbox 1,855-byte artifact and rendered Gmail 19,007-byte
artifact produced exact-byte Dropbox archives under the owned publication root.
Lost responses after successful writes, repeated publication and the Dropbox
source alias caused no duplicate rows or file writes. Local ledger references
were verified; no production ledger/configuration was touched. Test payment
attestation was explicitly synthetic and existed only in the local test state.

Broker grants limited connection/agent/use/expiry, not resources. A local guard
validated every request before network access, enforced the exact test sheet
and Dropbox root descendants, and disabled redirects. Two forbidden-resource
requests were rejected locally. Temporary grants/token and DynamoDB container
were removed after readbacks; the publication Dropbox root was removed.
Spreadsheet recoverable-trash provider confirmation is recorded in the report.
