"""CLI nouns: emails, webhooks, and workflows sample. triggers and hooks are aliases."""
import json

from dapier_cli import commands, main


def test_webhooks_list_and_hooks_list_call_the_hook_route(monkeypatch):
    seen = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        seen.append((method, path))
        return {"hooks": [], "base_url": "https://dapier.example.test", }

    monkeypatch.setattr(commands.api, "call", fake_call)
    assert main.main(["webhooks", "list"]) == 0
    assert main.main(["hooks", "list", "--kind", "telegram"]) == 0
    assert seen == [
        ("GET", "/api/agent/hook-triggers"),
        ("GET", "/api/agent/hook-triggers?kind=telegram"),
    ]


def test_workflows_sample_and_triggers_sample_share_flags_and_behavior(monkeypatch):
    seen = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        seen.append((method, path, body))
        return {"connector": "email", "event": "message.received", "source": "sample",
                "occurred_at": "t", "data": {"subject": "hi"}}

    monkeypatch.setattr(commands.api, "call", fake_call)
    assert main.main(["workflows", "sample", "email", "--event", "message.received"]) == 0
    assert main.main(["triggers", "sample", "email", "--event", "message.received"]) == 0
    assert seen[0] == seen[1]
    assert seen[0][0] == "POST"
    workflows = main.build_parser().parse_args(
        ["workflows", "sample", "email", "--workflow", "w", "--event", "e",
         "--connection-id", "c", "--limit", "3", "--resource", "slack.channels"])
    triggers = main.build_parser().parse_args(
        ["triggers", "sample", "email", "--workflow", "w", "--event", "e",
         "--connection-id", "c", "--limit", "3", "--resource", "slack.channels"])
    assert workflows.connector == triggers.connector == "email"
    assert workflows.workflow == triggers.workflow == "w"
    assert workflows.event == triggers.event == "e"
    assert workflows.connection_id == triggers.connection_id == "c"
    assert workflows.limit == triggers.limit == 3
    assert workflows.resource == triggers.resource == "slack.channels"


def test_help_moves_triggers_and_hooks_and_drops_agent_mail():
    parser = main.build_parser()
    top = parser.format_help()
    assert "agent-mail" not in top
    choices = [action for action in parser._subparsers._actions
               if getattr(action, "choices", None) and "triggers" in action.choices][0].choices
    assert "Moved" in choices["triggers"].format_help()
    assert "Moved" in choices["hooks"].format_help()
    webhooks_help = choices["webhooks"].format_help()
    assert "trigger" not in webhooks_help.lower()
    worker_help = choices["worker"].format_help()
    assert "API" not in worker_help
    emails_help = choices["emails"].format_help()
    for name in ("list", "show", "from"):
        assert name in emails_help
    for name in ("list", "show", "save", "delete"):
        assert name in webhooks_help
