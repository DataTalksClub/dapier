# Changelog

## Unreleased

The provider list collapses to `aws` alone: the `s3` provider alias, the
`s3`→`aws` test alias, and the `s3` pseudo-connection id are gone (use
`aws` for `connections discover|test`). `s3` survives only as the service
surface name — the discovery source (`s3.buckets`, `s3.objects`), the
trigger chip, and the `s3` / `s3.updates` / `s3.deletions` poll sources —
matching every other plugin's provider-vs-source split (provider `google`,
sources `google-drive`, `google-sheets`, ...). No stored data used the
alias: no live poll, trigger, or connection referenced `s3` as a provider.

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
