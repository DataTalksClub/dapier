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
