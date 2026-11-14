# Changelog

## Unreleased

- `hub_session` retries Hub create/assign with backoff (5s, 15s, 45s) on
  409 `machine_capacity`, 429, 5xx and transport timeouts, replaying the
  same keys. When retries run out the step fails with Hub's error code and
  short message, so the run's failure notice emails the notify list
  (intake b7701017).
- Retire the create-only `hub_intake` action: no workflow uses it;
  `hub_session` is the Hub path for workflows.

## 0.1.0

- Register `hub_session` in the shared action catalog with required acceptance
  criteria and workspace/tier routing.
- Read the service bearer credential exclusively through the DataOps team
  secret-store reader.
- Persist create/assign payloads before HTTP mutations; recover identical
  requests and results after ambiguous failures.
