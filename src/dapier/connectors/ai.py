"""AI connector: one OpenAI-compatible LLM completion as a workflow action.

``ai_complete`` sends a templated prompt — plus an optional system message —
to a configured OpenAI-compatible /chat/completions endpoint, with stdlib
urllib: no new infrastructure, no connection record and no discovery.
Configuration is environment-only on the Worker function: LLM_BASE_URL
(default https://api.openai.com/v1), LLM_API_KEY (no default — unset fails
the step with a setup message naming the env vars, loud like any other
unconfigured action), and LLM_MODEL (default gpt-4o-mini). template.yaml
carries no env default for the key, so enabling it is an ops step
(docs/connectors/ai.md).

Content problems never raise: a ``json_mode`` reply that will not parse
comes back as ``{ok: false, error, text}`` for the chain to branch on.
Transport problems raise the webhook/http_request ``HttpError`` (status and
Retry-After included), so autoretry and failure notify treat an LLM outage
like any other HTTP call.
"""
import json
import os
import re
import urllib.error
import urllib.request

from ..engine.actions.templating import render
from ..engine.actions.webhook import HttpError, retry_after_seconds
from .registry import Action, Connector, connector, register

connector(Connector(name="ai", label="AI", events=(), icon="sparkles"))

# The OpenAI chat-completions request shape, with temperature 0 and the
# configured model as defaults opened up as action fields.
TEMPERATURE_BOUNDS = (0.0, 2.0)
_JSON_TRUE = frozenset({"true", "1", "yes", "on"})
# Models wrap structured replies in code fences even under response_format,
# and the JSON parse should not care.
_JSON_FENCE_RE = re.compile(r"```(?:json)?[ \t]*\r?\n(.*?)```", re.DOTALL | re.IGNORECASE)

BASE_URL_ENV = "LLM_BASE_URL"
API_KEY_ENV = "LLM_API_KEY"
MODEL_ENV = "LLM_MODEL"
DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"
LLM_TIMEOUT_SECONDS = 25


def _base_url():
    return os.environ.get(BASE_URL_ENV, DEFAULT_BASE_URL).rstrip("/")


def _model():
    return os.environ.get(MODEL_ENV, DEFAULT_MODEL).strip() or DEFAULT_MODEL


register(Action(
    type="ai_complete",
    label="AI: complete",
    icon="sparkles",
    description=("One chat completion against a configured OpenAI-compatible "
                 "endpoint (LLM_* env on the Worker function). The "
                 "prompt renders from the event; JSON mode parses the reply "
                 "into `data` (an unparsable reply returns {ok: false, error} "
                 "instead of failing the step). Output: {ok, text|data, model, "
                 "usage}."),
    run=lambda action, event, workflow_id, steps=None: run_ai_complete(action, event, steps=steps),
    required=frozenset({"prompt"}),
    optional=frozenset({"system", "json_mode", "temperature", "model", "timeout_seconds"}),
    fields=(
        {"key": "prompt", "label": "Prompt", "type": "textarea", "required": True,
         "placeholder": "Summarize this message for the digest:\n{body}"},
        {"key": "system", "label": "System message", "type": "textarea",
         "help": "Optional role and standing instructions, sent as the system message"},
        {"key": "json_mode", "label": "JSON mode", "type": "boolean", "default": "false",
         "help": ("Ask for a JSON object reply and parse it into the output's "
                  "`data`; a reply that will not parse sets ok: false and keeps "
                  "the raw text in `text`")},
        {"key": "temperature", "label": "Temperature", "type": "number",
         "help": "0-2; 0 when left out"},
        {"key": "model", "label": "Model",
         "help": "Overrides the configured default (LLM_MODEL)"},
        {"key": "timeout_seconds", "label": "Timeout (s)", "type": "number"},
    ),
))


def _json_mode(action):
    """Whether the step asked for (and wants parsed) a JSON object reply."""
    value = action.get("json_mode")
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in _JSON_TRUE


def _temperature(value):
    """The action's temperature, validated against OpenAI's 0-2 range; left
    out keeps the 0 default."""
    if value is None or not str(value).strip():
        return 0
    if isinstance(value, bool):
        raise ValueError("ai_complete temperature must be a number between 0 and 2")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(
            "ai_complete temperature must be a number between 0 and 2") from None
    low, high = TEMPERATURE_BOUNDS
    if not low <= number <= high:
        raise ValueError(f"ai_complete temperature must be between {low:g} and {high:g}")
    return number


def _unfenced(reply):
    """The reply minus one wrapping markdown code fence, trimmed — models
    wrap structured replies in fences even when asked not to."""
    text = str(reply or "").strip()
    match = _JSON_FENCE_RE.search(text)
    return match.group(1).strip() if match else text


def _default_transport(url, payload, headers, timeout):
    """The urllib fallback — webhook's default transport without the headers
    this action has no use for. HTTP errors propagate for run_ai_complete
    to type."""
    request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, response.read()


def run_ai_complete(action, event, *, steps=None, transport=None):
    """One completion: the rendered prompt (plus optional system message) in,
    ``{ok, text|data, model, usage}`` out.

    Transport is the webhook/http_request seam — an injectable
    ``(method, url, *, headers, body, timeout) -> (status, raw)`` so tests
    record the request instead of hitting the network; the default is urllib
    against the configured endpoint. HTTP 4xx/5xx, timeouts and an unreadable
    response all raise ``HttpError`` (Retry-After included when the server
    sent one), which is what the engine's autoretry reads; a json_mode reply
    that will not parse is a verdict, never a raise.
    """
    temperature = _temperature(action.get("temperature"))
    key = os.environ.get(API_KEY_ENV, "").strip()
    if not key:
        raise ValueError(
            f"ai_complete is not configured (set {API_KEY_ENV} — and "
            f"optionally {BASE_URL_ENV} and {MODEL_ENV} — on "
            "the Worker function)")
    prompt = render(action.get("prompt") or "", event, steps).strip()
    if not prompt:
        raise ValueError("ai_complete requires a rendered prompt")
    messages = []
    system = render(str(action.get("system") or ""), event, steps).strip()
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    model = str(action.get("model") or "").strip() or _model()
    payload = json.dumps({
        "model": model,
        "messages": messages,
        "temperature": temperature,
        **({"response_format": {"type": "json_object"}} if _json_mode(action) else {}),
    }).encode()
    url = f"{_base_url()}/chat/completions"
    headers = {
        "authorization": f"Bearer {key}",
        "content-type": "application/json",
        "user-agent": "dapier-ai-complete",
    }
    timeout = action.get("timeout_seconds", LLM_TIMEOUT_SECONDS)
    if transport is not None:
        status, raw = transport("POST", url, headers=headers, body=payload, timeout=timeout)
    else:
        try:
            status, raw = _default_transport(url, payload, headers, timeout)
        except urllib.error.HTTPError as exc:
            # urllib raises for HTTP 4xx/5xx before the status check below;
            # type it like the webhook/http_request default path does.
            raise HttpError(
                f"ai_complete returned HTTP {exc.code}",
                status=exc.code,
                retry_after=retry_after_seconds(exc.headers),
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise HttpError(f"ai_complete request failed: {exc}") from exc
    if status >= 300:
        raise HttpError(f"ai_complete returned HTTP {status}", status=status)
    try:
        body = json.loads(raw.decode())
    except (ValueError, UnicodeDecodeError) as exc:
        raise HttpError(f"ai_complete returned an unexpected response: {exc}") from exc
    if not isinstance(body, dict) or not body.get("choices"):
        raise HttpError("ai_complete returned an unexpected response: "
                        f"{json.dumps(body)[:300]}")
    try:
        reply = str(body["choices"][0]["message"]["content"] or "")
    except (IndexError, KeyError, TypeError) as exc:
        raise HttpError(f"ai_complete returned an unexpected response: {exc}") from exc
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    answered_model = str(body.get("model") or model)

    if _json_mode(action):
        try:
            data = json.loads(_unfenced(reply))
        except ValueError as exc:
            return {
                "ok": False,
                "error": f"ai_complete json_mode could not parse the reply as JSON: {exc}",
                "text": reply,
                "model": answered_model,
                "usage": usage,
            }
        return {"ok": True, "data": data, "model": answered_model, "usage": usage}
    return {"ok": True, "text": reply, "model": answered_model, "usage": usage}
