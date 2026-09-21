import json

from src import agent_api


class Table:
    def __init__(self, items=None):
        self.items = items or {}

    def _lookup(self, key):
        if "grantee" in key:
            return self.items.get((key.get("connection_id"), key.get("grantee")))
        return self.items.get(key.get("connection_id"))

    def get_item(self, **kwargs):
        item = self._lookup(kwargs["Key"])
        return {"Item": dict(item)} if item else {}

    def scan(self, **kwargs):
        return {"Items": list(self.items.values())}


def configure(monkeypatch, *, claims=None, connections=None, grants=None):
    agent_api.reset_rate_limits()
    tables = {
        "connections": Table(connections or {}),
        "grants": Table(grants or {}),
    }

    class Dynamo:
        def Table(self, name):
            return tables[name]

    import boto3

    monkeypatch.setenv("CONNECTIONS_TABLE", "connections")
    monkeypatch.setenv("GRANTS_TABLE", "grants")
    monkeypatch.setenv("AUTH_CLI_CLIENT_ID", "cli-client")
    monkeypatch.delenv("AUDIT_TABLE", raising=False)
    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())
    monkeypatch.setattr(
        agent_api, "verify_id_token",
        lambda token, audience=None: dict(claims) if claims is not None else (_ for _ in ()).throw(ValueError("bad")),
    )
    return tables


def event(body=None, token="dtc-id-token", query=None):
    headers = {"host": "dapier.example.test"}
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    request = {"headers": headers, "cookies": []}
    if body is not None:
        request["body"] = json.dumps(body)
    if query is not None:
        request["queryStringParameters"] = query
    return request


CONNECTION = {
    "connection_id": "youtube-personal",
    "provider": "youtube",
    "display_name": "Personal YouTube",
    "scopes": ["s"],
    "granted_scopes": ["s"],
    "expected_account_id": "UC1",
    "verified_account_id": "UC1",
    "account_title": "Ch",
    "status": "connected",
    "version": 5,
    "updated_at": "now",
    "connected_at": "now",
}

GRANT = {
    "connection_id": "youtube-personal",
    "grantee": "subject-1#buildcamp-uploader",
    "subject": "subject-1",
    "agent": "buildcamp-uploader",
    "operations": ["use"],
}


def test_cli_unconfigured(monkeypatch):
    configure(monkeypatch, claims={"sub": "subject-1"})
    monkeypatch.delenv("AUTH_CLI_CLIENT_ID")
    response = agent_api.route(event({"connection_id": "x", "agent": "a"}), "POST", "/api/agent/token")
    assert response["statusCode"] == 503


def test_missing_and_bad_identity(monkeypatch):
    configure(monkeypatch, claims={"sub": "subject-1"})
    assert agent_api.route(event({}, token=None), "POST", "/api/agent/token")["statusCode"] == 401
    configure(monkeypatch, claims=None)
    response = agent_api.route(event({"connection_id": "x", "agent": "a"}), "POST", "/api/agent/token")
    assert response["statusCode"] == 401


def test_token_unknown_connection_is_404(monkeypatch):
    configure(monkeypatch, claims={"sub": "subject-1"})
    response = agent_api.route(
        event({"connection_id": "nope", "agent": "buildcamp-uploader"}), "POST", "/api/agent/token",
    )
    assert response["statusCode"] == 404


def test_token_without_grant_is_denied(monkeypatch):
    configure(monkeypatch, claims={"sub": "subject-1"}, connections={"youtube-personal": CONNECTION})
    response = agent_api.route(
        event({"connection_id": "youtube-personal", "agent": "buildcamp-uploader"}),
        "POST", "/api/agent/token",
    )
    assert response["statusCode"] == 403


def test_token_wrong_agent_is_denied(monkeypatch):
    configure(monkeypatch, claims={"sub": "subject-1"},
              connections={"youtube-personal": CONNECTION},
              grants={("youtube-personal", "subject-1#buildcamp-uploader"): GRANT})
    response = agent_api.route(
        event({"connection_id": "youtube-personal", "agent": "other-agent"}),
        "POST", "/api/agent/token",
    )
    assert response["statusCode"] == 403


def test_token_success_returns_no_secrets(monkeypatch):
    configure(monkeypatch, claims={"sub": "subject-1"},
              connections={"youtube-personal": CONNECTION},
              grants={("youtube-personal", "subject-1#buildcamp-uploader"): GRANT})
    monkeypatch.setattr(
        agent_api.tokens, "get_access_token",
        lambda connection: ("live-access", {
            "expires_at": 123, "scope": "s", "provider_account_id": "UC1",
            "account_title": "Ch", "refreshed": False,
        }),
    )
    response = agent_api.route(
        event({"connection_id": "youtube-personal", "agent": "buildcamp-uploader"}),
        "POST", "/api/agent/token",
    )
    assert response["statusCode"] == 200
    assert response["headers"]["cache-control"] == "no-store"
    body = json.loads(response["body"])
    assert body["access_token"] == "live-access"
    assert body["provider_account_id"] == "UC1"
    assert "refresh_token" not in body
    assert "client_secret" not in body
    assert "refresh" not in response["body"] or '"refreshed": false' in response["body"]


def test_token_binding_conflict_is_409_and_refresh_failure_502(monkeypatch):
    from src.connections import BindingError
    from src.tokens import TokenError

    configure(monkeypatch, claims={"sub": "subject-1"},
              connections={"youtube-personal": CONNECTION},
              grants={("youtube-personal", "subject-1#buildcamp-uploader"): GRANT})

    monkeypatch.setattr(
        agent_api.tokens, "get_access_token",
        lambda connection: (_ for _ in ()).throw(BindingError("bound elsewhere")),
    )
    assert agent_api.route(
        event({"connection_id": "youtube-personal", "agent": "buildcamp-uploader"}),
        "POST", "/api/agent/token",
    )["statusCode"] == 409

    monkeypatch.setattr(
        agent_api.tokens, "get_access_token",
        lambda connection: (_ for _ in ()).throw(TokenError("down")),
    )
    assert agent_api.route(
        event({"connection_id": "youtube-personal", "agent": "buildcamp-uploader"}),
        "POST", "/api/agent/token",
    )["statusCode"] == 502


def test_token_rate_limited(monkeypatch):
    configure(monkeypatch, claims={"sub": "subject-1"},
              connections={"youtube-personal": CONNECTION},
              grants={("youtube-personal", "subject-1#buildcamp-uploader"): GRANT})
    monkeypatch.setenv("TOKEN_RATE_LIMIT", "1")
    monkeypatch.setattr(
        agent_api.tokens, "get_access_token",
        lambda connection: ("t", {"expires_at": 1, "scope": "", "provider_account_id": "UC1",
                                  "account_title": "", "refreshed": False}),
    )
    body = {"connection_id": "youtube-personal", "agent": "buildcamp-uploader"}
    assert agent_api.route(event(body), "POST", "/api/agent/token")["statusCode"] == 200
    assert agent_api.route(event(body), "POST", "/api/agent/token")["statusCode"] == 429


def test_list_and_show_require_grants(monkeypatch):
    configure(monkeypatch, claims={"sub": "subject-1"},
              connections={"youtube-personal": CONNECTION},
              grants={("youtube-personal", "subject-1#buildcamp-uploader"): GRANT})

    listed = agent_api.route(event(), "GET", "/api/agent/connections")
    assert listed["statusCode"] == 200
    items = json.loads(listed["body"])["connections"]
    assert len(items) == 1
    assert items[0]["verified_account_id"] == "UC1"
    assert "client_secret" not in json.dumps(items)

    shown = agent_api.route(
        event(query={"agent": "buildcamp-uploader"}), "GET", "/api/agent/connections/youtube-personal",
    )
    assert shown["statusCode"] == 200

    hidden = agent_api.route(
        event(query={"agent": "other-agent"}), "GET", "/api/agent/connections/youtube-personal",
    )
    assert hidden["statusCode"] == 404

    configure(monkeypatch, claims={"sub": "stranger"},
              connections={"youtube-personal": CONNECTION},
              grants={("youtube-personal", "subject-1#buildcamp-uploader"): GRANT})
    assert agent_api.route(event(), "GET", "/api/agent/connections")["statusCode"] == 200
    assert json.loads(agent_api.route(event(), "GET", "/api/agent/connections")["body"])["connections"] == []
    assert agent_api.route(event(), "GET", "/api/agent/connections/youtube-personal")["statusCode"] == 404
