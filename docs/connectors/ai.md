# AI connector (ai_complete, decision)

`ai_complete` sends a templated prompt to an OpenAI-compatible chat endpoint
and lands the reply in the step output — summarize, classify, extract,
draft, right inside a workflow. There is **no connection**: configuration is
environment-only, and nothing new is deployed to enable it.

## Configure it

The endpoint, key, default model and extra headers are environment-only:

| Env var | Meaning | Default |
|---------|---------|---------|
| `LLM_API_KEY` | Bearer key for the endpoint (**required** — without it `ai_complete` refuses to run) | — |
| `LLM_BASE_URL` | OpenAI-compatible base URL | `https://api.openai.com/v1` |
| `LLM_MODEL` | Default model for completions | `gpt-4o-mini` |
| `LLM_HEADERS_JSON` | Extra request headers as a JSON object (rarely needed) | — |

template.yaml wires these onto both the API function (designer test runs)
and the **Worker** function (real runs) via the `LlmBaseUrl` / `LlmApiKey`
/ `LlmModel` parameters, empty by default. Enabling the action is an ops
step:

1. Keep the secret value out of git: store the key as the repository's
   `LLM_API_KEY` Actions secret (`gh secret set LLM_API_KEY`) and, for a
   non-OpenAI endpoint, `LLM_BASE_URL` / `LLM_MODEL` as Actions variables
   (`gh variable set LLM_BASE_URL --body https://api.anthropic.com/v1`).
   `scripts/deploy.sh` passes each one that is set as the `LlmApiKey` /
   `LlmBaseUrl` / `LlmModel` override; an unset one keeps the deployed value.
2. Redeploy (`gh workflow run deploy.yml`). No table, queue or connection
   changes are involved.

Unconfigured, the step fails with
`ai_complete is not configured (set LLM_API_KEY …)`, naming every variable
it needs.

## Anthropic (Claude) via the official OpenAI-compatible API

Anthropic serves an OpenAI-compatible `POST /v1/chat/completions` on
`https://api.anthropic.com/v1/` taking the Claude key as a Bearer token —
no gateway needed:

| Env var | Value |
|---------|-------|
| `LLM_BASE_URL` | `https://api.anthropic.com/v1` |
| `LLM_API_KEY` | the `ANTHROPIC_API_KEY` value (deploy-time parameter, never git) |
| `LLM_MODEL` | `claude-sonnet-5-5` (or per-step `model:`) |

Prefer a **workspace-scoped** key (console → API keys): it needs no extra
headers. A personal key that is not scoped to a workspace must send
`anthropic-workspace-id` on every request — put it in `LLM_HEADERS_JSON`
(`{"anthropic-workspace-id": "wrkspc_…"}`), which keeps header values out
of committed workflow YAML. A malformed `LLM_HEADERS_JSON` fails the step
with a setup error before any request goes out.

Verified compat notes (probed live against `claude-sonnet-5-5`):

- `json_schema` is **enforced strictly**: the action normalizes the schema
  (injects `additionalProperties: false` on objects, sends `strict: true`)
  when the endpoint is `api.anthropic.com`. Other endpoints keep the
  generic payload untouched.
- `json_object` is **rejected** by Anthropic, so `json_mode` without a
  schema sends no `response_format` there — the prompt must demand JSON
  and the local parse stays the guarantee.
- `temperature` is **deprecated** on current Claude models (HTTP 400), so
  the action omits it unless the step sets it.
- Auth is the standard `Authorization: Bearer` header the action already
  sends; usage tokens come back in the normal OpenAI shape.

## The action

```yaml
actions:
  - id: triage
    type: ai_complete
    prompt: |
      Classify this message and answer as JSON:
      {"category": "bug|billing|other", "urgency": "low|high"}
      Message: {body}
    json_mode: true
    temperature: 0.2
    # system: You are the support triage assistant.
    # model: gpt-4o        # overrides LLM_MODEL
    # timeout_seconds: 25
```

| Field | Required | Notes |
|-------|----------|-------|
| `prompt` | yes | Templated: `{field}`, `{trigger.*}`, `{steps.<id>.output.*}` and formatters all expand before the call |
| `system` | no | Sent as the system message |
| `json_mode` | no | Requests a JSON object (`response_format: json_object`) and parses it into `data` |
| `json_schema` | no | A JSON Schema object (dict or JSON string) with `type: object`; implies JSON mode, sends `response_format: json_schema` named `extraction`, and validates the parsed reply (required fields, top-level and array-item types). Note: Anthropic's compatibility layer ignores `response_format`, so with Claude the prompt must demand JSON and local validation is the guarantee |
| `temperature` | no | 0–2; omitted when left out (the provider default applies — required for Claude, which rejects the field) |
| `model` | no | Overrides `LLM_MODEL` for this step |
| `timeout_seconds` | no | Default 25 |

### Output

Text mode:

```json
{"ok": true, "text": "…the completion…", "model": "gpt-4o-mini",
 "usage": {"prompt_tokens": 31, "completion_tokens": 120, "total_tokens": 151}}
```

With `json_mode: true` the parsed object arrives as `data` instead of
`text`:

```json
{"ok": true, "data": {"category": "bug", "urgency": "high"},
 "model": "…", "usage": {…}}
```

A reply that does not parse as JSON is a **verdict, not a failure** — the
step succeeds with `{"ok": false, "error": "…", "text": "<raw reply>", …}`,
so a chain can branch on `{steps.triage.output.ok}` or retry with a
stricter prompt. Later steps template `{steps.triage.data.category}` when
`ok` is true.

With `json_schema` the same verdict rule covers schema mismatches: a reply
that parses but misses a required field or has the wrong type for a
top-level (or array-item) property returns
`{"ok": false, "error": "ai_complete json_schema validation failed: …",
"text": "<raw reply>", …}`. A `json_schema` value that is not a JSON Schema
object with `type: object` fails the step with `ValueError` (a
configuration error, not a verdict). A provider that rejects the
`json_schema` response_format answers HTTP 400, which surfaces as the
usual `HttpError` for `autoretry` — there is no silent `json_object`
fallback.

### Structured extraction

```yaml
actions:
  - id: extract
    type: ai_complete
    model: claude-sonnet-5-5
    prompt: |
      Extract the newsletter subject and paid sponsor placements as JSON matching the schema.
      Text:
      {text}
      HTML:
      {html}
    json_schema:
      type: object
      required: [newsletter, sponsors]
      properties:
        newsletter:
          type: string
        sponsors:
          type: array
        items:
          type: object
          required: [name]
          properties:
            name:
              type: string
            url:
              type: string
            category:
              type: string
              enum: [tool, product, service, event, course, book, job, community, other]
```

The request sends
`response_format: {type: json_schema, json_schema: {name: extraction,
schema: <schema>}}`; the parsed object lands in `data`
(`{steps.extract.data.newsletter}`,
`{steps.extract.data.sponsors}` when `ok` is true).

### Errors

Transport problems fail the step like any HTTP action: HTTP 4xx/5xx,
timeouts and unreadable responses raise the webhook/http_request `HttpError`
(with the status and the server's `Retry-After` hint attached), so
`autoretry` paces a rate-limited endpoint the same way it paces webhooks,
and `on_fail: continue` / `on_error` policies apply unchanged:

```yaml
  - id: triage
    type: ai_complete
    prompt: "Summarize: {body}"
    autoretry: {attempts: 2, initial_seconds: 2, max_seconds: 20}
```

## The decision step (smart if / switch)

`decision` is the branching sibling of `ai_complete`: one typed call to
**OpenRouter's decisions endpoint** (the jev route) picks a branch, and the
step runs that branch's steps inline — a smart `if` statement, or a
`switch` over named choices. The decisions endpoint returns a picked option
with probabilities and a confidence, not chat text, so there is nothing to
parse and a reply cannot route anywhere illegal. The endpoint is OpenRouter's
own, so the key is not env: `decision` authenticates with the stored
**`openrouter` credential** — the same credential store behind the Slack,
Mailchimp, and AWS secrets. Set it once from the console's Credentials tab
or with `dapier credentials set openrouter` (the key travels only in the
request body and is never shown again); the credential's **Test connection**
exercises the key against OpenRouter's key-introspection endpoint. The
decision model defaults to `~typesafe/jev-latest`; set `model:` per step to
override.

```yaml
actions:
  - id: classify-recording
    type: decision
    mode: switch
    prompt: |
      Classify this Zoom cloud recording into exactly one bucket.
      Topic: {topic}
      Host: {host_email}
      Start: {start_time}
    choices: [one-on-one, ai-shipping-labs]
    cases:
      - choice: one-on-one
        description: a recorded call between two people
        actions:
          - id: process-call
            type: agent
            engine: claude
            workspace: /home/alexey/git/zoom-calls
            prompt: "Process this one-on-one recording: {topic} ..."
      - choice: ai-shipping-labs
        description: an AI Shipping Labs session — cohort lecture, workshop, or guest talk
        actions:
          - id: process-aisl
            type: agent
            engine: claude
            workspace: /home/alexey/git/ai-shipping-labs
            prompt: "Process this AI Shipping Labs recording ..."
    default_actions:
      - id: ask-operator
        type: email_send
        to: alexey@datatalks.club
        subject: "Unclassified Zoom recording: {topic}"
        text: "The decision step could not classify this recording — route it by hand." 
```

| Field | Required | Notes |
|-------|----------|-------|
| `mode` | no | `if` or `switch`; omitted, it is inferred — a step carrying `choices`/`cases` is a switch, everything else an if |
| `prompt` | yes | Templated like `ai_complete`; sent as the question's `state` |
| `system` | no | The question's instructions (a mode-appropriate default when omitted) |
| `model` | no | Overrides the default decision model (`~typesafe/jev-latest`) |
| `timeout_seconds` | no | Default 25 |
| `choices` | switch | Non-empty list of unique strings — the options the model picks from |
| `cases` | switch | `[{choice, actions, description?}]`; each `choice` must be in `choices`, at most once per case; a `description` becomes that option's criterion |
| `default_actions` | switch | Runs when the answer is unusable or off-list; without it the step fails (on_fail/on_error then apply) |
| `then` / `else` | if | The steps for the yes/no sides, like a condition step's branches |

An `if` is the same choice question with `yes`/`no` criteria — the answer
maps to `then`/`else` and surfaces as a boolean `choice`. An off-list or
missing answer runs `default_actions` (switch) or fails the step (if, or
switch without a default). Transport errors raise the usual `HttpError`;
unlike the logic-step kind it resembles, `decision` is not in the worker's
config-error set, so the workflow-level `retry:` redrive still covers a
rate-limited endpoint. It cannot carry `autoretry:` (logic steps never do)
— use a per-step `on_error: run` branch or the workflow retry.

### Output

```json
{"mode": "switch", "choice": "one-on-one", "branch": "one-on-one",
 "steps": 1, "model": "~typesafe/jev-latest", "usage": {…},
 "confidence": 0.93, "probabilities": {"one-on-one": 0.93,
 "ai-shipping-labs": 0.07}}
```

`branch` is the side that ran (`then`/`else`, the matching choice, or
`default`), so later steps can react with
`{steps.classify-recording.choice}` without re-asking the model. The branch
id nests under the step in run history (`classify-recording.one-on-one.process-call`),
exactly like a condition's `then`/`else`. Per-step testing evaluates the
step without consulting the LLM (the report says what a run would check);
a test run with execute consults the model for real.

## Discovery

None. There is no provider account to browse — the model and its parameters
are the configuration. The palette chip (**AI**, no connection) exists so
the action is findable; trigger-style events do not apply.
