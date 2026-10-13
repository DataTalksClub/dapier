# Paid API cost tracking

The Console **Costs** page, `dapier costs --months 3`, and
`GET /api/{admin,agent}/costs?months=3` read the same operational request
telemetry. Tracking starts with deployment; historical billing is not imported.
The initial integration covers `ai_complete`, OpenRouter decisions (jev), and
connection inference tests. The record format accepts other paid API services
in future integrations.

Each successful HTTP inference response creates one request record before
reply parsing or schema validation. An unusable reply can still be billed.
Provider response IDs prevent duplicate recording of the same response; a
retry that produces another provider response is another billed request.
Responses without provider IDs receive a generated ID, identified explicitly
in the record. HTTP errors and requests whose response was lost cannot be
reliably priced by this telemetry; provider billing remains authoritative.

Records contain provider, model, connection, workflow or connection-test
context, timestamp, input/output tokens, currency, cost, and price source.
Prompts, replies, keys, and identity tokens are not retained. Recording failures
are logged and never change the action's result. This is operational API
telemetry, not invoice processing or a bookkeeping store.

`provider-reported` uses the provider's USD `usage.cost`, including an explicit
zero. `estimate` uses supported Anthropic standard global, non-batch token
rates below. Missing rates, unknown models, unsupported cache creation or
service tiers, and missing usage remain `unpriced` with a null cost. These
three categories stay separate in API totals, CLI output, and the Console.

Estimates use USD per **million tokens**, checked on **2026-10-13** against
[Anthropic's pricing documentation](https://platform.claude.com/docs/en/about-claude/pricing).

| Model generation | Input | Output | Cache read |
| --- | ---: | ---: | ---: |
| Sonnet 5.5 | 2 | 10 | 0.10 |
| Opus 5.5 | 4 | 20 | 0.20 |
| Sonnet 4.6 / 4.5 | 3 | 15 | 0.30 |
| Haiku 4.5 | 1 | 5 | 0.10 |

The estimate supports named model IDs and dated versions. OpenRouter prices
come from response receipts rather than static model tariffs. Requests may
have zero tokens without being unpriced when a provider reports a charge.

Reads paginate monthly DynamoDB partitions across 1–24 calendar months and
return at most 5,000 requests. If the limit is reached, `truncated: true`
marks totals as partial. Existing workflow visibility rules apply to both HTTP
surfaces. The DynamoDB table has point-in-time recovery and retention across
stack replacement; worker IAM grants only PutItem; the HTTP API has reads and a narrowly scoped
PutItem grant for inline decisions and connection tests.
