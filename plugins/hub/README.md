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
provisioning, attachment delivery, completion polling/email, live migration
and runtime retirement. Existing `hub_intake` behavior remains available.
No live launch readiness is claimed before credential setup and live proof.

The action runner supplies a `DynamoJournal` scoped to the workflow and
an authenticated `post(path, payload)` adapter rooted at Hub's intake tasks
endpoint. The adapter must return `(status, parsed_json)` and propagate network
timeouts. Do not log request payloads, authorization headers or response bodies.
Journal rows have no expiry: deleting them can allow duplicate launches. A
held/unknown response must remain pending and must not be called running or
successful. Attachment inputs currently fail explicitly until URL delivery is
implemented; they are never silently discarded.
