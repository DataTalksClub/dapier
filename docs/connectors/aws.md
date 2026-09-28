# Amazon S3 connector (the `aws` credential)

S3 actions run on a stored IAM access key pair — the key/secret approach,
not the stack's own identity — so workflows can write to any bucket the key
can reach. There is no OAuth client and no connection record by design: the
names `aws` and `s3` act as pseudo connections that resolve the stored
`aws` credential.

## Connect it

1. In the AWS IAM console, create an access key for a user whose
   permissions cover the buckets the workflows will target. The S3
   operations Dapier issues are ListBuckets, ListObjectsV2, PutObject,
   GetObject, and DeleteObject; the health check calls STS
   `get_caller_identity`. Temporary (ASIA…) and long-lived (AKIA…) keys
   both pass validation — the access key ID is 16–128 letters/digits and
   the secret is the 40-character value AWS shows once.
2. Store the pair. The CLI takes a JSON file with exactly the two fields:

   ```sh
   uv run dapier credentials set aws --file aws-keys.json
   ```

   ```json
   {"access_key_id": "AKIA…", "secret_access_key": "…"}
   ```

   In the console, use **Credentials → AWS keys** and fill both fields.
   The values are write-only and live immediately.
3. Verify it:

   ```sh
   uv run dapier connections test aws
   ```

   The check runs STS `get_caller_identity` and answers with the key's ARN,
   account, and user id. `s3` is an alias for the same check, and the
   console's **Credentials** view has the same test button.

Discovery works on either pseudo connection:
`dapier connections discover aws` lists `buckets` and `objects` (one
bucket's keys, up to 100, optional `prefix` param — pass the bucket as
`--param bucket=<name>`).

## Triggers: the `s3 file.created` poll source

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
to the shared `aws` keys. Each fire lists the bucket (following continuation
pages up to 1,000 keys) and objects with a `last_modified` strictly newer
than the stored watermark fire as `s3` / `file.created` events carrying
`{key, size, last_modified, etag}`. The first fire only seeds the watermark
— a new trigger must not fire on the bucket's whole history — and the
composite `last_modified|key` id keeps same-second uploads distinct.

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

- The key pair, not the stack's identity, writes the target bucket — grants
  on Dapier connections do not apply here; IAM permissions do.
- `s3_upload` takes exactly one source; naming `source_url` and `source_s3`
  together fails the step.
- Target keys are normalized per path segment (safe filenames); read,
  presign, and delete address the key exactly as rendered — no
  normalization.
- The first fire of an `s3` poll never emits: it only seeds the watermark.
  Push the trigger schedule tighter than your patience while testing.
- The generic HTTP poll trigger and the `s3` source are different `source`
  values on the same poll-trigger machinery; a bucket watch needs
  `source: "s3"`, not a `url`.
