# Changelog

## 0.1.0

- Register `hub_session` in the shared action catalog with required acceptance
  criteria and workspace/tier routing.
- Read the service bearer credential exclusively through the DataOps team
  secret-store reader.
- Persist create/assign payloads before HTTP mutations; recover identical
  requests and results after ambiguous failures.
