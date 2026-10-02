# AI connector (ai_complete)

`ai_complete` sends a templated prompt to an OpenAI-compatible chat endpoint
and lands the reply in the step output — summarize, classify, extract,
draft, right inside a workflow. There is **no connection**: configuration is
environment-only, and nothing new is deployed to enable it.

## Configure it

The endpoint, key and default model are environment-only:

| Env var | Meaning | Default |
|---------|---------|---------|
| `LLM_API_KEY` | Bearer key for the endpoint (**required** — without it `ai_complete` refuses to run) | — |
| `LLM_BASE_URL` | OpenAI-compatible base URL | `https://api.openai.com/v1` |
| `LLM_MODEL` | Default model for completions | `gpt-4o-mini` |

template.yaml wires these onto both the API function (designer test runs)
and the **Worker** function (real runs) via the `LlmBaseUrl` / `LlmApiKey`
/ `LlmModel` parameters, empty by default. Enabling the action is an ops
step:

1. Keep the secret value out of git: pass `LlmApiKey` at deploy time
   (parameter override backed by Secrets Manager or your usual parameter
   store), the way the other function secrets are handled.
2. Redeploy. No table, queue or connection changes are involved.

Unconfigured, the step fails with
`ai_complete is not configured (set LLM_API_KEY …)`, naming every variable
it needs.

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
| `temperature` | no | 0–2; 0 when left out |
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

## Discovery

None. There is no provider account to browse — the model and its parameters
are the configuration. The palette chip (**AI**, no connection) exists so
the action is findable; trigger-style events do not apply.
