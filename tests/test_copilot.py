"""Copilot tests: API/CLI prompt-to-draft, all with a stubbed LLM client.

No test ever hits the network: the LLM call is monkeypatched at
``copilot._llm_complete`` (the only network boundary in the module), and the
CLI tests stub the API client like the rest of the suite.
"""

import io
import json

import pytest

from dapier_cli import commands as cli_commands
from src.dapier import copilot
from src.dapier.api import admin
from src.dapier.api import agent as agent_api
from src.dapier.api import designer_store
from src.dapier.auth import session

WORKFLOW_YAML = """\
id: copilot-draft
enabled: true
trigger:
  connector: email
  event: message.received
  filters:
    route:
      equals: copilot-draft
actions:
  - id: notify-slack
    type: slack
    connection_id: slack
    channel: C123
    text: "New message: {subject}"
"""

# What the endpoint actually returns on the wire: extract_yaml trims the reply.
DRAFT_YAML = WORKFLOW_YAML.strip()

FENCED_REPLY = f"Here is your workflow:\n```yaml\n{WORKFLOW_YAML}```\nHope that helps!"

INVALID_YAML = "id: broken-draft\ntrigger:\n  connector: email\n  event: message.received\n"


# ---- Agent endpoint ----

def agent_request(method, path, body=None, token="dtc-token"):
    request = {
        "requestContext": {"http": {"method": method, "path": path}},
        "headers": {"host": "dapier.example.test"},
        "cookies": [],
    }
    if token is not None:
        request["headers"]["authorization"] = f"Bearer {token}"
    if body is not None:
        request["body"] = body if isinstance(body, str) else json.dumps(body)
    return request


@pytest.fixture
def agent_identity(monkeypatch):
    monkeypatch.setenv("AUTH_CLI_CLIENT_ID", "cli-client")
    monkeypatch.delenv("OPERATOR_SUBJECTS", raising=False)
    monkeypatch.delenv("OPERATOR_EMAILS", raising=False)
    monkeypatch.setattr(
        agent_api, "verify_id_token", lambda token, audience=None: {"sub": "agent-op"},
    )
    monkeypatch.setattr(agent_api, "audit", type("Audit", (), {
        "emit": staticmethod(lambda *args, **kwargs: None),
    }))


@pytest.fixture
def never_saves(monkeypatch):
    """The draft endpoint must never reach the designer save/publish path."""

    def boom(*args, **kwargs):
        raise AssertionError("a copilot draft must never be saved or published")

    monkeypatch.setattr(designer_store, "commit_workflow", boom)
    monkeypatch.setattr(designer_store, "api_save", boom)


def stub_llm(monkeypatch, reply=None, error=None):
    calls = []

    def fake(messages, **kwargs):
        calls.append(messages)
        if error is not None:
            raise error
        return reply

    monkeypatch.setattr(copilot, "_llm_complete", fake)
    return calls


def test_agent_copilot_draft_happy_path(monkeypatch, agent_identity, never_saves):
    calls = stub_llm(monkeypatch, reply=WORKFLOW_YAML)
    response = agent_api.route(
        agent_request("POST", "/api/agent/copilot/draft", {"prompt": "email me todos"}),
        "POST", "/api/agent/copilot/draft",
    )
    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["errors"] == []
    assert payload["workflow"]["id"] == "copilot-draft"
    assert payload["yaml"] == DRAFT_YAML
    # The system prompt carried the schema and the bundled few-shot examples.
    system = calls[0][0]["content"]
    assert "id:" in system and "actions:" in system
    assert "youtube-slack.yaml" in system


def test_agent_copilot_draft_unwraps_markdown_fences(monkeypatch, agent_identity, never_saves):
    stub_llm(monkeypatch, reply=FENCED_REPLY)
    response = agent_api.route(
        agent_request("POST", "/api/agent/copilot/draft", {"prompt": "email me todos"}),
        "POST", "/api/agent/copilot/draft",
    )
    payload = json.loads(response["body"])
    assert response["statusCode"] == 200
    assert payload["yaml"] == DRAFT_YAML
    assert payload["errors"] == []


def test_agent_copilot_draft_returns_errors_without_saving(monkeypatch, agent_identity, never_saves):
    stub_llm(monkeypatch, reply=INVALID_YAML)
    response = agent_api.route(
        agent_request("POST", "/api/agent/copilot/draft", {"prompt": "a broken one"}),
        "POST", "/api/agent/copilot/draft",
    )
    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["workflow"] is None
    assert payload["errors"] == ["connect at least one action to the trigger"]


def test_agent_copilot_draft_requires_a_prompt(monkeypatch, agent_identity):
    stub_llm(monkeypatch, reply=WORKFLOW_YAML)
    for body in (None, {}, {"prompt": ""}, {"prompt": "   "}, {"nope": 1}):
        response = agent_api.route(
            agent_request("POST", "/api/agent/copilot/draft", body),
            "POST", "/api/agent/copilot/draft",
        )
        assert response["statusCode"] == 400, body
    malformed = agent_request("POST", "/api/agent/copilot/draft", "{not json")
    assert agent_api.route(malformed, "POST", "/api/agent/copilot/draft")["statusCode"] == 400


def test_agent_copilot_draft_is_operator_gated(monkeypatch, agent_identity):
    monkeypatch.setenv("OPERATOR_SUBJECTS", "someone-else")
    stub_llm(monkeypatch, reply=WORKFLOW_YAML)
    assert agent_api.route(
        agent_request("POST", "/api/agent/copilot/draft", {"prompt": "p"}),
        "POST", "/api/agent/copilot/draft",
    )["statusCode"] == 403


def test_agent_copilot_draft_503_without_llm_key(monkeypatch, agent_identity):
    monkeypatch.delenv(copilot.API_KEY_ENV, raising=False)
    response = agent_api.route(
        agent_request("POST", "/api/agent/copilot/draft", {"prompt": "p"}),
        "POST", "/api/agent/copilot/draft",
    )
    assert response["statusCode"] == 503
    assert "COPILOT_LLM_API_KEY" in response["body"]


def test_agent_copilot_draft_502_when_llm_fails(monkeypatch, agent_identity):
    monkeypatch.setenv(copilot.API_KEY_ENV, "key")
    stub_llm(monkeypatch, error=copilot.LlmError("LLM request failed: HTTP 500 boom"))
    response = agent_api.route(
        agent_request("POST", "/api/agent/copilot/draft", {"prompt": "p"}),
        "POST", "/api/agent/copilot/draft",
    )
    assert response["statusCode"] == 502
    assert "HTTP 500" in response["body"]


# ---- Admin mirror ----

def admin_request(method, path, body=None):
    return {
        "requestContext": {"http": {"method": method, "path": path}},
        "headers": {"host": "dapier.example.test"},
        "cookies": [],
        "body": json.dumps(body) if body is not None else None,
    }


@pytest.fixture
def operator_session(monkeypatch):
    monkeypatch.setattr(session, "authenticated", lambda event: True)
    monkeypatch.setattr(session, "_csrf_ok", lambda event, method: True)
    monkeypatch.setattr(session, "require_operator", lambda event: ({"sub": "op-1"}, None))
    monkeypatch.setattr(session, "_audit_event", lambda *args, **kwargs: {"emitted": (args, kwargs)})


def test_admin_copilot_draft_drives_the_same_handler(monkeypatch, operator_session, never_saves):
    stub_llm(monkeypatch, reply=WORKFLOW_YAML)
    response = admin.route(
        admin_request("POST", "/api/admin/copilot/draft", {"prompt": "email me todos"}),
        "POST", "/api/admin/copilot/draft",
    )
    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["workflow"]["id"] == "copilot-draft"
    assert payload["errors"] == []


def test_admin_copilot_draft_requires_session(monkeypatch):
    monkeypatch.setattr(session, "authenticated", lambda event: False)
    response = admin.route(
        admin_request("POST", "/api/admin/copilot/draft", {"prompt": "p"}),
        "POST", "/api/admin/copilot/draft",
    )
    assert response["statusCode"] == 401


def test_admin_copilot_draft_requires_a_prompt(monkeypatch, operator_session):
    stub_llm(monkeypatch, reply=WORKFLOW_YAML)
    response = admin.route(
        admin_request("POST", "/api/admin/copilot/draft", {}),
        "POST", "/api/admin/copilot/draft",
    )
    assert response["statusCode"] == 400


# ---- Deterministic parsing helpers ----

def test_extract_yaml_handles_fence_variants():
    assert copilot.extract_yaml(FENCED_REPLY) == DRAFT_YAML
    assert copilot.extract_yaml(f"```\n{WORKFLOW_YAML}```") == DRAFT_YAML
    assert copilot.extract_yaml(f"```YAML\n{WORKFLOW_YAML}```") == DRAFT_YAML
    assert copilot.extract_yaml(WORKFLOW_YAML) == DRAFT_YAML
    assert copilot.extract_yaml("") == ""


def test_system_prompt_includes_bundled_examples():
    prompt = copilot.system_prompt()
    assert "render_html_to_pdf" in prompt  # action catalog
    for name in copilot.EXAMPLE_FILES:
        assert name in prompt


def test_draft_workflow_rejects_overlong_prompt(monkeypatch):
    stub_llm(monkeypatch, reply=WORKFLOW_YAML)
    status, payload = copilot.draft_workflow("x" * (copilot.MAX_PROMPT_CHARS + 1))
    assert status == 400


def test_llm_client_posts_chat_completions(monkeypatch):
    """The raw client: OpenAI-compatible wire format, bearer key, model env."""
    monkeypatch.setenv(copilot.API_KEY_ENV, "secret-key")
    monkeypatch.setenv(copilot.BASE_URL_ENV, "https://llm.example.test/v1/")
    monkeypatch.setenv(copilot.MODEL_ENV, "test-model")
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["body"] = json.loads(request.data)
        captured["timeout"] = timeout

        class Response:
            def read(self):
                return json.dumps({"choices": [{"message": {"content": "hello"}}]}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        return Response()

    monkeypatch.setattr(copilot.urllib.request, "urlopen", fake_urlopen)
    reply = copilot._llm_complete([{"role": "user", "content": "hi"}])
    assert reply == "hello"
    assert captured["url"] == "https://llm.example.test/v1/chat/completions"
    assert captured["body"]["model"] == "test-model"
    assert captured["body"]["messages"] == [{"role": "user", "content": "hi"}]
    headers = {key.lower(): value for key, value in captured["headers"].items()}
    assert headers["authorization"] == "Bearer secret-key"
    assert captured["timeout"] == copilot.LLM_TIMEOUT_SECONDS


def test_llm_client_wraps_http_errors(monkeypatch):
    monkeypatch.setenv(copilot.API_KEY_ENV, "key")

    def fake_urlopen(request, timeout=None):
        import urllib.error

        raise urllib.error.HTTPError(request.full_url, 401, "denied", {}, io.BytesIO(b"nope"))

    monkeypatch.setattr(copilot.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(copilot.LlmError, match="HTTP 401"):
        copilot._llm_complete([{"role": "user", "content": "hi"}])


# ---- CLI ----

def test_cli_workflows_draft_prints_yaml(monkeypatch, capsys):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path, body))
        return {"yaml": DRAFT_YAML, "workflow": {"id": "copilot-draft"}, "errors": []}

    monkeypatch.setattr(cli_commands.api, "call", fake_call)
    assert cli_commands.workflows_draft("https://api.example.test", "email me todos") == 0
    assert calls == [("POST", "/api/agent/copilot/draft", {"prompt": "email me todos"})]
    out, _ = capsys.readouterr()
    assert "id: copilot-draft" in out
    assert "Validation errors" not in out


def test_cli_workflows_draft_prints_errors(monkeypatch, capsys):
    monkeypatch.setattr(cli_commands.api, "call", lambda *args, **kwargs: {
        "yaml": INVALID_YAML, "workflow": None,
        "errors": ["connect at least one action to the trigger"],
    })
    assert cli_commands.workflows_draft("https://api.example.test", "broken") == 0
    out, _ = capsys.readouterr()
    assert "connect at least one action to the trigger" in out


def test_cli_workflows_draft_save_pipes_through_the_save_path(monkeypatch, capsys):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path, body))
        if method == "POST":
            assert path == "/api/agent/copilot/draft"
            return {"yaml": DRAFT_YAML, "workflow": {"id": "copilot-draft"}, "errors": []}
        assert (method, path) == ("PUT", "/api/agent/designer/workflows")
        assert body == {"yaml": DRAFT_YAML}
        return {"file": "copilot-draft.yaml", "commit": "abc1234", "published": True}

    monkeypatch.setattr(cli_commands.api, "call", fake_call)
    assert cli_commands.workflows_draft(
        "https://api.example.test", "email me todos", save=True) == 0
    assert [call[:2] for call in calls] == [
        ("POST", "/api/agent/copilot/draft"),
        ("PUT", "/api/agent/designer/workflows"),
    ]
    out, _ = capsys.readouterr()
    assert "Committed copilot-draft.yaml (abc1234) and published it live." in out


def test_cli_workflows_draft_save_refused_when_invalid(monkeypatch, capsys):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path))
        return {"yaml": INVALID_YAML, "workflow": None,
                "errors": ["connect at least one action to the trigger"]}

    monkeypatch.setattr(cli_commands.api, "call", fake_call)
    assert cli_commands.workflows_draft(
        "https://api.example.test", "broken", save=True) == 5
    assert calls == [("POST", "/api/agent/copilot/draft")]
    out, _ = capsys.readouterr()
    assert "Not saving" in out


def test_cli_workflows_draft_parser():
    from dapier_cli import main

    args = main.build_parser().parse_args(["workflows", "draft", "email me todos"])
    assert (args.group, args.command, args.prompt, args.save) == (
        "workflows", "draft", "email me todos", False)
    args = main.build_parser().parse_args(["workflows", "draft", "p", "--save"])
    assert args.save is True


# ---- Deploy wiring ----

def test_template_declares_copilot_llm_environment():
    text = open("template.yaml", encoding="utf-8").read()
    for parameter, env in (
        ("CopilotLlmBaseUrl", "COPILOT_LLM_BASE_URL"),
        ("CopilotLlmApiKey", "COPILOT_LLM_API_KEY"),
        ("CopilotLlmModel", "COPILOT_LLM_MODEL"),
    ):
        assert f"{parameter}:" in text
        assert f"{env}: !Ref {parameter}" in text
