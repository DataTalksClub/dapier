# Hub session integration

`session.py` implements the create/assign protocol with required rendered
acceptance criteria, headless workspace routing, and immutable request snapshots
in the existing workflow storage table. Tests inject authenticated HTTP; the
module never reads credentials or launches a local agent process.

`hub_session` registers through `plugin.py` in the shared catalog used by the
designer, HTTP workflow save/test/publish endpoints and `dapier workflows`.
It requires an explicit step id, title, text and nonblank rendered
`acceptance_criteria`. Optional fields: `workspace` (default `dapier-agents`),
`project` (default `dapier-agents`), `cwd` (workspace-relative), and `tier`
(`quick`, `daily`, `deep`; default `daily`). No engine selection is accepted.

Configure `HUB_INTAKE_TOKEN_SECRET_NAME` with the DataOps team-store name of
the scoped Hub service bearer token. The reader contract is
`src.dapier.secrets.value(name)`, supplied by the coordinated secret-store
cutover. A raw bearer token or JSON `token`/`access_token` value is accepted.
There is no Secrets Manager or environment-token fallback. The identity must
have intake write on project `dapier-agents` plus session launch authority.
`HUB_INTAKE_URL` defaults to Hub's intake tasks endpoint; only HTTPS is accepted,
and HTTP redirects are refused to keep the service bearer at that endpoint.
Workflow storage (`STORAGE_TABLE`) persists the retry snapshots.

Intake 17ae8720-c809-457a-89f1-8f82bea55af2 still requires service identity
provisioning, live attachment/completion proof, live migration
and runtime retirement. Existing `hub_intake` behavior remains available.
No live launch readiness is claimed before credential setup and live proof.

The action runner supplies a `DynamoJournal` scoped to the workflow and
an authenticated `post(path, payload)` adapter rooted at Hub's intake tasks
endpoint. The adapter must return `(status, parsed_json)` and propagate network
timeouts. Do not log request payloads, authorization headers or response bodies.
Journal rows have no expiry: deleting them can allow duplicate launches. A
held/unknown response must remain pending and must not be called running or
successful. Stored trigger attachments are forwarded by default; `attachments:
"off"` explicitly opts out. This select accepts only `on` and `off`.
Each file gets an HTTPS, GET-only S3 URL valid for at most one hour (temporary
credential expiry may shorten this). The private intake text and assigned brief
carry its sanitized filename and URL. Download promptly; held or delayed sessions
may outlive a URL and must report the file as unavailable. Immutable snapshots
reuse the original URLs on retries; they never silently refresh an ambiguous
create/assign request. At most ten files are accepted. Malformed file pointers
and requests exceeding Hub's text/brief byte limits fail before Hub creation,
so attachments are never silently dropped. No new attachment store is added.

Completion delivery uses the existing poll service and workflow storage. Create
one poll through Console Polls or `dapier polls save` with name
`hub-session-completions`, source `hub.sessions`, expression `rate(1 minute)`.
It emits connector `hub`, event `session.finished`, with task/session/link,
original workflow/event/step ids, canonical session state, correlated Hub report,
and optional fleet/worker status. Session termination does not imply success.
Hub-authored reports must match the assigned fleet launch; user DONE notes are
never substituted. After a five-minute report race window measured from Hub's
terminal-state timestamp, a missing report is explicitly `unavailable`, with
worker outcome unknown. Inaccessible tasks stay pending and log a bounded error
without blocking other tasks or the round-robin cursor.

`notify_to` defaults to the triggering email sender; an explicit empty value
suppresses email. Non-email events need an explicit recipient. `notify_from`
defaults to `agents@dtcdev.click`. Recipients and the first completion snapshot
are durable before delivery. Follow-up events and SES notifications have separate
receipts, so either can retry independently. SES is at least once after an
ambiguous send or receipt-write failure: the two-minute lease limits rapid
retries but cannot prevent duplicate mail after an accepted send times out.
Sample pulls and cursor reset are read-only; only actual poll execution delivers
notifications and acknowledges events. No new queue, daemon or runtime is added.
