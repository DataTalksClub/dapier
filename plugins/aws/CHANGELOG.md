# Changelog

## 1.0.0 — 2026-10-03

First release as a plugin. No behavior change: the upload/find/list/read/
presign/delete actions, bucket and object discovery, the identity health
check (registered under the `aws` connector, aliased for the `s3` provider
through the manifest's test_aliases), the trigger chip, the sample pull,
and the `s3` / `s3.updates` / `s3.deletions` poll sources registered
exactly as before — now from `plugins/aws/` with the plugin manifest
instead of the hard-coded registry maps.

`connections/aws.py` (assume-role credentials glue) stays in core — the
runner imports it, and core must not import plugin code.
