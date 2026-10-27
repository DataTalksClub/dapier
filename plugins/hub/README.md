# Hub session integration (preparation)

`session.py` implements the create/assign protocol with required rendered
acceptance criteria, headless workspace routing, and immutable request snapshots
in the existing workflow storage table. Tests inject authenticated HTTP; the
module never reads credentials or launches a local agent process.

This component is **not registered as a flow action yet**. Intake
17ae8720-c809-457a-89f1-8f82bea55af2 still requires the scoped Hub service identity,
DataOps secret reader, connector/catalog/designer wiring, attachment delivery,
completion polling/email, live migration and runtime retirement. Existing
`hub_intake` behavior remains available. No live launch readiness is claimed.

The future action runner supplies a `DynamoJournal` scoped to the workflow and
an authenticated `post(path, payload)` adapter rooted at Hub's intake tasks
endpoint. The adapter must return `(status, parsed_json)` and propagate network
timeouts. Do not log request payloads, authorization headers or response bodies.
Journal rows have no expiry: deleting them can allow duplicate launches. A
held/unknown response must remain pending and must not be called running or
successful. Attachment inputs currently fail explicitly until URL delivery is
implemented; they are never silently discarded.
