# Missing Hub credential and recording failure alert routing

Intakes: t3012u (`2b9a07b5-9f54-4712-9e31-c4453f651c5b`) and
t3013u (`53495edf-b0d9-4669-8926-5fab0763073d`).

## Failure and credential boundary

Live run `aisl-recording-agent:zoom:d9640eb73fcb8a78f15116eec3fba75e`
failed in `classify-recording.one-on-one.process-call` (`hub_session`),
inside the top-level `classify-recording` decision. The selected branch was
`one-on-one`, not `ai-shipping-labs`. The transcript filter stopped the
only file iteration, so this run did not upload a transcript.
Live workflow storage contains its immutable Hub request snapshot but returns
404 for both assignment and assigned-session receipts.

The error was `Secret does not exist or has no ciphertext: dapier.hub`.
The DataOps secrets inventory on 2026-11-11 confirms the name is absent.
`dapier.admin` and `dapier.dataops` exist but are unrelated credentials.
No secret values were read or copied for this investigation.

`template.yaml` defaults `HubIntakeTokenSecret` to `dapier.hub` and injects
it as `HUB_INTAKE_TOKEN_SECRET_NAME`. `plugins/hub/action.py:service_token`
reads that configured name using `src/dapier/secrets.py:value`.
`src/dapier/dataops_secrets.py:SecretReader.read` raises this error when the
team-store record has no ciphertext, before KMS decryption. The secret name
is therefore absent from the workflow YAML by design. The plugin README
already records service identity provisioning as outstanding; migration to
`hub_session` went live without satisfying that dependency.

An operator must supply the intended scoped Hub service bearer, with intake
write on project `dapier-agents` and session launch authority for the intended
workspaces. Store it under DataOps secret `dapier.hub`, field `value`, through
the existing secrets API/CLI. The value may be the raw bearer or JSON containing
`token`/`access_token`. Use protected input, never a command-line literal or
logs. Do not mint privileges, substitute another service secret, or add a token
fallback. This investigation cannot finish credential repair without that
operator-held credential. No code change can safely manufacture it.

## Alert delivery is a separate defect

The failed-run email at 2026-11-11 12:50:22 CET went from
`DTC Alerts <alerts@dtcdev.click>` to `alexey@datatalks.club` only,
according to the supplied Gmail delivery evidence (message
`1a1ca71e0bed668b`). It did not arrive in the Developer/oncall path.
This is distinct from the earlier EventQueueAge alert.

Live workflow revision 9 had `notify: [alexey@datatalks.club]`, matching the
repository definition. Change only failure `notify` to `[dev@dtcdev.click]`
so future failures use the existing dev-mailbox Developer/oncall route.
The live dev-mailbox workflow is enabled, and its Developer/oncall URL and
token storage keys are all populated (values were not printed).
Hub completion `notify_to` values are a separate notification policy and are
unchanged. No test mail or historical alert resend is needed.

The change is reversible by restoring the previous `notify` setting and
publishing through Dapier. Preserve any intervening workflow edits rather than
blindly rolling back the whole definition.

## Recovery constraints

The missing-credential failure occurs before Hub's create HTTP request in
`run_hub_session`, so this attempt could not launch a Hub session.
Do not delete its durable journal. The Hub request/assignment key derives
from workflow ID, event ID, and the fully qualified action ID.

Dapier's ordinary run replay creates a fresh `replay-<uuid>` event ID.
That changes the Hub deduplication key even when replay starts at
`classify-recording`. Do not treat replay as preserving launch idempotency.
After credential provisioning, first check existing Hub tasks, journal phase
receipts, and other recording/transcript deliveries for the same meeting and
recording. Recovery must preserve the original execution identity or prove
there is no competing/previous launch. The recording workflow can launch a
session with completion email and downstream writes; no replay, publication,
posting, or completion email is authorized merely as a credential smoke test.

The failed run remains failed while credential provisioning and a safe recovery
decision are outstanding. Do not mark it resolved as if processing succeeded.
