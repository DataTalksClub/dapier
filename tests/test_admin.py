import json
import boto3
from decimal import Decimal

from src.dapier.api import admin
from src.dapier.connections import credentials as credentials_module
from src.dapier.api import router as ingress


def request(method, path, body=None, cookies=None):
    return {
        "requestContext": {"http": {"method": method, "path": path}},
        "headers": {"host": "dapier.example.test"},
        "cookies": cookies or [],
        "body": json.dumps(body) if body is not None else None,
    }


def configure_oidc(monkeypatch):
    monkeypatch.setenv("AUTH_BASE_URL", "https://auth.example.test")
    monkeypatch.setenv("AUTH_CLIENT_ID", "dapier-client")
    monkeypatch.setenv("AUTH_CALLBACK_URL", "https://dapier.example.test/auth/callback")
    monkeypatch.setenv("AUTH_LOGOUT_URL", "https://dapier.example.test/")
    monkeypatch.setenv("AUTH_ISSUER", "https://issuer.example.test/pool")
    monkeypatch.setenv("AUTH_JWKS_URL", "https://issuer.example.test/pool/.well-known/jwks.json")


def begin_oidc(monkeypatch, claims):
    """Run login, then feed `claims` back through the callback as the ID token."""
    configure_oidc(monkeypatch)
    monkeypatch.setattr(session, "_credentials", lambda: {"password": "session-secret"})
    start = login.auth_login(request("GET", "/auth/login"))
    assert start["statusCode"] == 302
    from urllib.parse import parse_qs, urlparse

    query = parse_qs(urlparse(start["headers"]["location"]).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"][0]
    state_cookie = start["cookies"][0].split(";", 1)[0]
    pending = session._verify(state_cookie.split("=", 1)[1], kind="oidc")
    monkeypatch.setattr(dtc_auth, "exchange_auth_code", lambda code, verifier: {"id_token": "signed-token"})
    monkeypatch.setattr(dtc_auth, "verify_id_token", lambda token: {"nonce": pending["nonce"], **claims})
    event = request("GET", "/auth/callback", cookies=[state_cookie])
    event["queryStringParameters"] = {"code": "valid-code", "state": query["state"][0]}
    return login.auth_callback(event)


def test_oidc_login_uses_pkce_and_google_identity_creates_session(monkeypatch):
    # Cognito emits the mapped Google attribute as the string "true", not a boolean.
    callback = begin_oidc(monkeypatch, {
        "sub": "Google_115538746644348324376", "email": "Person@DataTalks.Club",
        "email_verified": "true",
    })

    assert callback["statusCode"] == 302
    assert callback["headers"]["location"] == "/"
    session_cookie = next(value for value in callback["cookies"] if value.startswith("dapier_session="))
    decoded = session._verify(session_cookie.split(";", 1)[0].split("=", 1)[1])
    assert decoded["sub"] == "person@datatalks.club"


def test_oidc_callback_rejects_token_without_email(monkeypatch):
    callback = begin_oidc(monkeypatch, {"sub": "Google_115538746644348324376"})

    assert callback["statusCode"] == 303
    assert callback["headers"]["location"] == "/auth/error"
    assert not any(value.startswith("dapier_session=") for value in callback["cookies"])


def test_oidc_callback_rejects_invalid_state(monkeypatch):
    configure_oidc(monkeypatch)
    monkeypatch.setattr(session, "_credentials", lambda: {"password": "session-secret"})
    response = login.auth_callback(request("GET", "/auth/callback"))
    assert response["statusCode"] == 303
    assert response["headers"]["location"] == "/auth/error"
    assert response["headers"]["cache-control"] == "no-store"
    assert response["headers"]["referrer-policy"] == "no-referrer"
    assert response["body"] == ""


def test_oidc_error_page_is_hardened():
    response = login.auth_error()
    assert response["statusCode"] == 403
    assert "Dapier" in response["body"]
    assert response["headers"]["cache-control"] == "no-store"
    assert response["headers"]["referrer-policy"] == "no-referrer"
    assert "default-src 'none'" in response["headers"]["content-security-policy"]


def test_admin_api_rejects_unauthenticated_request(monkeypatch):
    monkeypatch.setattr(session, "_credentials", lambda: {"username": "admin", "password": "correct-password"})

    response = admin.route(request("GET", "/api/admin/overview"), "GET", "/api/admin/overview")

    assert response["statusCode"] == 401


def test_save_slack_credential_is_write_only(monkeypatch):
    writes = []
    monkeypatch.setattr(credentials_module, "put_credential",
                        lambda credential_id, value, **kwargs: writes.append((credential_id, value, kwargs)))
    token = "xoxb-123456789012345678901234"

    response = admin.save_credential("slack", request("PUT", "/api/admin/credentials/slack", {"token": token}))

    assert response["statusCode"] == 200
    assert token not in response["body"]
    assert writes[0] == ("slack", {"token": token}, {"provider": "slack"})


def test_save_connection_stores_no_client_credentials(monkeypatch):
    credentials = []
    records = []

    class Table:
        def put_item(self, **kwargs):
            records.append(kwargs["Item"])

        def get_item(self, **kwargs):
            return {}

    class Dynamo:
        def Table(self, _name):
            return Table()

    monkeypatch.setenv("CONNECTIONS_TABLE", "connections")
    monkeypatch.setattr(credentials_module, "put_credential", lambda credential_id, value, **kwargs: credentials.append((credential_id, value, kwargs)))
    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())
    body = {
        "connection_id": "team-dropbox",
        "provider": "dropbox",
        "display_name": "Team Dropbox",
        "scopes": ["files.metadata.read"],
    }

    response = admin.save_connection(request("PUT", "/api/admin/connections", body))

    assert response["statusCode"] == 200
    assert credentials == []
    assert "client_id" not in records[0]
    assert "client_secret" not in records[0]
    assert records[0]["credential_id"] == "oauth#team-dropbox"


def _fake_connections_table(monkeypatch, records):
    class Table:
        def put_item(self, **kwargs):
            records.append(kwargs["Item"])

        def get_item(self, **kwargs):
            return {}

    class Dynamo:
        def Table(self, _name):
            return Table()

    monkeypatch.setenv("CONNECTIONS_TABLE", "connections")
    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())


def test_save_slack_connection_verifies_and_stores_token(monkeypatch):
    records = []
    credentials = []
    _fake_connections_table(monkeypatch, records)
    monkeypatch.setattr(slack_tokens, "verify_account", lambda token: ("T012345", "DataTalks"))
    monkeypatch.setattr(credentials_module, "put_credential", lambda credential_id, value, **kwargs: credentials.append((credential_id, value, kwargs)))
    token = "xoxb-" + "a" * 30
    body = {
        "connection_id": "slack",
        "provider": "slack",
        "display_name": "DataTalks Slack",
        "token": token,
    }

    response = admin.save_connection(request("PUT", "/api/admin/connections", body))

    assert response["statusCode"] == 200
    assert records[0]["status"] == "connected"
    assert records[0]["verified_account_id"] == "T012345"
    assert records[0]["account_title"] == "DataTalks"
    assert credentials == [("oauth#slack", {"token": token}, {"provider": "slack"})]
    assert token not in response["body"]


def test_save_slack_connection_requires_token_without_stored_secret(monkeypatch):
    records = []
    _fake_connections_table(monkeypatch, records)

    def missing(credential_id):
        raise KeyError(credential_id)

    monkeypatch.setattr(credentials_module, "get_credential", missing)

    response = admin.save_connection(request("PUT", "/api/admin/connections", {
        "connection_id": "slack",
        "provider": "slack",
        "display_name": "DataTalks Slack",
    }))

    assert response["statusCode"] == 400
    assert "token is required" in response["body"]
    assert records == []


def test_save_slack_connection_rejects_token_slack_rejects(monkeypatch):
    records = []
    _fake_connections_table(monkeypatch, records)

    def reject(token):
        raise slack_tokens.SlackTokenError("Slack rejected the token: invalid_auth")

    monkeypatch.setattr(slack_tokens, "verify_account", reject)

    response = admin.save_connection(request("PUT", "/api/admin/connections", {
        "connection_id": "slack",
        "provider": "slack",
        "display_name": "DataTalks Slack",
        "token": "xoxp-" + "b" * 30,
    }))

    assert response["statusCode"] == 400
    assert "invalid_auth" in response["body"]
    assert records == []


def test_oauth_start_rejects_token_provider(monkeypatch):
    monkeypatch.setattr(oauth_flow, "_connection", lambda connection_id: {
        "connection_id": "slack", "provider": "slack", "status": "connected",
    })

    response = oauth_flow.oauth_start(request("GET", "/api/admin/oauth/slack/start"), "slack")

    assert response["statusCode"] == 400
    assert "directly provided token" in response["body"]


def test_root_serves_console_with_security_headers():
    response = ingress.handler(request("GET", "/"), None)

    assert response["statusCode"] == 200
    assert "Dapier" in response["body"]
    assert response["headers"]["content-type"].startswith("text/html")
    assert "frame-ancestors 'none'" in response["headers"]["content-security-policy"]


def test_json_response_serializes_dynamodb_numbers():
    response = http._json_response(200, {"expires_at": Decimal("1791655833")})

    assert json.loads(response["body"])["expires_at"] == 1791655833


# --- API tokens: console cookie path ---

import time

from src.dapier.auth import api_tokens
from src.dapier import http
from src.dapier.api.admin import login
from src.dapier.connections import oauth_flow
from src.dapier.auth import session
from src.dapier.connections.providers import slack_tokens
from src.dapier.auth import dtc_auth
from src.dapier.api.admin import routes


class TokenTable:
    def __init__(self):
        self.items = {}

    def put_item(self, **kwargs):
        item = kwargs["Item"]
        self.items[item["token_hash"]] = item

    def get_item(self, **kwargs):
        item = self.items.get(kwargs["Key"]["token_hash"])
        return {"Item": dict(item)} if item else {}

    def update_item(self, **kwargs):
        item = self.items.get(kwargs["Key"]["token_hash"])
        if item is not None:
            item["last_used_at"] = kwargs["ExpressionAttributeValues"][":now"]

    def scan(self, **kwargs):
        return {"Items": list(self.items.values())}


def operator_request(method, path, body=None, cookies=None, origin=True):
    event = request(method, path, body, cookies)
    if origin:
        event["headers"]["origin"] = "https://dapier.example.test"
    return event


def configure_tokens(monkeypatch):
    monkeypatch.setattr(session, "_credentials", lambda: {"username": "admin", "password": "pw"})
    table = TokenTable()

    class Dynamo:
        def Table(self, _name):
            return table

    import boto3

    monkeypatch.setenv("API_TOKENS_TABLE", "api-tokens")
    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())
    cookie = session._sign({"sub": "op@datatalks.club", "subject": "op-sub",
                          "exp": int(time.time()) + 600})
    return table, [f"dapier_session={cookie}"]


def test_admin_token_lifecycle_create_list_revoke(monkeypatch):
    table, cookies = configure_tokens(monkeypatch)

    created = admin.route(
        operator_request("PUT", "/api/admin/tokens",
                         {"token_id": "personal-scheduler", "agent": "personal-scheduler"},
                         cookies=cookies),
        "PUT", "/api/admin/tokens",
    )
    assert created["statusCode"] == 200
    body = json.loads(created["body"])
    assert body["token"].startswith("dap_")
    assert body["subject"] == "token:personal-scheduler"
    assert body["token"] not in json.dumps(list(table.items.values()))
    # The plaintext is never echoed by the list endpoint.
    listed = admin.route(
        operator_request("GET", "/api/admin/tokens", cookies=cookies),
        "GET", "/api/admin/tokens",
    )
    assert listed["statusCode"] == 200
    assert body["token"] not in listed["body"]
    assert json.loads(listed["body"])["tokens"][0]["token_prefix"].startswith("dap_")

    delete_event = operator_request("DELETE", "/api/admin/tokens", cookies=cookies)
    delete_event["queryStringParameters"] = {"token_id": "personal-scheduler"}
    revoked = admin.route(delete_event, "DELETE", "/api/admin/tokens")
    assert revoked["statusCode"] == 200
    assert json.loads(revoked["body"])["revoked_at"]
    assert api_tokens.verify(body["token"], table_ref=table) is None


def test_admin_tokens_require_operator_session(monkeypatch):
    configure_tokens(monkeypatch)

    listed = admin.route(operator_request("GET", "/api/admin/tokens"),
                         "GET", "/api/admin/tokens")

    assert listed["statusCode"] == 401


def test_admin_duplicate_token_is_conflict(monkeypatch):
    table, cookies = configure_tokens(monkeypatch)

    first = admin.route(
        operator_request("PUT", "/api/admin/tokens",
                         {"token_id": "scheduler", "agent": "scheduler"}, cookies=cookies),
        "PUT", "/api/admin/tokens",
    )
    second = admin.route(
        operator_request("PUT", "/api/admin/tokens",
                         {"token_id": "scheduler", "agent": "scheduler"}, cookies=cookies),
        "PUT", "/api/admin/tokens",
    )

    assert first["statusCode"] == 200
    assert second["statusCode"] == 409

