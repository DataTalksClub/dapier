# Amazon S3 connector (the `aws` credential)

S3 actions use the configured `aws` credential. Operators can select an IAM
role that Dapier assumes with its Lambda execution identity, or retain a legacy
access key pair. The names `aws` and `s3` are pseudo connections backed by the
same provider configuration; there is no OAuth client.

## Configure an IAM role

1. Deploy a role that trusts the Dapier ingress and worker execution roles.
   Give it only the bucket/object permissions needed by your workflows.
   Cross-account access requires both the target trust and Dapier's
   `sts:AssumeRole` identity permission; see
   [AWS cross-account access](https://docs.aws.amazon.com/IAM/latest/UserGuide/access_policies-cross-account-resource-access.html).
2. Include the role ARN in the Dapier stack's `AwsAssumableRoleArns` parameter.
   This supplies both the runtime permission and the API's operator allowlist.
   The default is `arn:aws:iam::387546586013:role/dapier-mailchimp-backup`.
3. In **Console → Credentials → AWS**, select **Assume IAM role** and enter
   the role ARN. Optional fields are external ID, region and comma-separated
   bucket names. The CLI accepts the same configuration through a JSON file:

   ```json
   {
     "role_arn": "arn:aws:iam::387546586013:role/dapier-mailchimp-backup",
     "region": "eu-west-1",
     "buckets": ["datatalks-mailchimp-backup"]
   }
   ```

   ```sh
   uv run dapier credentials set aws --file aws-role.json
   uv run dapier connections test aws
   ```

   The console and CLI call the shared credential API. Configuration is live
   immediately. The API rejects unapproved roles and mixed role/key fields.
   The connection test reports the effective assumed-role ARN/account; it
   does not prove permissions on a particular bucket.

Dapier requests 15-minute STS sessions and supplies the returned session token
when creating provider clients. Temporary credentials stay in memory and are
reacquired for subsequent operations. Actions, S3 polls, object discovery and
connection testing use the same resolver. `external_id` is passed to STS when
specified; the configured region defaults to the Lambda region or `eu-west-1`.

An optional `buckets` list populates the picker without requiring account-wide
`ListBuckets` permissions. Without that list, discovery asks S3 for all buckets
visible to the AWS identity. Object discovery still checks real bucket access.
`dapier connections discover aws` exposes `buckets` and `objects`; the latter
accepts `--param bucket=<name>` and an optional prefix.

## Existing access key configuration

Existing key pairs remain supported. Choose **Access keys** in the Console,
enter the access key ID and secret, or use:

```json
{"access_key_id": "AKIA…", "secret_access_key": "…"}
```

```sh
uv run dapier credentials set aws --file aws-keys.json
```

Values are write-only. A role configuration replaces the previous key pair;
Dapier does not retain keys or fall back to them if role assumption fails.

## Triggers: file events from a bucket (`s3`, `s3.updates`, `s3.deletions`)

New files surface through a poll trigger with source `s3` — Dapier lists
the bucket on the schedule machinery, no S3 event notifications to
configure:

```json
{
  "name": "backup-dropbox",
  "source": "s3",
  "expression": "rate(10 minutes)",
  "bucket": "datatalks-mailchimp-backup",
  "prefix": "reports/2026/",
  "flow": "backup-dropbox"
}
```

`bucket` is required; `prefix` narrows the watch; `credential_id` defaults
to the shared `aws` configuration. Each fire lists the bucket (following continuation
pages up to 1,000 keys) and objects with a `last_modified` strictly newer
than the stored watermark fire as `s3` / `file.created` events carrying
`{bucket, key, size, last_modified, etag}`. The first fire only seeds the
watermark — a new trigger must not fire on the bucket's whole history — and
the composite `last_modified|key` id keeps same-second uploads distinct.

### Updated and deleted files: the `s3.updates` / `s3.deletions` sources

S3 has no change feed to subscribe to, so two sibling sources diff
consecutive listings on the same machinery — the previous listing rides in
the stored cursor, the same slot drive's changes-page token occupies, with
the same save shape (`bucket` required, `prefix` and `credential_id`
optional):

- `s3.updates` fires `file.updated` when a listed key comes back with a
  changed fingerprint (etag, size or last_modified — an overwrite moves at
  least one), carrying `{bucket, key, size, last_modified, etag}`. A
  brand-new key is the created source's news, never double-fired here.
- `s3.deletions` fires `file.deleted` when a previously-listed key is gone
  from the listing; the event carries only `{bucket, key}` — the object's
  facts went with it.

Each source keeps its own trigger, cursor and seen-set, so all three walk
the bucket independently, and the first fire of each only seeds. Three
situations re-seed and emit nothing instead of diffing: a changed `prefix`
(the old keys were a different watch), a capped listing on a deletions
watch (keys beyond the 1,000-key cap are unlisted, not absent — absence
proves nothing), and an unreadable snapshot. False deletions are worse
than late ones; the cost is that buckets larger than the cap cannot track
deletions (updates still fire from the listed page).

## Actions

All S3 actions take `credential_id` (default `aws`) and render fields from
the event.

| action | S3 call | notes |
| --- | --- | --- |
| `s3_upload` | `PutObject` | target `bucket` + `key`; bytes from exactly one of `source_url` (optionally bearer-authorized by `source_connection_id`, e.g. a Drive download URL) or staged `source_s3 {bucket, key}`; `content_type` defaults to the trigger's `mimeType`; output `{bucket, key, bytes, content_type}` |
| `s3_find` | `ListObjectsV2` | objects matching `pattern` (**Match**: exact, prefix, suffix, contains — compared against the key, falling back to its basename); first match fills `key`/`size`/`last_modified`, up to 25 ride in `matches`; `next_token` chains a truncated walk; a miss is `{found: false}`, not an error |
| `s3_list_objects` | `ListObjectsV2` | bounded listing under `prefix`: `max_items` (default 20, cap 100), keys in S3's alphabetical order; `truncated` + `next_token` keep the walk going; output `{bucket, prefix, items, count, truncated, next_token}` |
| `s3_read_object` | `GetObject` | downloads one object and stages the bytes for later steps; `key` defaults to the triggering event's object key; output `{filename, size, content_type, bucket, key, source_bucket, source_key}` — the staged `bucket`/`key` feed any `source_s3` step |
| `s3_presign_url` | presigned GET (computed locally) | short-lived download link, one hour by default, capped at SigV4's seven days; output `{link, bucket, key, expires_in}` — feeds any step's `source_url` |
| `s3_delete_object` | `DeleteObject` | S3 deletes are idempotent, so there is no `found` flag — output `{deleted: true, bucket, key}` |

### The staged-file handoff

The read actions — `s3_read_object`, `dropbox_read_file`,
`drive_read_file` — stage downloaded bytes in Dapier's artifacts bucket,
and their `{bucket, key}` output plugs straight into the `source_s3` input
of `s3_upload`, `drive_upload_file`, `slack_upload_file`,
`telegram_send_photo` / `telegram_send_document`, and
`youtube_upload_video`. Staging is in memory (the Lambda has no scratch
disk), so keep sources modest.

## Gotchas

- Target bucket permissions come from the selected assumed role or legacy
  keys. Internal staging still uses Dapier's own execution identity.
- `s3_upload` takes exactly one source; naming `source_url` and `source_s3`
  together fails the step.
- Upload keys use safe filename normalization by default. `key_mode: exact`
  preserves the original title and removes a leading slash; read, presign and
  delete address the rendered key directly.
- The first fire of any `s3`-family poll never emits: the watermark/snapshot
  seeds only. Push the trigger schedule tighter than your patience while
  testing.
- The generic HTTP poll trigger and the `s3` sources are different `source`
  values on the same poll-trigger machinery; a bucket watch needs
  `source: "s3"` (new), `"s3.updates"` (changed) or `"s3.deletions"`
  (removed), not a `url`.

### Object metadata without downloading content

`s3_head_object` requires `bucket` and `key`, with optional `credential_id` or
`connection_id`. It uses the existing assumed role/credential and S3 HEAD only,
returning Content-Type, size, last-modified and ETag. It does not retrieve
content or custom metadata. The Console catalog, shared API and CLI workflow
test path expose the same action; no new IAM permission is needed beyond
existing object-read access.
