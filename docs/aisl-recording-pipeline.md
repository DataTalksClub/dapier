# AI Shipping Labs recording stages

`aisl-recording-agent` sends both Zoom recording events directly to the
platform's authenticated `POST /api/recording-intake`, before transcript
archival, classification, or agent launch. It sends the recording instance
UUID, meeting ID, start time, event type, and the signed Zoom producer timestamp
(`data.available_at`, normalized from integer epoch-millisecond `event_ts`).
The signature header `x-zm-request-timestamp` uses epoch seconds and is only
for request freshness; it is not recording availability. Dapier
`trigger.occurred_at` is local queue receipt and must not start the deadline.
Delayed queue processing or retries retain the original producer time.
Missing, malformed, or future recording timestamps fail closed with a visible
webhook refusal/workflow error; there is no receipt-now fallback. The platform re-lists Zoom with its
own credentials rather than trusting download URLs supplied in the event.

The website owns recording-instance matching, durable idempotency, S3 upload,
host identity/forward-alias deduplication, email delivery state, and pipeline
alerts. Zero matches return `200` with `matched: false`; exactly one match
returns `202` with `matched: true`. Ambiguous matches return `409` and fail the
workflow visibly rather than guessing. HTTP/network errors stop processing
and use the workflow's bounded retry policy (four attempts, 30-second backoff).
The normal workflow failure notice goes to the existing developer mailbox.

## Stage contracts

| Trigger | Action | Bound from Zoom producer availability |
| --- | --- | --- |
| `recording.completed` | Website queues MP4 upload and one host notice per person; no transcript dependency | S3 and host delivery within 60 minutes |
| `recording.transcript_completed` | Website syncs transcript and invokes its configured restored Hub inbound webhook | Hetzner draft job running within 5 minutes; finished within 60 minutes |

The platform watchdog checks at most every five minutes; alert detection can
therefore follow the breached deadline by up to five minutes. The stage-two
job prepares chapters and a workshop summary following the `workshops-raw`
format and `workshop-followup-materials` skill. Results remain review drafts.
The Dapier AISL path never launches the former recap-publishing agent.

Unmatched recordings retain transcript archival and the existing one-on-one
`zoom-calls` processing branch. The classifier cannot override an AISL match
or launch an AISL publication path. Transcript archival happens after the
platform intake so an archive outage cannot delay recording acceptance.

## Configuration and rollout

1. Deploy and verify the website recording-intake endpoint and Hub integration
   before publishing this workflow. Publish only after independent review and
   green CI for the exact source revision.
2. Store the website staff Authorization header in this workflow's secret
   storage key `aisl_authorization`, using the API/CLI storage setter. Its value
   is the complete `Token <staff-token>` header. Never put it in YAML, command
   arguments, test events, review evidence, or logs. A missing key fails the
   request before any network call. Use the authenticated storage HTTP API from
   a local script if the CLI cannot read secrets from a file or standard input.
3. Save the workflow as a draft, inspect its diff, and publish the saved draft.
   Inspect the live version afterward. Saving YAML alone does not publish it.
4. Use mocked unit tests and the workflow dry-run API. Never replay a live
   recording, execute a synthetic recipient campaign, or publish a recap to
   test this change. End-to-end acceptance needs separately controlled
   non-delivering website fixtures and an isolated Hub draft job.

The operator's Dapier CLI session must be valid to configure or publish. If
`dapier workflows show aisl-recording-agent` says `Not signed in`, use the
supported device login; do not bypass authentication with AWS/DynamoDB writes.

Timestamp reference: [Zoom Meetings webhook event schemas](https://developers.zoom.us/docs/api/meetings/events/) (`event_ts`).
