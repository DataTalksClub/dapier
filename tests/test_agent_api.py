import json
import time
from datetime import datetime, timezone

from src.dapier.api import agent as agent_api
from src.dapier.connections import tokens


import pytest

from src.dapier.connections.providers import oauth_clients


@pytest.fixture(autouse=True)
def clean_oauth_client_cache():
    """The module-level client cache outlives a test; drop it around each one."""
    oauth_clients.invalidate_cache()
    yield
    oauth_clients.invalidate_cache()


class Table:
    def __init__(self, items=None):
        self.items = items or {}

    def _lookup(self, key):
        if "grantee" in key:
            return self.items.get((key.get("connection_id"), key.get("grantee")))
        if "token_hash" in key:
            return self.items.get(key.get("token_hash"))
        return self.items.get(key.get("connection_id"), self.items.get(key.get("credential_id")))

    def get_item(self, **kwargs):
        item = self._lookup(kwargs["Key"])
        return {"Item": dict(item)} if item else {}

    def put_item(self, **kwargs):
        item = kwargs["Item"]
        if "grantee" in item:
            self.items[(item["connection_id"], item["grantee"])] = item
        else:
            self.items[item.get("connection_id") or item.get("credential_id")
                       or item.get("token_hash")] = item

    def update_item(self, **kwargs):
        item = self._lookup(kwargs["Key"])
        if item is not None:
            item["last_used_at"] = kwargs["ExpressionAttributeValues"][":now"]

    def delete_item(self, **kwargs):
        key = kwargs["Key"]
        if "token_hash" in key:
            self.items.pop(key["token_hash"], None)
        elif "grantee" in key:
            self.items.pop((key["connection_id"], key["grantee"]), None)
        else:
            self.items.pop(key.get("connection_id"), None)

    def query(self, **kwargs):
        value = kwargs["ExpressionAttributeValues"][":connection"]
        return {"Items": [item for item in self.items.values()
                          if item.get("connection_id") == value]}

    def scan(self, **kwargs):
        return {"Items": list(self.items.values())}


def configure(monkeypatch, *, claims=None, connections=None, grants=None):
    agent_api.reset_rate_limits()
    tables = {
        "connections": Table(connections or {}),
        "grants": Table(grants or {}),
        "credentials": Table(),
        "api-tokens": Table(),
    }

    class Dynamo:
        def Table(self, name):
            return tables[name]

    import boto3

    monkeypatch.setenv("CONNECTIONS_TABLE", "connections")
    monkeypatch.setenv("GRANTS_TABLE", "grants")
    monkeypatch.setenv("CREDENTIALS_TABLE", "credentials")
    monkeypatch.setenv("API_TOKENS_TABLE", "api-tokens")
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
    from src.dapier.connections.records import BindingError
    from src.dapier.connections.tokens import TokenError

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
    monkeypatch.setenv("OPERATOR_EMAILS", "op@datatalks.club")

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


def test_connection_health_flags_expired_tokens(monkeypatch):
    tables = configure(monkeypatch, claims={"sub": "subject-1"},
                       connections={"youtube-personal": CONNECTION},
                       grants={("youtube-personal", "subject-1#buildcamp-uploader"): GRANT})
    monkeypatch.setenv("OPERATOR_EMAILS", "op@datatalks.club")
    tables["credentials"].put_item(Item={
        "credential_id": "oauth#youtube-personal", "provider": "google", "version": 1,
        "value": {"access_token": "at", "refresh_token": "rt", "expires_at": 1_000},
    })

    listed = json.loads(agent_api.route(event(), "GET", "/api/agent/connections")["body"])["connections"]
    assert listed[0]["health"] == "expired"
    assert listed[0]["token_expires_at"]

    shown = json.loads(agent_api.route(event(), "GET", "/api/agent/connections/youtube-personal")["body"])
    assert shown["health"] == "expired"

    tables["credentials"].put_item(Item={
        "credential_id": "oauth#youtube-personal", "provider": "google", "version": 2,
        "value": {"access_token": "at", "refresh_token": "rt",
                  "expires_at": int(time.time()) + 3600},
    })
    fresh = json.loads(agent_api.route(event(), "GET", "/api/agent/connections")["body"])["connections"]
    assert fresh[0]["health"] == "ok"


def test_operator_can_create_and_edit_oauth_connection_metadata(monkeypatch):
    tables = configure(monkeypatch, claims={"sub": "op-1", "email": "op@datatalks.club"})
    scopes = ["https://www.googleapis.com/auth/youtube.readonly"]
    created = agent_api.route(
        event({"connection_id": "youtube-team", "provider": "youtube",
               "display_name": "Team YouTube", "scopes": scopes}),
        "PUT", "/api/agent/connections",
    )
    assert created["statusCode"] == 200
    assert json.loads(created["body"])["status"] == "ready"

    edited = agent_api.route(
        event({"display_name": "DTC YouTube", "scopes": scopes}),
        "PUT", "/api/agent/connections/youtube-team",
    )
    assert edited["statusCode"] == 200
    view = json.loads(edited["body"])
    assert view["display_name"] == "DTC YouTube"
    assert view["scopes"] == scopes
    assert tables["connections"].items["youtube-team"]["status"] == "ready"


def test_operator_can_show_connection_without_agent_grant(monkeypatch):
    configure(monkeypatch, claims={"sub": "op-1", "email": "op@datatalks.club"},
              connections={"youtube-personal": CONNECTION})
    response = agent_api.route(
        event(), "GET", "/api/agent/connections/youtube-personal",
    )
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["connection_id"] == "youtube-personal"


def test_show_connection_serializes_dynamodb_decimals(monkeypatch):
    from decimal import Decimal

    connection = {**CONNECTION, "version": Decimal("5"), "scopes": [Decimal("1")]}
    configure(monkeypatch, claims={"sub": "op-1", "email": "op@datatalks.club"},
              connections={"youtube-personal": connection})
    response = agent_api.route(
        event(), "GET", "/api/agent/connections/youtube-personal",
    )
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["version"] == 5


def test_show_connection_serializes_dynamodb_decimals(monkeypatch):
    from decimal import Decimal

    connection = {**CONNECTION, "version": Decimal("5"), "scopes": [Decimal("1")]}
    configure(monkeypatch, claims={"sub": "op-1", "email": "op@datatalks.club"},
              connections={"youtube-personal": connection})
    response = agent_api.route(
        event(), "GET", "/api/agent/connections/youtube-personal",
    )
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["version"] == 5


def test_create_connection_rejects_duplicate_and_token_provider(monkeypatch):
    configure(monkeypatch, claims={"sub": "op-1", "email": "op@datatalks.club"},
              connections={"youtube-personal": CONNECTION})
    duplicate = agent_api.route(
        event({"connection_id": "youtube-personal", "provider": "youtube",
               "scopes": ["https://www.googleapis.com/auth/youtube.readonly"]}),
        "PUT", "/api/agent/connections",
    )
    assert duplicate["statusCode"] == 409
    token_provider = agent_api.route(
        event({"connection_id": "slack-team", "provider": "slack"}),
        "PUT", "/api/agent/connections",
    )
    assert token_provider["statusCode"] == 400


def test_operator_endpoints_reject_non_operators(monkeypatch):
    configure(monkeypatch, claims={"sub": "subject-1", "email": "agent@example.test"})
    monkeypatch.setenv("OPERATOR_EMAILS", "op@datatalks.club")
    requests = (
        (event(), "GET", "/api/agent/overview"),
        (event(), "GET", "/api/agent/grants"),
        (event({"token": "xoxb-" + "a" * 20}), "PUT", "/api/agent/credentials/slack"),
        (event(), "DELETE", "/api/agent/connections/youtube-personal/tokens"),
        (event({"connection_id": "youtube-team", "provider": "youtube",
                "scopes": ["https://www.googleapis.com/auth/youtube.readonly"]}),
         "PUT", "/api/agent/connections"),
        (event(), "GET", "/api/agent/oauth-clients"),
        (event({"client_id": "i", "client_secret": "s"}), "PUT", "/api/agent/oauth-clients/google"),
    )
    for request, method, path in requests:
        assert agent_api.route(request, method, path)["statusCode"] == 403


def test_grants_roundtrip_over_bearer(monkeypatch):
    configure(monkeypatch, claims={"sub": "op-1", "email": "op@datatalks.club"},
              connections={"youtube-personal": CONNECTION})

    saved = agent_api.route(
        event({"connection_id": "youtube-personal", "subject": "subject-9",
               "agent": "buildcamp-uploader", "operations": ["use"]}),
        "PUT", "/api/agent/grants",
    )
    assert saved["statusCode"] == 200
    assert json.loads(saved["body"])["grantee"] == "subject-9#buildcamp-uploader"

    listed = agent_api.route(event(), "GET", "/api/agent/grants")
    assert listed["statusCode"] == 200
    assert [g["grantee"] for g in json.loads(listed["body"])["grants"]] == [
        "subject-9#buildcamp-uploader",
    ]

    filtered = agent_api.route(
        event(query={"connection_id": "youtube-personal"}), "GET", "/api/agent/grants",
    )
    assert json.loads(filtered["body"])["grants"]

    revoked = agent_api.route(
        event(query={"connection_id": "youtube-personal",
                     "grantee": "subject-9#buildcamp-uploader"}),
        "DELETE", "/api/agent/grants",
    )
    assert revoked["statusCode"] == 200
    assert json.loads(agent_api.route(event(), "GET", "/api/agent/grants")["body"])["grants"] == []


def test_grants_put_validates_body_and_connection(monkeypatch):
    configure(monkeypatch, claims={"sub": "op-1", "email": "op@datatalks.club"},
              connections={"youtube-personal": CONNECTION})
    missing = agent_api.route(event({"connection_id": " ", "subject": ""}), "PUT", "/api/agent/grants")
    assert missing["statusCode"] == 400
    unknown = agent_api.route(
        event({"connection_id": "nope", "subject": "s", "agent": "a"}), "PUT", "/api/agent/grants",
    )
    assert unknown["statusCode"] == 404
    bad_delete = agent_api.route(event(query={"connection_id": "youtube-personal"}),
                                 "DELETE", "/api/agent/grants")
    assert bad_delete["statusCode"] == 400


def test_credentials_set_is_write_only(monkeypatch):
    configure(monkeypatch, claims={"sub": "op-1", "email": "op@datatalks.club"})
    token = "xoxb-123456789012345678901234"

    saved = agent_api.route(event({"token": token}), "PUT", "/api/agent/credentials/slack")
    assert saved["statusCode"] == 200
    assert token not in saved["body"]

    invalid = agent_api.route(event({"token": "nope"}), "PUT", "/api/agent/credentials/slack")
    assert invalid["statusCode"] == 400

    unknown = agent_api.route(
        event({"api_key": "a" * 20 + "-us1"}), "PUT", "/api/agent/credentials/youtube",
    )
    assert unknown["statusCode"] == 404


def test_overview_mirrors_console_payload(monkeypatch, tmp_path):
    from src.dapier.triggers import published_workflows
    tables = configure(monkeypatch, claims={"sub": "op-1", "email": "op@datatalks.club"},
                       connections={"youtube-personal": CONNECTION})
    tables["executions"] = Table()
    monkeypatch.setenv("EXECUTIONS_TABLE", "executions")
    monkeypatch.setenv(published_workflows.TABLE_ENV, "published-test")
    monkeypatch.setattr(published_workflows, "load_items", lambda table_ref=None: [{
        "workflow_id": "demo", "file": "demo.yaml",
        "workflow": {"id": "demo", "trigger": {"connector": "webhook",
                                                "event": "request.received"},
                     "actions": [{"type": "webhook", "url": "https://example.test"}]},
    }])

    response = agent_api.route(event(), "GET", "/api/agent/overview")
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["service"] == "dapier"
    assert [w["id"] for w in body["workflows"]] == ["demo"]
    assert [c["connection_id"] for c in body["connections"]] == ["youtube-personal"]


def test_revoke_tokens_marks_connection_revoked(monkeypatch):
    configure(monkeypatch, claims={"sub": "op-1", "email": "op@datatalks.club"},
              connections={"youtube-personal": CONNECTION})
    assert agent_api.route(event(), "DELETE", "/api/agent/connections/nope/tokens")["statusCode"] == 404

    response = agent_api.route(event(), "DELETE", "/api/agent/connections/youtube-personal/tokens")
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["status"] == tokens.STATUS_REVOKED


def test_oauth_clients_roundtrip_over_bearer(monkeypatch):
    configure(monkeypatch, claims={"sub": "op-1", "email": "op@datatalks.club"})
    monkeypatch.delenv("DAPIER_SKIP_CONFIG_DB", raising=False)

    saved = agent_api.route(
        event({"client_id": "dropbox-app-id", "client_secret": "dropbox-secret-value"}),
        "PUT", "/api/agent/oauth-clients/dropbox",
    )
    assert saved["statusCode"] == 200
    assert json.loads(saved["body"]) == {
        "provider": "dropbox", "client_id": "dropbox-app-id",
        "source": "config", "configured": True,
    }
    assert "dropbox-secret-value" not in saved["body"]

    alias = agent_api.route(
        event({"client_id": "g-id", "client_secret": "g-secret-value"}),
        "PUT", "/api/agent/oauth-clients/youtube",
    )
    assert alias["statusCode"] == 200
    assert json.loads(alias["body"])["provider"] == "google"

    listed = agent_api.route(event(), "GET", "/api/agent/oauth-clients")
    assert listed["statusCode"] == 200
    clients = {item["provider"]: item for item in json.loads(listed["body"])["clients"]}
    assert set(clients) == {"dropbox", "google", "zoom"}
    assert clients["dropbox"]["source"] == "config"
    assert clients["google"]["client_id"] == "g-id"

    missing = agent_api.route(event({"client_id": "only-id"}), "PUT", "/api/agent/oauth-clients/google")
    assert missing["statusCode"] == 400
    unknown = agent_api.route(
        event({"client_id": "i", "client_secret": "s"}), "PUT", "/api/agent/oauth-clients/slack",
    )
    assert unknown["statusCode"] == 404


# --- API tokens: machine-principal bearer authentication ---

TOKEN_GRANT = {
    "connection_id": "youtube-personal",
    "grantee": "token:buildcamp-token#buildcamp-uploader",
    "subject": "token:buildcamp-token",
    "agent": "buildcamp-uploader",
    "operations": ["use"],
}


def _issue_token(tables, token_id="buildcamp-token", agent="buildcamp-uploader"):
    _, payload = agent_api.api_tokens.api_create(
        {"token_id": token_id, "agent": agent}, "operator-1",
        table_ref=tables["api-tokens"],
    )
    assert "token" in payload, payload
    return payload["token"]


def test_api_token_bearer_issues_connection_token(monkeypatch):
    tables = configure(monkeypatch, claims={"sub": "subject-1"},
                       connections={"youtube-personal": CONNECTION},
                       grants={("youtube-personal", TOKEN_GRANT["grantee"]): TOKEN_GRANT})
    secret = _issue_token(tables)
    monkeypatch.setattr(
        agent_api.tokens, "get_access_token",
        lambda connection: ("live-access", {
            "expires_at": 123, "scope": "s", "provider_account_id": "UC1",
            "account_title": "Ch", "refreshed": False,
        }),
    )

    response = agent_api.route(
        event({"connection_id": "youtube-personal", "agent": "buildcamp-uploader"},
              token=secret),
        "POST", "/api/agent/token",
    )

    assert response["statusCode"] == 200
    assert response["headers"]["cache-control"] == "no-store"
    body = json.loads(response["body"])
    assert body["access_token"] == "live-access"
    stored = tables["api-tokens"].items
    assert all(item["token_hash"] != secret for item in stored.values())
    assert any(item.get("last_used_at") for item in stored.values())


def test_api_token_is_bound_to_its_agent(monkeypatch):
    tables = configure(monkeypatch, claims={"sub": "subject-1"},
                       connections={"youtube-personal": CONNECTION},
                       grants={("youtube-personal", TOKEN_GRANT["grantee"]): TOKEN_GRANT})
    secret = _issue_token(tables)

    response = agent_api.route(
        event({"connection_id": "youtube-personal", "agent": "other-agent"}, token=secret),
        "POST", "/api/agent/token",
    )

    assert response["statusCode"] == 403
    assert "buildcamp-uploader" in response["body"]


def test_api_token_without_grant_is_denied(monkeypatch):
    tables = configure(monkeypatch, claims={"sub": "subject-1"},
                       connections={"youtube-personal": CONNECTION})
    secret = _issue_token(tables)

    response = agent_api.route(
        event({"connection_id": "youtube-personal", "agent": "buildcamp-uploader"},
              token=secret),
        "POST", "/api/agent/token",
    )

    assert response["statusCode"] == 403


def test_revoked_api_token_stops_authenticating(monkeypatch):
    tables = configure(monkeypatch, claims={"sub": "subject-1"},
                       connections={"youtube-personal": CONNECTION},
                       grants={("youtube-personal", TOKEN_GRANT["grantee"]): TOKEN_GRANT})
    secret = _issue_token(tables)
    _, duplicate = agent_api.api_tokens.api_create(
        {"token_id": "buildcamp-token", "agent": "buildcamp-uploader"}, "operator-1",
        table_ref=tables["api-tokens"],
    )
    assert "already exists" in duplicate.get("error", "")
    agent_api.api_tokens.api_revoke("buildcamp-token", table_ref=tables["api-tokens"])

    response = agent_api.route(
        event({"connection_id": "youtube-personal", "agent": "buildcamp-uploader"},
              token=secret),
        "POST", "/api/agent/token",
    )

    assert response["statusCode"] == 401


def test_api_token_needs_no_cli_client_configuration(monkeypatch):
    tables = configure(monkeypatch, claims=None,
                       connections={"youtube-personal": CONNECTION},
                       grants={("youtube-personal", TOKEN_GRANT["grantee"]): TOKEN_GRANT})
    monkeypatch.delenv("AUTH_CLI_CLIENT_ID")
    secret = _issue_token(tables)
    monkeypatch.setattr(
        agent_api.tokens, "get_access_token",
        lambda connection: ("live-access", {
            "expires_at": 123, "scope": "s", "provider_account_id": "UC1",
            "account_title": "Ch", "refreshed": False,
        }),
    )

    response = agent_api.route(
        event({"connection_id": "youtube-personal", "agent": "buildcamp-uploader"},
              token=secret),
        "POST", "/api/agent/token",
    )

    assert response["statusCode"] == 200


def test_unknown_api_token_is_401(monkeypatch):
    configure(monkeypatch, claims={"sub": "subject-1"})

    response = agent_api.route(
        event({"connection_id": "youtube-personal", "agent": "buildcamp-uploader"},
              token="dap_entirely-unknown-value"),
        "POST", "/api/agent/token",
    )

    assert response["statusCode"] == 401


def test_api_token_never_qualifies_as_operator(monkeypatch):
    """An empty operator allowlist admits every DTC account; a machine
    token must not inherit that, or it could mint tokens and grants."""
    tables = configure(monkeypatch, claims={"sub": "subject-1"})
    secret = _issue_token(tables)

    listed = agent_api.route(event(token=secret), "GET", "/api/agent/tokens")
    created = agent_api.route(
        event({"token_id": "escalate", "agent": "escalate"}, token=secret),
        "PUT", "/api/agent/tokens",
    )
    grants = agent_api.route(event(token=secret), "GET", "/api/agent/grants")

    assert listed["statusCode"] == 403
    assert created["statusCode"] == 403
    assert grants["statusCode"] == 403


def test_operator_token_lifecycle_over_bearer(monkeypatch):
    configure(monkeypatch, claims={"sub": "subject-1"})

    created = agent_api.route(
        event({"token_id": "personal-scheduler", "agent": "personal-scheduler"}),
        "PUT", "/api/agent/tokens",
    )
    assert created["statusCode"] == 200
    body = json.loads(created["body"])
    assert body["token"].startswith("dap_")
    assert body["subject"] == "token:personal-scheduler"

    listed = agent_api.route(event(), "GET", "/api/agent/tokens")
    items = json.loads(listed["body"])["tokens"]
    assert items == [{key: body[key] for key in (
        "token_id", "token_prefix", "agent", "subject",
        "created_by", "created_at", "last_used_at", "revoked_at")}]

    revoked = agent_api.route(
        event(query={"token_id": "personal-scheduler"}), "DELETE", "/api/agent/tokens")
    assert revoked["statusCode"] == 200
    assert json.loads(revoked["body"])["revoked_at"]

    missing = agent_api.route(
        event(query={"token_id": "never-existed"}), "DELETE", "/api/agent/tokens")
    assert missing["statusCode"] == 404


def test_operator_token_purge_removes_revoked_token_and_grants(monkeypatch):
    tables = configure(monkeypatch, claims={"sub": "subject-1"})

    created = agent_api.route(
        event({"token_id": "personal-scheduler", "agent": "personal-scheduler"}),
        "PUT", "/api/agent/tokens",
    )
    assert created["statusCode"] == 200
    tables["grants"].put_item(Item={
        "connection_id": "yt-one",
        "grantee": "token:personal-scheduler#personal-scheduler",
        "subject": "token:personal-scheduler",
        "agent": "personal-scheduler",
        "operations": ["use"],
    })

    active = agent_api.route(
        event(query={"token_id": "personal-scheduler", "purge": "1"}),
        "DELETE", "/api/agent/tokens")
    assert active["statusCode"] == 409
    assert tables["api-tokens"].items

    agent_api.route(
        event(query={"token_id": "personal-scheduler"}), "DELETE", "/api/agent/tokens")
    purged = agent_api.route(
        event(query={"token_id": "personal-scheduler", "purge": "1"}),
        "DELETE", "/api/agent/tokens")
    assert purged["statusCode"] == 200
    assert json.loads(purged["body"])["grants_removed"] == 1
    assert not tables["api-tokens"].items
    assert not tables["grants"].items


def _configure_runs_table(monkeypatch, items):
    class RunsTable:
        def scan(self, **kwargs):
            return {"Items": items}

        def query(self, **kwargs):
            values = list((kwargs.get("ExpressionAttributeValues") or {}).values())
            wanted = values[0] if values else None
            return {"Items": [item for item in items if item.get("run_id") == wanted]}

    monkeypatch.setattr(agent_api.runs, "_table", lambda: RunsTable())


def test_runs_list_over_bearer_requires_operator(monkeypatch):
    configure(monkeypatch, claims={"sub": "subject-1", "email": "agent@example.test"})
    monkeypatch.setenv("OPERATOR_EMAILS", "op@datatalks.club")

    listed = agent_api.route(event(), "GET", "/api/agent/runs")

    assert listed["statusCode"] == 403


def test_runs_list_and_detail_over_bearer(monkeypatch):
    configure(monkeypatch, claims={"sub": "op-1", "email": "op@datatalks.club"})
    monkeypatch.setenv("OPERATOR_EMAILS", "op@datatalks.club")
    steps = [
        {"execution_id": "wf-1:post:evt-1", "run_id": "wf-1:evt-1", "workflow_id": "wf-1",
         "action_id": "post", "action_type": "webhook", "connector": "email",
         "event_type": "message.received", "correlation_id": "evt-1", "status": "completed",
         "started_at": "2026-09-25T10:00:00+00:00", "finished_at": "2026-09-25T10:00:01+00:00",
         "duration_ms": 980, "input": {"subject": "invoice"}, "output": {"status": 200}},
        {"execution_id": "wf-1:notify:evt-1", "run_id": "wf-1:evt-1", "workflow_id": "wf-1",
         "action_id": "notify", "action_type": "slack", "connector": "email",
         "event_type": "message.received", "correlation_id": "evt-1", "status": "failed",
         "started_at": "2026-09-25T10:00:01+00:00", "finished_at": "2026-09-25T10:00:02+00:00",
         "duration_ms": 340, "input": {"subject": "invoice"}, "error": "Slack rejected message"},
    ]
    _configure_runs_table(monkeypatch, steps)

    listed = agent_api.route(event(query={"limit": "5"}), "GET", "/api/agent/runs")
    assert listed["statusCode"] == 200
    body = json.loads(listed["body"])
    assert body["runs"][0]["run_id"] == "wf-1:evt-1"
    assert body["runs"][0]["status"] == "failed"
    assert body["runs"][0]["steps"] == 2

    detail = agent_api.route(event(), "GET", "/api/agent/runs/wf-1%3Aevt-1")
    assert detail["statusCode"] == 200
    flow = json.loads(detail["body"])
    assert [step["action_id"] for step in flow["steps"]] == ["post", "notify"]
    assert flow["steps"][1]["error"] == "Slack rejected message"
    assert flow["steps"][0]["output"] == {"status": 200}

    missing = agent_api.route(event(), "GET", "/api/agent/runs/wf-1:missing")
    assert missing["statusCode"] == 404


def _configure_runs_table(monkeypatch, items):
    class RunsTable:
        def scan(self, **kwargs):
            return {"Items": items}

        def query(self, **kwargs):
            values = list((kwargs.get("ExpressionAttributeValues") or {}).values())
            wanted = values[0] if values else None
            return {"Items": [item for item in items if item.get("run_id") == wanted]}

    monkeypatch.setattr(agent_api.runs, "_table", lambda: RunsTable())


def test_runs_list_over_bearer_requires_operator(monkeypatch):
    configure(monkeypatch, claims={"sub": "subject-1", "email": "agent@example.test"})
    monkeypatch.setenv("OPERATOR_EMAILS", "op@datatalks.club")

    listed = agent_api.route(event(), "GET", "/api/agent/runs")

    assert listed["statusCode"] == 403


def test_runs_list_and_detail_over_bearer(monkeypatch):
    configure(monkeypatch, claims={"sub": "op-1", "email": "op@datatalks.club"})
    monkeypatch.setenv("OPERATOR_EMAILS", "op@datatalks.club")
    steps = [
        {"execution_id": "wf-1:post:evt-1", "run_id": "wf-1:evt-1", "workflow_id": "wf-1",
         "action_id": "post", "action_type": "webhook", "connector": "email",
         "event_type": "message.received", "correlation_id": "evt-1", "status": "completed",
         "started_at": "2026-09-25T10:00:00+00:00", "finished_at": "2026-09-25T10:00:01+00:00",
         "duration_ms": 980, "input": {"subject": "invoice"}, "output": {"status": 200}},
        {"execution_id": "wf-1:notify:evt-1", "run_id": "wf-1:evt-1", "workflow_id": "wf-1",
         "action_id": "notify", "action_type": "slack", "connector": "email",
         "event_type": "message.received", "correlation_id": "evt-1", "status": "failed",
         "started_at": "2026-09-25T10:00:01+00:00", "finished_at": "2026-09-25T10:00:02+00:00",
         "duration_ms": 340, "input": {"subject": "invoice"}, "error": "Slack rejected message"},
    ]
    _configure_runs_table(monkeypatch, steps)

    listed = agent_api.route(event(query={"limit": "5"}), "GET", "/api/agent/runs")
    assert listed["statusCode"] == 200
    body = json.loads(listed["body"])
    assert body["runs"][0]["run_id"] == "wf-1:evt-1"
    assert body["runs"][0]["status"] == "failed"
    assert body["runs"][0]["steps"] == 2

    detail = agent_api.route(event(), "GET", "/api/agent/runs/wf-1%3Aevt-1")
    assert detail["statusCode"] == 200
    flow = json.loads(detail["body"])
    assert [step["action_id"] for step in flow["steps"]] == ["post", "notify"]
    assert flow["steps"][1]["error"] == "Slack rejected message"
    assert flow["steps"][0]["output"] == {"status": 200}

    missing = agent_api.route(event(), "GET", "/api/agent/runs/wf-1:missing")
    assert missing["statusCode"] == 404


def _configure_replay_queue(monkeypatch):
    monkeypatch.setenv("EVENT_QUEUE_URL", "https://sqs.test/events")
    calls = []

    class Queue:
        def send_message(self, **kwargs):
            calls.append(kwargs)
            return {"MessageId": "sqsm-1"}

    monkeypatch.setattr(agent_api.runs, "_queue", lambda: Queue())
    return calls


def test_runs_replay_over_bearer_requires_operator(monkeypatch):
    configure(monkeypatch, claims={"sub": "subject-1", "email": "agent@example.test"})
    monkeypatch.setenv("OPERATOR_EMAILS", "op@datatalks.club")
    _configure_runs_table(monkeypatch, [])

    replayed = agent_api.route(event(), "POST", "/api/agent/runs/wf-1:evt-1/replay")

    assert replayed["statusCode"] == 403


def test_runs_replay_over_bearer_reinjects_the_original_event(monkeypatch):
    configure(monkeypatch, claims={"sub": "op-1", "email": "op@datatalks.club"})
    monkeypatch.setenv("OPERATOR_EMAILS", "op@datatalks.club")
    _configure_runs_table(monkeypatch, [
        {"execution_id": "wf-1:post:evt-1", "run_id": "wf-1:evt-1", "workflow_id": "wf-1",
         "action_id": "post", "connector": "email", "event_type": "message.received",
         "status": "failed", "started_at": "2026-09-25T10:00:00+00:00",
         "finished_at": "2026-09-25T10:00:01+00:00", "input": {"route": "invoice"}},
    ])
    calls = _configure_replay_queue(monkeypatch)

    replayed = agent_api.route(event(), "POST", "/api/agent/runs/wf-1%3Aevt-1/replay")

    assert replayed["statusCode"] == 202
    body = json.loads(replayed["body"])
    assert body["accepted"] is True
    assert body["replayed_from"] == "wf-1:evt-1"
    assert body["run_id"].startswith("wf-1:replay-")
    envelope = json.loads(calls[0]["MessageBody"])
    assert envelope["id"].startswith("replay-")
    assert envelope["correlation_id"] == "evt-1"
    assert envelope["connector"] == "email"
    assert envelope["data"] == {"route": "invoice"}

    missing = agent_api.route(event(), "POST", "/api/agent/runs/wf-1:missing/replay")
    assert missing["statusCode"] == 404


def test_runs_replay_from_step_over_bearer(monkeypatch):
    configure(monkeypatch, claims={"sub": "op-1", "email": "op@datatalks.club"})
    monkeypatch.setenv("OPERATOR_EMAILS", "op@datatalks.club")
    _configure_runs_table(monkeypatch, [
        {"execution_id": "wf-1:fetch:evt-1", "run_id": "wf-1:evt-1", "workflow_id": "wf-1",
         "action_id": "fetch", "connector": "email", "event_type": "message.received",
         "status": "completed", "started_at": "2026-09-25T10:00:00+00:00",
         "finished_at": "2026-09-25T10:00:01+00:00", "input": {"route": "invoice"},
         "output": {"rows": 3}},
        {"execution_id": "wf-1:post:evt-1", "run_id": "wf-1:evt-1", "workflow_id": "wf-1",
         "action_id": "post", "connector": "email", "event_type": "message.received",
         "status": "failed", "started_at": "2026-09-25T10:00:01+00:00",
         "finished_at": "2026-09-25T10:00:02+00:00", "input": {"route": "invoice"},
         "error": "boom"},
    ])
    calls = _configure_replay_queue(monkeypatch)
    monkeypatch.setattr(agent_api.runs, "_workflows_now", lambda: [{
        "id": "wf-1", "enabled": True,
        "actions": [{"id": "fetch", "type": "webhook"},
                    {"id": "post", "type": "webhook"}],
    }])

    replayed = agent_api.route(event(body={"from_step": "post"}),
                               "POST", "/api/agent/runs/wf-1%3Aevt-1/replay")

    assert replayed["statusCode"] == 202
    body = json.loads(replayed["body"])
    assert body["accepted"] is True
    assert body["from_step"] == "post"
    assert body["run_id"].startswith("wf-1:replay-")
    resume = json.loads(calls[0]["MessageBody"])["dapier_resume"]
    assert [step["id"] for step in resume["segments"][0]["steps"]] == ["post"]
    assert resume["event"]["correlation_id"] == "evt-1"
    assert resume["step_outputs"]["fetch"]["output"] == {"rows": 3}

    # a step the workflow does not have cannot start a replay
    refused = agent_api.route(event(body={"from_step": "nope"}),
                              "POST", "/api/agent/runs/wf-1%3Aevt-1/replay")
    assert refused["statusCode"] == 404


def test_runs_list_over_bearer_content_search(monkeypatch):
    configure(monkeypatch, claims={"sub": "op-1", "email": "op@datatalks.club"})
    monkeypatch.setenv("OPERATOR_EMAILS", "op@datatalks.club")
    _configure_runs_table(monkeypatch, [
        {"execution_id": "wf-1:post:evt-1", "run_id": "wf-1:evt-1", "workflow_id": "wf-1",
         "action_id": "post", "connector": "email", "event_type": "message.received",
         "status": "completed", "started_at": "2026-09-25T10:00:00+00:00",
         "input": {"subject": "invoice"}, "output": {"permalink": "order-1234"}},
        {"execution_id": "wf-2:post:evt-2", "run_id": "wf-2:evt-2", "workflow_id": "wf-2",
         "action_id": "post", "connector": "email", "event_type": "message.received",
         "status": "completed", "started_at": "2026-09-26T10:00:00+00:00",
         "input": {"subject": "hello"}, "output": {"status": 200}},
    ])

    listed = agent_api.route(event(query={"q": "order-1234"}), "GET", "/api/agent/runs")

    assert listed["statusCode"] == 200
    body = json.loads(listed["body"])
    assert [run["run_id"] for run in body["runs"]] == ["wf-1:evt-1"]
    assert body["paging"]["filtered"] is True


def test_poll_triggers_over_bearer_requires_operator(monkeypatch):
    configure(monkeypatch, claims={"sub": "agent-1", "email": "agent@example.test"})
    monkeypatch.setenv("OPERATOR_EMAILS", "op@datatalks.club")

    listed = agent_api.route(event(), "GET", "/api/agent/poll-triggers")

    assert listed["statusCode"] == 403


def test_poll_trigger_save_list_delete_over_bearer(monkeypatch):
    configure(monkeypatch, claims={"sub": "op-1", "email": "op@datatalks.club"})
    monkeypatch.setenv("OPERATOR_EMAILS", "op@datatalks.club")

    from src.dapier.triggers import poll_triggers

    class PollTable:
        def __init__(self):
            self.items = {}

        def scan(self, **_kwargs):
            return {"Items": [dict(item) for item in self.items.values()]}

        def get_item(self, Key):
            return {"Item": dict(self.items[Key["poll_id"]])} if Key["poll_id"] in self.items else {}

        def put_item(self, Item):
            self.items[Item["poll_id"]] = dict(Item)

        def delete_item(self, Key):
            self.items.pop(Key["poll_id"], None)

    class CursorTable:
        def __init__(self):
            self.items = {}

        def get_item(self, Key):
            return {"Item": dict(self.items[Key["cursor_id"]])} if Key["cursor_id"] in self.items else {}

        def put_item(self, Item):
            self.items[Key["cursor_id"]] = dict(Item)

        def delete_item(self, Key):
            self.items.pop(Key["cursor_id"], None)

    table, cursors = PollTable(), CursorTable()
    rules, removed = [], []
    monkeypatch.setattr(poll_triggers, "get_table", lambda table_ref=None: table_ref or table)
    monkeypatch.setattr(poll_triggers, "cursor_table", lambda table_ref=None: cursors)
    monkeypatch.setattr(poll_triggers, "sync_rule",
                        lambda item, **_kwargs: rules.append(item["poll_id"]))
    monkeypatch.setattr(poll_triggers, "remove_rule",
                        lambda poll_id, **_kwargs: removed.append(poll_id))

    body = {"name": "drive-updates", "expression": "rate(1 hour)",
            "url": "https://example.test/list", "list_path": "data.items",
            "id_path": "createdTime",
            "actions": [{"type": "slack", "channel": "#news", "text": "new item"}]}
    saved = agent_api.route(event(body), "PUT", "/api/agent/poll-triggers")

    assert saved["statusCode"] == 200
    assert json.loads(saved["body"])["created"] is True
    assert rules == ["drive-updates"]

    listed = agent_api.route(event(), "GET", "/api/agent/poll-triggers")

    assert [item["poll_id"] for item in json.loads(listed["body"])["polls"]] == ["drive-updates"]

    deleted = agent_api.route(event(query={"name": "drive-updates"}), "DELETE",
                              "/api/agent/poll-triggers")

    assert deleted["statusCode"] == 200
    assert table.items == {} and removed == ["drive-updates"]




def test_zoom_oauth_connection_issues_tokens(monkeypatch):
    """A Zoom meeting connection is a regular OAuth connection: once granted,
    the agent token endpoint issues its access token like any other provider.
    (The old blanket zoom block only ever served the webhook connection.)"""
    zoom_connection = {
        **CONNECTION, "connection_id": "zoom-meetings", "provider": "zoom",
        "scopes": ["meeting:write:meeting"],
        "expected_account_id": "zoom-user-1",
        "verified_account_id": "zoom-user-1",
    }
    configure(monkeypatch, claims={"sub": "subject-1"},
              connections={"zoom-meetings": zoom_connection},
              grants={("zoom-meetings", "subject-1#buildcamp-uploader"): GRANT})
    monkeypatch.setattr(
        agent_api.tokens, "get_access_token",
        lambda connection: ("zoom-access", {
            "expires_at": 123, "scope": "meeting:write:meeting",
            "provider_account_id": "zoom-user-1", "account_title": "A",
            "refreshed": False,
        }),
    )
    response = agent_api.route(
        event({"connection_id": "zoom-meetings", "agent": "buildcamp-uploader"}),
        "POST", "/api/agent/token",
    )
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["access_token"] == "zoom-access"
    assert body["provider"] == "zoom"


def test_zoom_oauth_connection_issues_tokens(monkeypatch):
    """A Zoom meeting connection is a regular OAuth connection: once granted,
    the agent token endpoint issues its access token like any other provider.
    (The old blanket zoom block only ever served the webhook connection.)"""
    zoom_connection = {
        **CONNECTION, "connection_id": "zoom-meetings", "provider": "zoom",
        "scopes": ["meeting:write:meeting"],
        "expected_account_id": "zoom-user-1",
        "verified_account_id": "zoom-user-1",
    }
    configure(monkeypatch, claims={"sub": "subject-1"},
              connections={"zoom-meetings": zoom_connection},
              grants={("zoom-meetings", "subject-1#buildcamp-uploader"): GRANT})
    monkeypatch.setattr(
        agent_api.tokens, "get_access_token",
        lambda connection: ("zoom-access", {
            "expires_at": 123, "scope": "meeting:write:meeting",
            "provider_account_id": "zoom-user-1", "account_title": "A",
            "refreshed": False,
        }),
    )
    response = agent_api.route(
        event({"connection_id": "zoom-meetings", "agent": "buildcamp-uploader"}),
        "POST", "/api/agent/token",
    )
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["access_token"] == "zoom-access"
    assert body["provider"] == "zoom"


def _operator(monkeypatch):
    configure(monkeypatch, claims={"sub": "op-1", "email": "op@datatalks.club"})
    monkeypatch.setenv("OPERATOR_EMAILS", "op@datatalks.club")


def test_errors_summary_groups_failed_runs_by_workflow(monkeypatch):
    _operator(monkeypatch)
    from src.dapier.api import errors

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 30, 12, tzinfo=timezone.utc)

    monkeypatch.setattr(errors, "datetime", FixedDatetime)
    from src.dapier.api import runs as runs_api

    failed = [
        {"run_id": "wf-1:e1", "workflow_id": "wf-1", "status": "failed",
         "started_at": "2026-09-25T10:00:00+00:00", "error": "Slack rejected message"},
        {"run_id": "wf-1:e2", "workflow_id": "wf-1", "status": "error",
         "started_at": "2026-09-26T10:00:00+00:00", "error": "connection timeout"},
        {"run_id": "wf-2:e3", "workflow_id": "wf-2", "status": "failed",
         "started_at": "2026-09-26T11:00:00+00:00", "error": "boom"},
        {"run_id": "wf-3:e4", "workflow_id": "wf-3", "status": "completed",
         "started_at": "2026-09-26T12:00:00+00:00"},
    ]
    seen = {}

    def fake_recent(limit, workflow_id=None, status=None, since=None, before=None):
        seen["args"] = (limit, status, since)
        return [run for run in failed if run["status"] in ("failed", "error")]

    monkeypatch.setattr(runs_api, "recent", fake_recent)

    response = agent_api.route(
        event(query={"days": "7"}), "GET", "/api/agent/errors/summary")

    assert response["statusCode"] == 200
    limit, status, since = seen["args"]
    assert status == "problems"
    assert since < "2026-09-26"
    body = json.loads(response["body"])
    assert body["window_days"] == 7
    assert body["total_failed_runs"] == 3
    assert [row["workflow_id"] for row in body["workflows"]] == ["wf-1", "wf-2"]
    assert body["workflows"][0]["failed_runs"] == 2
    assert body["workflows"][0]["last_failed_at"] == "2026-09-26T10:00:00+00:00"
    assert body["workflows"][0]["last_error"] == "connection timeout"
    assert body["workflows"][1]["last_error"] == "boom"


def test_errors_summary_requires_operator(monkeypatch):
    configure(monkeypatch, claims={"sub": "subject-1", "email": "agent@example.test"})
    monkeypatch.setenv("OPERATOR_EMAILS", "op@datatalks.club")

    response = agent_api.route(event(), "GET", "/api/agent/errors/summary")

    assert response["statusCode"] == 403


def test_usage_endpoint_serves_the_rollup_over_bearer(monkeypatch):
    _operator(monkeypatch)

    class September2026(datetime):
        """Stands in for the clock: the endpoint derives its month key
        from ``datetime.now`` and has no ``now`` seam."""

        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 15, tzinfo=timezone.utc)

    from src.dapier.api import agent as agent_api

    monkeypatch.setattr(agent_api.usage, "datetime", September2026)

    class UsageTable:
        def query(self, **kwargs):
            wanted = kwargs["KeyConditionExpression"]._values[-1]
            if wanted != "202609":
                return {"Items": []}
            return {"Items": [{"month": "202609", "workflow_id": "wf-1", "tasks": 7}]}

        def get_item(self, **kwargs):
            return {}  # no quota config item, no _total row yet

    monkeypatch.setenv("TASK_USAGE_TABLE", "task-usage")
    monkeypatch.setattr(agent_api.usage, "_table", lambda: UsageTable())

    response = agent_api.route(
        event(query={"months": "1"}), "GET", "/api/agent/usage")

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["usage"] == [{"month": "202609", "workflow_id": "wf-1", "tasks": 7}]


# --- Cancel of a suspended run (POST /api/agent/runs/{id}/cancel) ---

DELAYED_STEP = {
    "execution_id": "wf-1:pause:evt-1", "run_id": "wf-1:evt-1", "workflow_id": "wf-1",
    "action_id": "pause", "action_type": "delay", "connector": "email",
    "event_type": "message.received", "status": "delayed",
    "started_at": "2026-09-25T10:00:00+00:00", "finished_at": "2026-09-25T10:00:00+00:00",
    "output": {"resume_at": "2026-09-26T10:00:00+00:00"},
}


def _configure_cancellable_runs_table(monkeypatch, items):
    updates = []

    class RunsTable:
        def scan(self, **kwargs):
            return {"Items": items}

        def query(self, **kwargs):
            values = list((kwargs.get("ExpressionAttributeValues") or {}).values())
            wanted = values[0] if values else None
            return {"Items": [item for item in items if item.get("run_id") == wanted]}

        def update_item(self, **kwargs):
            updates.append(kwargs)
            for item in items:
                if item.get("execution_id") == kwargs["Key"]["execution_id"]:
                    item["status"] = kwargs["ExpressionAttributeValues"][":cancelled"]
            return {}

    monkeypatch.setattr(agent_api.runs, "_table", lambda: RunsTable())
    return updates


def test_runs_cancel_over_bearer_requires_operator(monkeypatch):
    configure(monkeypatch, claims={"sub": "subject-1", "email": "agent@example.test"})
    monkeypatch.setenv("OPERATOR_EMAILS", "op@datatalks.club")
    _configure_cancellable_runs_table(monkeypatch, [dict(DELAYED_STEP)])

    cancelled = agent_api.route(event(), "POST", "/api/agent/runs/wf-1:evt-1/cancel")

    assert cancelled["statusCode"] == 403


def test_runs_cancel_over_bearer_closes_the_parked_steps(monkeypatch):
    configure(monkeypatch, claims={"sub": "op-1", "email": "op@datatalks.club"})
    monkeypatch.setenv("OPERATOR_EMAILS", "op@datatalks.club")
    updates = _configure_cancellable_runs_table(monkeypatch, [dict(DELAYED_STEP)])
    audited = []
    monkeypatch.setattr(agent_api.audit, "emit", lambda *a, **k: audited.append((a, k)))

    cancelled = agent_api.route(event(), "POST", "/api/agent/runs/wf-1%3Aevt-1/cancel")

    assert cancelled["statusCode"] == 200
    body = json.loads(cancelled["body"])
    assert body["accepted"] is True
    assert body["cancelled"] == 1
    assert body["run"]["status"] == "cancelled"
    # Conditional close-out, mirroring the worker's: only a still-parked
    # step flips, so a concurrent resume cannot double-write it.
    assert updates[0]["ConditionExpression"] == "#status = :delayed"
    assert audited == [(("wf-1:evt-1", "runs.cancel", "op-1"), {"outcome": "ok"})]

    missing = agent_api.route(event(), "POST", "/api/agent/runs/wf-1:missing/cancel")
    assert missing["statusCode"] == 404


def test_runs_cancel_over_bearer_rejects_a_run_not_suspended(monkeypatch):
    configure(monkeypatch, claims={"sub": "op-1", "email": "op@datatalks.club"})
    monkeypatch.setenv("OPERATOR_EMAILS", "op@datatalks.club")
    _configure_cancellable_runs_table(monkeypatch, [{
        "execution_id": "wf-1:post:evt-1", "run_id": "wf-1:evt-1", "workflow_id": "wf-1",
        "action_id": "post", "connector": "email", "event_type": "message.received",
        "status": "completed", "started_at": "2026-09-25T10:00:00+00:00",
        "finished_at": "2026-09-25T10:00:01+00:00",
    }])

    cancelled = agent_api.route(event(), "POST", "/api/agent/runs/wf-1:evt-1/cancel")

    assert cancelled["statusCode"] == 409
    assert json.loads(cancelled["body"])["error"] == "Run is not suspended; nothing to cancel"


# --- Audit trail read (GET /api/agent/audit, mirroring /api/admin/audit) ---

def _configure_audit_table(monkeypatch, items):
    class AuditTable:
        def scan(self, **kwargs):
            return {"Items": items}

    monkeypatch.setenv("AUDIT_TABLE", "audit")
    monkeypatch.setattr(agent_api.audit, "audit_table", lambda: AuditTable())


def test_audit_endpoint_requires_operator(monkeypatch):
    configure(monkeypatch, claims={"sub": "agent-1", "email": "agent@example.test"})
    monkeypatch.setenv("OPERATOR_EMAILS", "op@datatalks.club")

    response = agent_api.route(event(), "GET", "/api/agent/audit")

    assert response["statusCode"] == 403


def test_audit_endpoint_never_serves_an_api_token(monkeypatch):
    tables = configure(monkeypatch, claims={"sub": "subject-1"})
    secret = _issue_token(tables)

    response = agent_api.route(event(token=secret), "GET", "/api/agent/audit")

    assert response["statusCode"] == 403


def test_audit_endpoint_serves_rows_newest_first_filtered_and_projected(monkeypatch):
    _operator(monkeypatch)
    _configure_audit_table(monkeypatch, [
        {"audit_id": "conn-a#100#2", "connection_id": "conn-a", "action": "connect",
         "actor_subject": "op-1", "outcome": "created",
         "timestamp": "2026-09-27T10:00:00+00:00", "expires_at": 999},
        {"audit_id": "conn-b#200#1", "connection_id": "conn-b", "action": "token",
         "actor_subject": "token:scheduler", "agent": "scheduler", "outcome": "error",
         "timestamp": "2026-09-28T10:00:00+00:00", "error": "provider is unavailable",
         "internal_field": "never"},
    ])

    response = agent_api.route(
        event(query={"action": "token", "limit": "10"}),
        "GET", "/api/agent/audit",
    )

    assert response["statusCode"] == 200
    assert response["headers"]["cache-control"] == "no-store"
    body = json.loads(response["body"])
    # Action filter applied: only the token row survives.
    assert [item["action"] for item in body["events"]] == ["token"]
    assert body["events"][0] == {
        "audit_id": "conn-b#200#1",
        "connection_id": "conn-b", "action": "token",
        "actor_subject": "token:scheduler", "agent": "scheduler",
        "outcome": "error", "timestamp": "2026-09-28T10:00:00+00:00",
        "error": "provider is unavailable",
    }
    # The projection keeps internal bookkeeping and any unexpected column
    # out of the payload, even if one ever reached the table.
    serialized = json.dumps(body)
    assert "expires_at" not in serialized
    assert "internal_field" not in serialized


def test_audit_endpoint_without_the_table_is_an_empty_trail(monkeypatch):
    _operator(monkeypatch)
    monkeypatch.delenv("AUDIT_TABLE", raising=False)

    response = agent_api.route(event(), "GET", "/api/agent/audit")

    assert response["statusCode"] == 200
    assert json.loads(response["body"])["events"] == []


def test_runs_export_route_requires_operator(monkeypatch):
    configure(monkeypatch, claims={"sub": "subject-1", "email": "agent@example.test"})
    monkeypatch.setenv("OPERATOR_EMAILS", "op@datatalks.club")
    monkeypatch.delenv("AUDIT_TABLE", raising=False)

    exported = agent_api.route(event(), "GET", "/api/agent/runs/export")

    assert exported["statusCode"] == 403


def test_runs_export_route_returns_csv(monkeypatch):
    _operator(monkeypatch)
    monkeypatch.delenv("AUDIT_TABLE", raising=False)
    _configure_runs_table(monkeypatch, [{
        "execution_id": "wf-1:post:evt-1", "run_id": "wf-1:evt-1",
        "workflow_id": "wf-1", "action_id": "post", "action_type": "webhook",
        "connector": "email", "event_type": "message.received", "status": "completed",
        "started_at": "2026-09-25T10:00:00+00:00", "finished_at": "2026-09-25T10:00:01+00:00",
    }])

    exported = agent_api.route(event(query={"max_rows": "10", "workflow": "wf-1"}),
                               "GET", "/api/agent/runs/export")

    assert exported["statusCode"] == 200
    body = json.loads(exported["body"])
    assert body["count"] == 1
    assert body["filename"].startswith("dapier-runs-")
    assert "wf-1:evt-1" in body["csv"]
