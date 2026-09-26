"""AI copilot: turn a natural-language prompt into a DRAFT workflow YAML.

API/CLI-first by design (issue #13): coding agents drive the copilot through
the operator-gated ``POST /api/agent/copilot/draft`` endpoint and the
``dapier workflows draft`` command; ``POST /api/admin/copilot/draft`` mirrors
the same handler for a future console view. A draft is returned, never saved
or published — the caller reviews the YAML and saves it through the existing
designer save path, so nothing the model produced goes live without an
explicit operator commit.

The LLM is any OpenAI-compatible /chat/completions endpoint, called with
stdlib urllib (no new dependencies). Configuration is environment-only:
COPILOT_LLM_BASE_URL (default https://api.openai.com/v1), COPILOT_LLM_API_KEY
(no default — unset means the copilot answers 503 with setup instructions),
and COPILOT_LLM_MODEL (default gpt-4o-mini).

The system prompt describes the exact workflow schema the engine reads
(triggers, filters, action catalog, shared flows) with few-shot examples
mined from the deployed workflows/*.yaml bundle, and demands a reply that is
only a YAML document. The reply is parsed deterministically and validated
with the designer save path's own ``parse_workflow``, so what the caller
sees in ``errors[]`` is exactly what a save would reject.
"""

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

import yaml

from .api import designer_store

BASE_URL_ENV = "COPILOT_LLM_BASE_URL"
API_KEY_ENV = "COPILOT_LLM_API_KEY"
MODEL_ENV = "COPILOT_LLM_MODEL"
DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"
LLM_TIMEOUT_SECONDS = 25
MAX_PROMPT_CHARS = 4000

# Few-shot demos: real workflows from the bundle, covering a YouTube trigger
# with a Slack action, an email trigger with a Dropbox action, and a Dropbox
# trigger with a DataOps chain. Loaded from the same directory the engine
# reads, so the examples never drift from what actually runs.
EXAMPLE_FILES = (
    "youtube-slack.yaml",
    "email-attachment-dataops.yaml",
    "dropbox_on_upload.yaml",
)


class LlmError(Exception):
    """The copilot could not get a usable completion from the LLM."""


class LlmConfigError(LlmError):
    """The LLM is not configured (no COPILOT_LLM_API_KEY)."""


def base_url():
    return os.environ.get(BASE_URL_ENV, DEFAULT_BASE_URL).rstrip("/")


def model():
    return os.environ.get(MODEL_ENV, DEFAULT_MODEL).strip() or DEFAULT_MODEL


def _api_key():
    return os.environ.get(API_KEY_ENV, "").strip()


def _llm_complete(messages, *, timeout=LLM_TIMEOUT_SECONDS):
    """One chat completion against the configured OpenAI-compatible endpoint.

    Deliberately a small standalone function: tests monkeypatch it, and no
    other module needs to know the wire format.
    """
    key = _api_key()
    if not key:
        raise LlmConfigError(
            "the copilot LLM is not configured: set COPILOT_LLM_API_KEY "
            "(and optionally COPILOT_LLM_BASE_URL and COPILOT_LLM_MODEL) "
            "to enable draft generation"
        )
    payload = json.dumps({
        "model": model(),
        "messages": messages,
        "temperature": 0,
    }).encode()
    request = urllib.request.Request(
        f"{base_url()}/chat/completions",
        data=payload,
        headers={
            "authorization": f"Bearer {key}",
            "content-type": "application/json",
            "user-agent": "dapier-copilot",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise LlmError(f"LLM request failed: HTTP {exc.code} {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise LlmError(f"LLM request failed: {exc}") from exc
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LlmError(f"LLM returned an unexpected response: {json.dumps(body)[:300]}") from exc
    return str(content or "")


_SCHEMA_INSTRUCTIONS = """\
You are the Dapier workflow copilot. Convert the user's description into ONE
Dapier workflow definition as a single YAML document.

Reply with ONLY the YAML document — no prose, no markdown fences, no
explanation.

Schema (all validated server-side; the draft must satisfy all of it):
- id: required. kebab-case, letters/digits/hyphens/underscores, max 63 chars.
- enabled: optional boolean, default true.
- Exactly one of:
  - trigger: a single trigger mapping, or
  - triggers: a non-empty list of trigger mappings sharing the same actions.
  Every trigger needs `connector` and `event`, plus optional `filters`.
  Connectors and their events:
  - email / message.received     (inbound email; filter on `route`, `sender`, `subject`)
  - dropbox / file.created       (filter on `path` with prefix/suffix)
  - youtube / video.published    (filter on `channel_id` with equals)
  - webhook / request.received   (stored webhook trigger)
  - telegram / message.received  (stored Telegram trigger)
  - schedule / schedule.triggered
  filters: a mapping of event field -> exactly one rule:
  {equals: v} {in: [a, b]} {prefix: p} {suffix: s} {contains: c}
- Exactly one of:
  - actions: a non-empty list, or
  - flow: the name of a shared flow (only for flows that already exist; when
    in doubt use inline actions).
- Never put both `actions` and `flow` on one workflow.
- Every action needs an `id` (unique in the workflow) and a `type`.
- Write `actions` (or the `flows`/`flow` binding) before `trigger` in the
  document; `id` and `enabled` come first.

Action catalog (type: fields):
- webhook: url (POSTs the event JSON), optional secret_id, timeout_seconds
- slack: connection_id, channel, text (template with {field} placeholders
  from the event data), optional unfurl_links
- telegram_send: connection_id, optional chat_id (defaults to the triggering
  chat), text
- email_send: to, subject, and text or html ({field} templates allowed)
- dataops: connection_id, url_env, auth_secret_id (pushes an intake document)
- dropbox_upload: connection_id, folder, optional source: attachment
- dropbox_delete: connection_id
- render_html_to_pdf: optional input_field, output_key

Use a sensible id derived from the description. Keep action ids like
notify-slack or intake-invoice. Only reference connections by a plain id
slug (e.g. connection_id: slack). Do not invent action types, connectors,
or events that are not listed here."""


def _workflows_root():
    env = os.environ.get("WORKFLOWS_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "workflows"


def _examples():
    """Few-shot YAML from the deployed bundle; missing files are skipped."""
    blocks = []
    for name in EXAMPLE_FILES:
        try:
            text = (_workflows_root() / name).read_text()
            if isinstance(yaml.safe_load(text), dict):
                blocks.append(f"Example — {name}:\n```yaml\n{text.strip()}\n```")
        except (OSError, yaml.YAMLError):
            continue
    return blocks


def system_prompt():
    return "\n\n".join([_SCHEMA_INSTRUCTIONS, *_examples()])


_FENCE_RE = re.compile(r"```(?:ya?ml)?[ \t]*\r?\n(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_yaml(reply):
    """The YAML document in a model reply: the first fenced block when one is
    present, otherwise the whole reply trimmed. Deterministic on purpose."""
    match = _FENCE_RE.search(str(reply or ""))
    if match:
        return match.group(1).strip()
    return str(reply or "").strip()


def draft_workflow(prompt):
    """A validated DRAFT workflow for ``prompt``; ``(status, payload)``.

    200 with ``{"yaml", "workflow", "errors"}`` even when validation fails
    (``errors[]`` lists what a save would reject, so a coding agent can
    iterate); 400 for a bad prompt, 503 when no LLM key is configured, and
    502 when the LLM call itself fails. Never saves or publishes anything.
    """
    prompt = str(prompt or "").strip()
    if not prompt:
        return 400, {"error": "A non-empty 'prompt' string is required"}
    if len(prompt) > MAX_PROMPT_CHARS:
        return 400, {"error": f"the prompt is too long (max {MAX_PROMPT_CHARS} characters)"}
    try:
        reply = _llm_complete([
            {"role": "system", "content": system_prompt()},
            {"role": "user", "content": prompt},
        ])
    except LlmError as exc:
        status = 503 if isinstance(exc, LlmConfigError) else 502
        return status, {"error": str(exc)}
    yaml_text = extract_yaml(reply)
    if not yaml_text:
        return 502, {"error": "the model returned an empty response; retry"}
    errors = []
    workflow = None
    try:
        workflow = designer_store.parse_workflow(yaml_text)
    except designer_store.WorkflowError as exc:
        errors.append(str(exc))
    return 200, {"yaml": yaml_text, "workflow": workflow, "errors": errors}
