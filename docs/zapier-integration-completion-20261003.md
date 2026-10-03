# Integration completion — 2026-10-03

All executable integration checks within the authorized synthetic-only scope passed,
including isolated tests of the original Zapier adapters.
Required invoice/Dropbox publication passed through the actual DataOps worker
with isolated live providers and local ledger state. Production ledger
publication, genuine YouTube publisher delivery, production Dropbox cutover
and future upstream exporter behavior remain unverified. Acceptance criteria have not
been narrowed.
Historical failures are retained; fixes and retests below supersede them.

## Results

| Flow | Completed verification | Boundary |
| --- | --- | --- |
| Mailing-list backup | Original ZIP and fresh PDF delivery previously passed. New isolated Drive file A backed up once; replacement B retained the same ID and did not back up; new ID with the same name replaced the exact S3 key with C. Idle polling produced no extra runs. Content-Type is literal `none`. Folders are now excluded. | Future upstream exporter naming/replacement is not established. Legacy January 2025 S3 metadata confirms literal `none` MIME parity. |
| Email TODO | Existing actual Gmail retest produced exactly one correctly mapped row and DataOps receipt. Published fixed-CST source now matches live v8. Six fixed-offset midnight/DST cases and actual Gmail row110/one DataOps receipt passed. | Earlier America/Chicago DST interpretation was a parity bug, now corrected to fixed UTC−06:00. |
| Invoice attachment email | Actual Gmail ingress/archive/DataOps receipt previously passed. Multiple PDFs and inline-PNG-plus-PDF fail closed before archive/intake. Same-byte and changed-byte collisions return HTTP 409, without overwrite or autorename. | Original adapter: single PDF unchanged; multiple/inline ZIP bundling; same-byte no-op and changed-byte autorename. Dapier single selection and strict conflicts are explicit adjustments. |
| Telegram TODO | Existing actual native text/confirmation, empty-text guard, isolated JSON/form authentication and stable-ID retry tests passed. Mapping unchanged; private audio was never opened or replayed. | Native Telegram is primary. Unknown original catch-hook sender is compatibility-only; sender discovery/cutover is conditional, outside acceptance. |
| YouTube → Slack | Real HTTP callback challenge/signature/XML/identity validation passed. Signed synthetic Atom initial delivery and retry produced exactly one completed action and one provider message, username YouTube, in #integration_test. | Synthetic callback is not a genuine upload or publisher-to-hub delivery. Future genuine-event dependency below. |
| Dropbox landing → archive | Prior automatic isolated polling passed. Normal, already-prefixed and multi-extension names preserve all bytes and extensions. Rename and archive collisions return 409, retain source files, and preserve destination revisions. UTC processing-date behavior passed. | Production landing poll remains disabled; existing legacy consumer remains unchanged. |
| Selected invoice body → PDF | Actual no-attachment HTML Gmail email completed rendering; archived PDF contains the heading/marker and matches completion checksum. Intake-only forwarding of that actual artifact returned accepted/needs-review. Disabled paired migration definitions validate. | DataOps forwarding was an explicit CLI action using the invoice-pdf contract, after isolated archive verification; it was not an automatic production completion consumer. Original disabled Zap153699998 source UI now confirms Body Html→ConvertAPI PDF→UTC Dropbox archive→Sheets. Native rendering and DataOps ownership are documented adjustments. |

## Provider evidence

### Drive and S3

Owned folder: `1LB_AVp5eksOq_LYQ3ePvbx3DXrQ_goys`.
All runs below belong to `integration-drive-backup-20261003`.

| Case | Drive ID / automatic run suffix | Result |
| --- | --- | --- |
| A create | `1x0xkQbODWdlmy4aKMhVEq0SMaw5dq9h6`; `integration-drive-poll-20261003-edbc95ec5f037ecb`, 04:05:06 UTC | 43 bytes, SHA256 `7ba5c904cb31fb90d31297eb59c2c32a4de5fb3884757260ab337a60e954353c`. |
| B replace | Same ID, creation 04:05:01.562Z unchanged, modification 04:07:35.730Z | 48 bytes matching B, SHA256 `eabe06cd45755e9b0542b9233f67baa47d77e0791c958d76f822b26870e346bf`. No creation run; S3 remained A with unchanged ETag. |
| First C | `1AY95wcq7FOYZQ31UYyeXO2_Kq7M46j2M`; `integration-drive-poll-20261003-5e9591ee75daf2a4`, 04:11:06 UTC | Poll caught Chrome's temporary `(1)` suffix before rename. This case is not the same-key overwrite proof. |
| Controlled C | `13VpCvEEmSyJWZ1peRvKr_1TuxvAqvir4`; `integration-drive-poll-20261003-3fefb596578ee910`, 04:15:43 UTC | Poll paused for upload/rename, resumed with retained cursor 04:10:54.928Z. New ID produced one run; exact key `integration-20261003/dapier-drive-repeat-20261003.txt` contains C's 60 bytes, SHA256 `c48f2010473e736d6824ed95685f196d788c23fb1dbbecee7dfdf678ee0ac73d`, ETag `ab228d75d86a89dbf2e54227fdc0c6f4`. |

Both exact-key signed GETs returned Content-Type `none`. Final count remained
three automatic runs across idle cycles: A, temporary-suffix C, controlled C.
The provider-side copy fallback failed before creating a file. No real export
was modified. After poll/workflow removal, Chrome moved the owned folder to
recoverable trash; a provider GET returned HTTP 200 and `trashed: true`.

### Attachment selection and conflicts

Actual ingress IDs:

- MULTIPLE: `7084212c2c039a3287016ce33c9469c8cf7d1fdda4d8bc5fe6e9335a9cd396ce`.
- INLINE: `db6182b5acba16d5029bf92a2ec129453fe9bb22fb326c62bcf67634a686d0ee`.
- First collision: `b18d99df557bfb5f31cdb06f4c96ccad8666d1dd77dfc441a4ba520390642da7`.
- Second same-byte email: `3ea8e2a3bbd64d670fcbef070dd60661b06f2505b08eb1d8ac80f7385c5c70f4`.
- Changed-byte email: `a84758760276a68a62f4994451d7cdd163a368159a331f23d10fbf4c12da2e8f`.

MULTIPLE had two ordinary 1,855-byte attachments. INLINE normalized the 68-byte
PNG as `inline`, content ID `ii_mururjiw0`, and the 1,855-byte PDF as
`attachment`, content ID `f_mururzyh1`. Both were ambiguous under explicit
single selection. Provider searches found zero matching archives; isolated
workflows contained no DataOps action. Negative failures were subsequently
absorbed with `on_fail: continue` to prevent queue retries.

The second email initially demonstrated Dropbox's identical-byte successful
no-op, not strict rejection. `strict_conflict: true` was added and tested. The
changed-byte Gmail attachment was 1,497 bytes, SHA256
`36dd080af300b32df9fc0ac95069713bdfeb37ba0474ae5ff042dd71a7fccac5`.
Its archive step was skipped. After fixing live HTTP error reporting, API/CLI
archive-only attempts using both actual stored attachment references returned
`dropbox upload returned HTTP 409 (path)`. Neither re-sent email nor invoked
DataOps. Exactly one archive remained, revision `65ce78d0cd9e702505a9a`, server
modification 03:50:33Z, 1,855 bytes and SHA256
`a81cc4eced5b087376cd1ccc8da187b55bc72cf063bf84471d9be647769e7388`.

Dropbox landing cases archived normal `.pdf`, already-prefixed `.pdf`, and
`.tar.gz` names with those exact bytes. The already-prefixed result was
`2026-10-03-2026-10-02-synthetic-prefixed-20261003.pdf`: the literal source mapping
adds today's prefix once per new event; it does not remove an existing prefix.
Both move and rename collision retests returned HTTP 409 `(to/conflict)`.
Sources remained present and destination revisions unchanged. No DataOps action
was included in these edge-case tests.

### Time and DataOps lifecycle

Executed date-time cases covered UTC email date crossing midnight, Chicago
summer/winter midnight, March 8 spring-forward and November 1 fall-back on
both sides of the transition, plus current UTC processing date. Intended
choices remain UTC for invoice dates/prefixes. Original TODO source UI now
confirms fixed CST (UTC−06:00); the earlier Chicago tests are superseded by
the fixed-offset correction and retest, not evidence of source parity.

Chrome Finance evidence for canonical draft
`c4cfb2964c1e15da72f5c7005633043e46976e8a2c479bf86725aae124fdb264`:
Gmail intake `email-98789095e7d530f15a054227e36e428d` and reprocessed Dropbox
intake `email-9464938f04aa4335fe840aec55760086` linked the same canonical artifact
`email-document-4d519a4bce17ab855060e39e17417b9e` and one pending manual draft.
Reprocessing did not create another draft or increment Revision1.

Comment `SYNTHETIC TEST Dapier draft lifecycle 2026-10-03` saved with verification
unchecked: corrected at 03:57:43.398Z, Revision2. Rejection at 03:57:58.385Z
retained Revision2. Review audit also retained the staged entry at
2026-10-02T19:54:45.086Z, Revision1. Final state: rejected, publication
not-confirmed, Dropbox and Spreadsheet pending. Handler review confirmed ordinary
save and reject only persist review state/audit; they invoke no publication
operation. No synthetic ledger publication or real invoice mutation occurred.
This verifies the selected review gate, not final ledger publication.

After OAuth refresh fixes, Chrome reported all five publication checks Ready:
source-storage, google-broker-account-scopes, dropbox-broker-account-scopes,
sheet-headers, dropbox-folder. Existing grants and account mappings were retained;
no expanded OAuth consent was needed.

### Actual HTML rendering

Subject: `Dapier synthetic HTML render 2026-10-03 01`.
Actual email ID: `b0b9a9d92a344eb01bff6adffe24ef62b260f7a5d82d2c52a1e5cab0744d956b`.
Incoming render run completed 04:02:27 UTC; completion archive run completed
04:03:25–04:03:27 UTC. No attachments or inline pictures were present.

The one-page archived PDF is 19,007 bytes, SHA256
`8c99ac31eb90d67f1e53cbbc4cb5bddc8b5f84cd5eb90ee119d7a60bb4f97bdc`, matching the
completion event. Extracted text includes `Synthetic renderer integration test`
and `DAPIER-RENDER-20261003-01`. Intake-only CLI forwarding of this exact artifact,
using the accepted invoice-pdf contract, returned accepted item
`email-514ed914849d1a9fa2aac1eb2539776d`, artifact
`email-document-80ca8bbc2ee6f53e8f02358288200c3d`, needs-review. This second artifact
was not processed or published. The Finance UI has no received-artifact read-only
lookup; the intake receipt is the available provider evidence.

### Real callback and Slack

Real `/hooks/youtube` requests: challenge HTTP 200 with exact response, missing
and bad signatures HTTP 401, signed malformed XML and missing identity HTTP 400,
valid signed Atom initial request and retry HTTP 202. Signing used the existing
server-held secret; no credential or signature was printed.

Stable event `youtube:DapierT2603` produced exactly one completed run
`integration-websub-20261003:youtube:DapierT2603`, 04:20:39–04:20:40 UTC, and one
provider message. Title: `SYNTHETIC TEST Dapier WebSub ingress 2026-10-03 01`.
Username: YouTube. Channel: C01S3EGKMJP. Timestamp: `1791001240.108009`.
[Provider permalink](https://datatalks-club.slack.com/archives/C01S3EGKMJP/p1791001240108009).
Chrome independently confirmed rendering and exactly one visible message. Final
provider history count remained one after retry/cleanup.

Production YouTube v2 still targets C01BQC114P2. It adds username YouTube,
link_names true, reply_broadcast false, unfurl_media true; existing text and
unfurl_links are preserved. Publishing sends no message. The test workflow
matched only a synthetic channel namespace plus the exact synthetic video ID;
production's real channel filter did not match. No synthetic announcement was
sent to production.

Read-only YouTube refresh verifies channel UCDvErgK0j5ur3aLgn6U-LqQ. Its latest
upload is `aSI_dxt8KhY`, published 2026-09-30T04:44:20Z; no genuine ingress was
recorded in the test window. Live workflow names that channel, no YouTube poll
is configured. The old renewal interval was five days; the repair uses four
days to leave margin against the observed five-day lease.
The authenticated read-only lease diagnostic uses the existing server-held
secret and returns only state/expiry/topic/callback. Its final provider result
is recorded in the completion handoff; the earlier unauthenticated 400 is not
an external blocker.

The authenticated diagnostic initially returned `expired`. An idempotent renewal
of the existing watched channel used the exact deployed callback/topic/current
secret and was accepted with HTTP 202; hub state became `verified`. The hub's
human-formatted expiry required RFC 2822 parsing; regression tests preserve
`active: null` when expiry is unknown. Authenticated provider readback confirmed `active: true`, state `verified`,
expiry `2026-10-08T05:01:07+00:00`, with the existing topic and callback.
The sanitized handoff proof records this readback.

The renewal Lambda had only Secrets Manager access even though `all_workflows()`
reads four DynamoDB tables. This confirmed stack configuration defect is repaired
with read-only access to those exact existing workflow/trigger tables and an
explicitly enabled four-day schedule. It is a plausible expiry cause; historical
scheduled failures are not claimed without log evidence. Local AWS inspection
was blocked with `AWS Gate is closed or denied: HTTP 403`. Post-deploy CI uses
existing authority to inspect physical rule/target and bounded recent failure
logs. Any read denial remains an evidence limitation; no diagnostic IAM
permissions were added and successful automatic scheduled invocation is not
claimed from declaration alone. The final CI/handoff records actual read results.

**External dependency:** a future legitimate upload by the channel owner is
needed to verify genuine publisher → hub → Dapier delivery. No
public upload was made for testing. The signed callback test proves downstream
intake, authentication, validation, routing, action and retry behavior only.

## Remaining required acceptance and dependencies

- **Invoice and Dropbox publication: passed in isolated integration.** Actual
  worker, live test Sheet/Dropbox, local ledger and dedupe/readbacks passed.
  **Production ledger publication remains unperformed**; no real payment or
  production financial publication is claimed.
- **Genuine YouTube upload: unchecked.** Requires the next legitimate owner
  upload; synthetic callback delivery does not satisfy publisher-to-hub proof.
- **Production Dropbox cutover: unchecked.** Concrete rollback and cursor tests
  passed, but user/deployment governance must authorize and perform the planned
  non-overlap transition; legacy consumer unchanged and landing poll disabled.
- **Future exporter behavior: unknown.** Owned-repo read-only searches found
  imports/tests/docs and the assumed-role policy, not the upstream exporter.
  Future create/replace/naming requires owner evidence. Source CST, UTC dates
  and prefix, Slack options, renderer steps, legacy MIME and original adapter
  hydration/collisions are resolved; stricter Dapier behavior is explicit.

### Isolated actual-worker live-provider publication

The existing DataOps factory supported the safe test without DataOps source
edits or production configuration changes. `confirmInvoice` / `publishInvoice`
used `officialProviders(testConfig)`, DynamoDB Local and checksum-verified fixture
storage. The private test spreadsheet
`1Wso806eRDQY9w9-jMITx6AcpLTGwn1KlMfgvZ8JpEW8`, sole tab `Sheet1`, had exact
headers A1:M1 and exactly two data rows. Chrome independently verified rows 2–3
and blank row 4; screenshot `localoutputs/dataops-isolated-publication-20261003.png`.

| Source | Invoice identity / row | Live archive readback |
| --- | --- | --- |
| Original actual Gmail PDF plus Dropbox source alias | `c4cfb2964c1e15da72f5c7005633043e46976e8a2c479bf86725aae124fdb264`, row 2 | 1,855 bytes, SHA256 `a81cc4eced5b087376cd1ccc8da187b55bc72cf063bf84471d9be647769e7388`, Dropbox `id:nn8KyArhBWgAAAAAAACGQQ`. |
| Actual rendered Gmail PDF | `93f90a71fdf01fbf408b993f101d11729aa68e85fa5fc2fc7d417bfd11473be9`, row 3 | 19,007 bytes, SHA256 `8c99ac31eb90d67f1e53cbbc4cb5bddc8b5f84cd5eb90ee119d7a60bb4f97bdc`, Dropbox `id:nn8KyArhBWgAAAAAAACGQg`. |

Both rows contained synthetic descriptions, EUR -10.25, Count 1, expense and
`NO REAL LEDGER OR PAYMENT`. Test payment attestation was explicitly synthetic
and confined to local invoice/ledger state. Both publication statuses completed;
local ledger references verified both destinations. Production ledger/state
and the existing rejected production synthetic draft were untouched.

The harness deliberately lost the first successful Dropbox and Sheet write
responses. Retry reconciled real provider readbacks; another retry and the
Dropbox source alias produced no additional writes. Total: two successful file
writes, two successful row writes, exactly two files and two data rows.
The source alias was staged from the previously verified Dropbox/Gmail bytes
in local intake state; no production intake or ledger was republished.

Broker grants constrained agent, connection, use and 30-minute expiry; **they did
not enforce resource limits**. As explicitly approved, a local request guard
ran before every provider/broker network request, allowed only the exact test
spreadsheet and owned Dropbox root descendants, rejected traversal and unknown
endpoints, and disabled redirects. Two forbidden-resource probes were denied
without network access. No new OAuth scope or production configuration change
was made. The temporary machine token stayed in process memory/stdin, never in
a file/log. After readbacks both grants and the token were revoked/removed, and
the DynamoDB Local container was removed. The owned Dropbox publication root
was inventoried through all provider pages (two files) and removed.

Root independently ran `npm run test:invoice-publication` in the DataOps backend:
all 13 DynamoDB Local transaction tests passed in 8 seconds and the Docker
cleanup trap ran. Coverage included publication gates, stale/unauthorized/no
side effects, concurrent claims, lost-response reconciliation/dedupe, checksum
and readback conflicts, independent partial destinations, rejected/missing-EUR
no effects and reviewed bank-payment verification. Providers in that existing
harness are simulated: this is local transaction evidence, **not live Sheet or
Dropbox publication** and does not complete either required publication box.

## Deployment, governance and cleanup

Focused fixes: preserve granted OAuth scope on omitted refresh responses;
refresh generic HTTP OAuth authentication; expose strict Dropbox conflicts;
exclude Drive folders; support server-signed raw Atom; validate entire Atom
feed before publishing; preserve live Dropbox HTTP error statuses. Production
invoice v6 uses strict conflicts. Mailing backup v3 rejects folder events,
including a queued folder event from test-folder creation. The historical
failed folder run remains recorded; it was not reclassified as success.

[OAuth deployment 37095522623](https://github.com/DataTalksClub/dapier/actions/runs/37095522623)
passed after 3,765 tests. [Integration deployment 37095848215](https://github.com/DataTalksClub/dapier/actions/runs/37095848215)
passed after 3,771 tests. [Conflict-reporting deployment 37096342537](https://github.com/DataTalksClub/dapier/actions/runs/37096342537)
passed after 3,772 tests. UI catalog bundles and shared API/CLI action paths were
updated together. [Lease diagnostic/disabled renderer draft deployment
37097363754](https://github.com/DataTalksClub/dapier/actions/runs/37097363754)
passed with 3,776 tests. [Watched-channel renewal deployment
37098175001](https://github.com/DataTalksClub/dapier/actions/runs/37098175001)
passed with 3,778 tests. Final expiry parsing and documentation deployment are
recorded in the completion handoff file after CI finishes.

Isolated cutover simulation: enabled baseline v1 → disabled v2 → rollback to v1
published as v3. Readback equalled the baseline exactly. Pausing/resuming the
owned Drive poll preserved its cursor. Production Dropbox definition remained
byte-for-byte equivalent as JSON and invoice-landing stayed disabled. The
migration plan specifies pausing/draining the legacy consumer, seeding the new
cursor, inspecting backlog explicitly, enabling one consumer, and reversing
that order on rollback. Production activation is governance, not a passed test.

All six owned workflows and the owned poll/rule were removed. Original renderer
completion was restored exactly; both isolated recipients disappeared from
routing. The test channel message, synthetic S3 objects, intake receipts, rejected draft,
local PDFs and sanitized evidence are retained for audit. After exact-byte
checks, all 13 entries under the owned Dropbox fixture root
`/dapier-integration-tests/completion-20261003` were inventoried through all
provider pages and removed. Original October 2 invoice archives were untouched.
S3 objects under `integration-20261003/` remain as documented evidence; no IAM
permission was widened for deletion. The publication test spreadsheet was
recoverably trashed in Chrome; Drive metadata independently confirmed
`trashed: true` for its exact ID. No persistent grants or secrets were added. The short-lived publication
token/grants were removed after readbacks; final inventory retains existing
DataOps publication grants and contains no integration-test grant. Earlier owned hooks/grants were already removed and were not recreated.

API deletion reported Git sync unavailable, but these test workflow definitions
never existed in repository workflows; inventory verified their removal and
there is no source file for a deploy to restore. The recoverable Drive-trash
provider read is recorded above. No original export, invoice, bot mapping,
production Dropbox consumer or production Slack destination was replaced.

## Audit of remaining unchecked boxes

Source UI review resolved CST, UTC invoice dates, UTC Dropbox processing time,
unconditional prefix/extension handling, Slack spacing/options and body-PDF
steps. Legacy S3 metadata independently resolved MIME wire parity. Only these
three checklist items remain unchecked. Original adapter isolation is complete;
no remaining executable synthetic check is omitted.

| Unchecked criterion | Concrete boundary / dependency |
| --- | --- |
| Future exporter creates/replaces/repeats names | Requires upstream exporter definition or future owner export evidence; historical matching metadata does not prove future behavior. Isolated create, replace and repeated-name polling passed. |
| Genuine YouTube upload delivery | Requires the next legitimate owner upload; authenticated synthetic callback/retry passed without public upload. |
| Production Dropbox cutover | Prepared non-overlap/rollback plan requires an authorized production migration window; existing consumer unchanged, new production poll disabled. |

Production financial ledger publication was not performed. Required integration
publication checks were completed in the explicitly authorized isolated live
provider/local-ledger setup; no real payment attestation is implied.

## Published-source UI reconciliation

Chrome inspected original published/disabled definitions read-only; source Zaps
were never edited, run or activated. Mailing v1 confirms two-minute legacy
Drive polling and exact title/file/S3 settings, including literal MIME `none`;
metadata-only HEAD of the matching January 2025 archive independently confirms literal `none` wire Content-Type. Invoice v1
confirms UTC Raw Date formatting, archive path, singular Attachment input,
overwrite No and Output-Subject.pdf naming.

TODO v1 uses a fixed-CST system variable. [Zapier documents CST and CDT as
separate fixed offsets](https://help.zapier.com/hc/en-us/articles/35720226565773-Create-reusable-variables-to-use-in-Zap-workflows).
The migration's Chicago/DST interpretation was corrected; live v8 preserves
processing time/ISO and changes only email timezone. Exactly one actual Gmail
row110 contains `2026-10-02T23:36:39-06:00`, correct sender/task, blank Notes/NEW;
receipt `email-afded997578052334528c1a4f444d63c` was accepted once.

Dropbox v1 formats Python processing time as YYYY-MM-DD, then unconditionally
prefixes Date-File Name, retains the extension and moves Path Display to the
invoice archive. [Zapier documents UTC for Python Code actions](https://help.zapier.com/hc/en-us/articles/8496326417549-Use-Python-code-in-Zap-workflows),
confirming UTC source parity. Isolated original Rename/Move tests also resolve collision autorename; Dapier strict rejection is an adjustment.

Slack v1 matches the live template exactly: two newlines around Title,
`Link: ` followed by Play Url, YouTube bot name, no automation link, expanded
links/name linking and no broadcast. Existing provider output matches with
Slack's native angle-bracket URL markup; no new test post was sent.

The original disabled body-PDF source now confirms Raw Date→UTC YYYY-MM-DD,
Body Html→ConvertAPI PDF (`test.html` input), File URL→Dropbox archive with
Output-Raw Subject.pdf and overwrite No, followed by Sheets. The native
renderer/disabled completion definitions preserve that intent; DataOps review
and publication replace the domain-specific Sheets step. This resolves the
previous export-only gap without claiming identical PDF-engine output.

## Final legacy MIME and cleanup proof

The existing deployed `s3_head_object` action read metadata only for
`datatalks-mailchimp-backup/2025-01-02-audience_export_d5c87e5d20.zip`:
Content-Type `none`, size 7,374,681, LastModified
`2025-01-02T16:49:29+00:00`. Matching Drive metadata for file
`17CkStUYbrzDVKSRxIrPWYn_i3olN2V1O` establishes the same name/size and
January 2025 creation. This confirms original archive MIME parity without
downloading customer content or writing any object. Proof files:
`.tmp/root-legacy-s3-head-proof.json` and `.tmp/legacy-s3-drive-lineage-proof.json`.

Final paginated cleanup audit (`.tmp/root-cleanup-inventory-proof.json`) found
zero current-run temporary workflows, polls, tokens or grants. Production
`invoice-landing` remains disabled; production YouTube destination remains
`C01BQC114P2`. Owned Drive folder and publication spreadsheet are recoverably
trashed; owned Dropbox test roots and local DynamoDB were removed. Existing
grants remain; synthetic S3 evidence and audit receipts are intentionally
retained without deletion IAM expansion. No further browser test actions are queued. Original-adapter isolation completed
below; its owned provider resources and temporary URL files are removed.

## Isolated original-adapter evidence — completed

An inactive copy of invoice Zap `382288655` retains the original Dropbox
Upload action and `Overwrite: No`; its Sheets step was removed and the target
is only `/dapier-integration-tests/zapier-adapter-20261003`. Chrome tested the
Dropbox step only; neither whole-Zap execution nor activation occurred.

Baseline A uploaded exactly 1,855 synthetic bytes, SHA256 `a81cc4eced5b087376cd1ccc8da187b55bc72cf063bf84471d9be647769e7388`,
file ID `id:nn8KyArhBWgAAAAAAACGRw`, revision `65ce9651c138202505a9a`.
One confirmed same-byte retest reported “A File was sent” and retained that
ID/revision, size, modified time and hash; provider inventory contains exactly
one destination file. Original same-byte success/no-op behavior is therefore
resolved. An earlier preview guard prevented a click and is explicitly not
counted as a test.

Dapier's strict 409 is retained as an intentional migration adjustment: a new
email colliding with an existing archive filename fails before a further intake.
Stable-ID delivery retries are handled separately by event/intake dedupe.
Changed-byte conflict and singular Attachment hydration were subsequently
verified below; opaque trigger output alone was not used to infer first-file
selection. Adapter input resources and URL files have been removed.

Changed-byte B succeeded in the original copied adapter and created
`synthetic-zapier-collision-20261003 (1).pdf`, 1,497 bytes, SHA256
`36dd080af300b32df9fc0ac95069713bdfeb37ba0474ae5ff042dd71a7fccac5`,
ID `id:nn8KyArhBWgAAAAAAACGSA`, revision `65ce9789d175802505a9a`.
Independent byte readback confirms both files and the unchanged baseline.
Original invoice conflicts are therefore resolved: same-byte success/no-op,
changed-byte autorename. Dapier deliberately uses strict 409 for both new-ingress
conflict cases; it does not claim parity for this behavior. MIME hydration results are verified below.

Original singular `Attachment` hydration of the actual synthetic two-PDF email
succeeded as a ZIP, rather than selecting the first PDF. Dropbox output adds
`.zip` to the configured `.pdf` name: `synthetic-zapier-multiple-hydration-20261003.pdf.zip`,
ID `id:nn8KyArhBWgAAAAAAACGSQ`, revision `65ce98ba715fe02505a9a`, 4,012 bytes,
SHA256 `f532b6f10c57a01d153beddf3ba609a8e0b0e7272118babe6f599246bb847ee0`.
ZIP magic and independent entry inspection confirm exactly
`dapier-attachment-A-20261003.pdf` and `dapier-attachment-B-20261003.pdf`,
each 1,855 bytes with original fixture SHA256 `a81cc4eced5b087376cd1ccc8da187b55bc72cf063bf84471d9be647769e7388`.
No customer attachment was inspected. Dapier's multiple-attachment ambiguity
rejection is an explicit adjustment to keep archive/intake document selection
unambiguous, not source parity. Single/inline controls passed as recorded below.

Single control independently verifies an unchanged PDF: 1,855 bytes, original
fixture SHA256, ID `id:nn8KyArhBWgAAAAAAACGSg`, revision
`65ce9b4ed809002505a9a`; exactly one single-control output exists.

Actual forwarded inline control hydrates as
`synthetic-zapier-inline-hydration-20261003.pdf.zip`, 2,213 bytes,
ID `id:nn8KyArhBWgAAAAAAACGSw`, revision `65ce9bf125a2f02505a9a`, ZIP SHA256
`fcaf39705cc7ab6791c93ccacae893060620fb4eb6b6f95b12324f442c24eeb9`.
Exactly two entries match local synthetic fixtures: PNG
`dapier-inline-20261003.png`, 68 bytes, SHA256
`bf0bf9ded3de6859d23a91140e903b41e0dcac7507e3fef04d2d7c60fa0707eb`;
PDF `dapier-attachment-A-20261003.pdf`, 1,855 bytes, original fixture hash.
Original email selection/collision source comparisons are resolved. Dapier
intentionally rejects multiple/inline ambiguity instead of bundling ZIPs and
rejects collisions instead of no-op/autorename. Original Dropbox rename/move collision isolation also completed below.

Inactive copy `382289809` of the original Dropbox flow tested only Rename
against the dedicated collision pair. Original Rename autorenamed source A to
`rename/synthetic-rename-target-20261003 (1).pdf`, retaining its ID
`id:nn8KyArhBWgAAAAAAACGUA` and exact 1,855-byte fixture/hash; new revision
`65ce9d70d6f6e02505a9a`. Original target B remains 1,497 bytes with unchanged
ID/revision/hash. Exactly two files remain; the old source path is absent as
expected for rename. Dapier's conflict rejection is intentionally stricter.
The separate prepared Move source/destination were untouched by Rename and
tested independently below. No full Zap or original code ran.

Original Move succeeded with autorename to
`move/destination/synthetic-move-collision-20261003 (1).pdf`, 1,855 bytes,
original A SHA256 and ID `id:nn8KyArhBWgAAAAAAACGUg`, new revision
`65ce9e2bd04ae02505a9a`. Source path is absent; existing target B retains
1,497 bytes, ID/revision/hash; exactly two destination files. Chrome and
independent provider readback agree. Original Rename/Move collision semantics
are fully resolved for the tested changed-byte conflict. Strict Dapier rejection
is an intentional adjustment that avoids silently autorenaming invoice paths.

Final adapter cleanup paginated all 17 entries (11 synthetic files), removed
only `/dapier-integration-tests/zapier-adapter-20261003`, and verified absence
through the parent provider listing. Temporary A/B URL files were removed.
Proof: `.tmp/zapier-adapter-cleanup-proof.json`. Inactive copied Zaps
`382288655` and `382289809` are owned cleanup items; original Zaps were never
edited, activated or run. Copy deletion confirmation is recorded in handoff.

Upstream exporter search across owned repositories found only audience-export
imports/tests/docs and folder references in Dapier triggers/tests. The
aws-infra Mailchimp template is an assumed-role policy, not an exporter. Future
upstream naming/create/replace remains dependent on owner evidence; local
polling semantics are tested. No public YouTube upload, production Dropbox
cutover or real financial publication was performed.
