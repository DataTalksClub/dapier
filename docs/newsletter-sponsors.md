# Newsletter sponsors: email to the DataOps sponsors-leads intake

Newsletter emails routed to `newsletter@dtcdev.click` are read for paid
sponsor mentions and filed into the DataOps sponsors-leads intake
(`POST /api/v1/intake/leads`, contract `2026-10-10`). Dapier is only the
pipe — transport, extraction and envelope formatting. Sponsor cleansing,
validation, deduplication, records, outreach and review live in DataOps. The canonical definition is
`workflows/newsletter-sponsors.yaml`, live and `enabled: true`; the file
mirrors the published definition. Nothing in Dapier keeps sponsor state.

This mirrors the invoice intake (`docs/invoice-intake.md`,
`workflows/invoice-intake.yaml`): Dapier never parses domain contents for
its own stores; it forwards to the service that owns the domain.

## Pipeline

```
SES (newsletter@dtcdev.click) → email connector → newsletter-sponsors workflow:
  1. extract-sponsors — one ai_complete call (model claude-sonnet-5-5
     via Anthropic's OpenAI-compatible API) with a json_schema for
     {newsletter: string, sponsors: [{name, url?, category?, blurb?, context?}]}
     pulls paid placements from the subject, text, and html. The prompt is
     paid-only (gold 2026-10-10): labeled sponsor slots, asterisk
     placements, "Brought to you by", partner/sponsor headings, mid-roll
     ads, and third-party course-sponsor thanks. It skips editorial links,
     author bios, unsubscribe, "sponsor this newsletter" CTAs, the
     newsletter's own job board, and publisher self-promo. `name` is
     required; `category` is optional (omit rather than guess). The schema
     is enforced provider-side (response_format json_schema) and
     re-validated locally; an unparsable reply or a schema mismatch
     returns {ok: false, ...} for the chain to branch on. Datamailer
     stores `data.body` as `{html: {value: ...}}` and SES intake stores
     string `html`/`text`; the prompt lists both so the model sees the
     issue either way. Before rendering, `ai_complete` loads referenced
     UTF-8 text/HTML parts from storage into a private copy of the event.
     This also covers HTML-only messages. Unreadable parts, invalid UTF-8,
     or parts over 2 MB fail the step before contacting the model; missing
     content must not become a successful empty sponsor result. A slim
     event with only a raw MIME reference is decoded before rendering.
  2. forward-leads — dataops POST to the DataOps connection origin plus
     `/api/v1/intake/leads` with the
     dapier/dataops secret as x-dataops-intake-secret, the same auth as
     the invoice intake. The envelope is built by
     engine.actions.dataops._leads_intake_body from the email event plus
     the extraction. It forwards the sponsors array unchanged, including
     duplicate mentions, invalid URLs and whitespace. DataOps trims and
     caps text, drops unusable or hallucinated URLs while retaining names,
     drops nameless entries, deduplicates by URL (otherwise normalized name),
     then validates the 20-sponsor limit. First mention wins. An empty
     cleaned array still records the issue in DataOps. A forward failure fails the run.
```

Each email is one SQS message, so concurrent newsletters file concurrently.
The intake dedups per lead on `sha256("lead:"+messageId+name+url)`:
exact/cosmetic replays return `duplicate`; the same identity with changed
content returns `409 idempotency-conflict` (first write wins). Retrying
after a lost response is safe.

For audit purposes, distinguish a completed run from a verified extraction.
Older runs may have rendered only body-reference metadata. Check that the
model received the body before accepting an empty array as evidence of no
sponsors. The redacted referenced-body regression fixture exercises the
shipped prompt with a closing placement beyond 60 KB; its completion is
stubbed, so it verifies transport rather than model accuracy.

## Leads contract (implemented in DataOps)

`POST {connection.url}/api/v1/intake/leads`
(the DataOps connection's https origin, e.g. `https://ops.dtcdev.click`),
`Content-Type: application/json`, credential in `x-dataops-intake-secret`
(the existing `dapier/dataops` secret — no new secret):

```json
{
  "version": "2026-10-10",
  "messageId": "<abc123@example.test>",
  "newsletter": {
    "name": "Data Bites Weekly",
    "sender": "news@example.test",
    "subject": "Data Bites Weekly #42",
    "receivedAt": "2026-10-10T08:00:00Z"
  },
  "sponsors": [
    {"name": "Acme Cloud", "url": "https://acme.example",
     "category": "tool", "blurb": "Try Acme free", "context": "mid-roll"}
  ]
}
```

`version` is exactly `2026-10-10`. `sponsors` carries the raw extraction array; DataOps accepts 0–20 entries
after cleansing (an empty cleaned array records the issue);
`url` is optional (omit when unknown or not `https:` — DataOps drops
junk URLs rather than failing); `category` is optional (one of
tool/product/service/event/course/book/job/community/other, max 40);
`blurb`/`context` are optional (max 2000).
Response lead/failure indices refer to original request positions, even when
DataOps filters or deduplicates entries. Existing source identities and
idempotency conflicts remain unchanged. Deploy the DataOps receiver change
before deploying the raw Dapier forwarder.

`receivedAt` is strict UTC `YYYY-MM-DDTHH:MM:SS[.mmm]Z`. No extra keys at
any level. Tests: `tests/test_dataops_leads.py`.

## Live setup (one-time)

1. Deploy. Set the DataOps API origin on the connection:
   `dapier connections edit dataops --url https://ops.dtcdev.click`.
   The dataops step uses `connection_id: dataops` and
   `extraction_step` (leads) or omits it (documents).

2. The `dapier/dataops` secret already holds the intake token — reuse it;
   the leads intake accepts the same credential as email-documents.
3. `dapier workflows save workflows/newsletter-sponsors.yaml && dapier
   workflows publish newsletter-sponsors && dapier workflows on
   newsletter-sponsors`.
4. Forward a newsletter to `newsletter@dtcdev.click`; expect DataOps
   lead items, not a dapier-side entry.

## Console note

`designer/src/catalog.ts` carries the new `json_schema` (ai_complete) and
`extraction_step` (dataops) fields. The vendored console bundle
(`src/web/designer.js`, via `make designer-console`) is rebuilt separately
— rerun it after the in-flight designer bundle change lands.
