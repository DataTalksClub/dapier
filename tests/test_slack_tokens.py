import pytest

from src.dapier.connections.providers import slack_tokens


class FakeTransport:
    def __init__(self, status=200, body=b'{"ok": true, "team_id": "T1", "team": "DataTalks", "user_id": "U2", "user": "dapier"}'):
        self.status = status
        self.body = body

    def __call__(self, method, url, *, headers, body, timeout=10):
        assert headers["authorization"].startswith("Bearer xoxb-")
        return self.status, self.body


def test_validate_token_accepts_bot_and_user_tokens():
    assert slack_tokens.validate_token(" xoxb-" + "a" * 30 + " ") == "xoxb-" + "a" * 30
    assert slack_tokens.validate_token("xoxp-" + "b" * 30)
    with pytest.raises(slack_tokens.SlackTokenError):
        slack_tokens.validate_token("not-a-token")
    with pytest.raises(slack_tokens.SlackTokenError):
        slack_tokens.validate_token("xoxb-short")


def test_verify_account_returns_workspace_identity():
    account_id, title = slack_tokens.verify_account("xoxb-" + "a" * 30, transport=FakeTransport())
    assert account_id == "T1"
    assert title == "DataTalks"


def test_verify_account_fails_closed():
    with pytest.raises(slack_tokens.SlackTokenError):
        slack_tokens.verify_account(
            "xoxb-" + "a" * 30,
            transport=FakeTransport(body=b'{"ok": false, "error": "invalid_auth"}'),
        )
    with pytest.raises(slack_tokens.SlackTokenError):
        slack_tokens.verify_account("xoxb-" + "a" * 30, transport=FakeTransport(status=500, body=b"boom"))


class SlackApi:
    """Answer Slack Web API methods by name; records the calls made."""

    def __init__(self, **methods):
        self.methods = methods
        self.calls = []

    def __call__(self, method, url, *, headers, body, timeout=10):
        name = url.rsplit("/", 1)[-1]
        self.calls.append((name, body))
        if name not in self.methods:
            return 200, b'{"ok": false, "error": "missing_scope"}'
        import json
        return 200, json.dumps({"ok": True, **self.methods[name]}).encode()


def test_describe_bot_token_names_the_app():
    api = SlackApi(**{
        "auth.test": {"team_id": "T1", "team": "DataTalks.Club", "user_id": "U9",
                      "user": "au-tomator", "bot_id": "B42"},
        "bots.info": {"bot": {"id": "B42", "name": "Au-Tomator"}},
    })
    identity = slack_tokens.describe_token("xoxb-" + "a" * 30, transport=api)
    assert identity == {"kind": "app", "name": "Au-Tomator"}
    assert ("bots.info", b"bot=B42") in api.calls
    assert slack_tokens.identity_label(identity) == "Au-Tomator (App)"


def test_describe_user_token_names_the_person():
    api = SlackApi(**{
        "auth.test": {"team_id": "T1", "team": "DataTalks.Club", "user_id": "U1",
                      "user": "alexey"},
        "users.info": {"user": {"id": "U1", "real_name": "Alexey Grigorev"}},
    })
    identity = slack_tokens.describe_token("xoxp-" + "a" * 30, transport=api)
    assert identity == {"kind": "user", "name": "Alexey Grigorev"}
    assert slack_tokens.identity_label(identity) == "Alexey Grigorev (User)"


def test_describe_token_falls_back_to_the_handle_then_the_prefix():
    # bots.info needs users:read; without it the auth.test handle names it.
    api = SlackApi(**{"auth.test": {"team_id": "T1", "user_id": "U9",
                                    "user": "au-tomator", "bot_id": "B42"}})
    assert slack_tokens.describe_token("xoxb-" + "a" * 30, transport=api) == {
        "kind": "app", "name": "au-tomator"}

    def offline(*_args, **_kwargs):
        raise OSError("down")

    # Slack unreachable: the kind still follows from the token prefix.
    assert slack_tokens.describe_token("xoxp-" + "a" * 30, transport=offline) == {
        "kind": "user", "name": None}
    assert slack_tokens.identity_label({"kind": "user", "name": None}) == "User token"
    assert slack_tokens.identity_label(None) == ""
