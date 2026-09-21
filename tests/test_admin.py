import json
from decimal import Decimal

from src import admin, ingress


def request(method, path, body=None, cookies=None):
    return {
        "requestContext": {"http": {"method": method, "path": path}},
        "headers": {"host": "dapier.example.test"},
        "cookies": cookies or [],
        "body": json.dumps(body) if body is not None else None,
    }


def test_login_creates_signed_http_only_session(monkeypatch):
    monkeypatch.setenv("LEGACY_ADMIN_LOGIN_ENABLED", "true")
    monkeypatch.setattr(admin, "_credentials", lambda: {"username": "admin", "password": "correct-password"})

    response = admin.login(request("POST", "/api/admin/session", {"username": "admin", "password": "correct-password"}))

    assert response["statusCode"] == 200
    assert "correct-password" not in response["body"]
    assert response["cookies"][0].startswith("dapier_session=")
    assert "HttpOnly" in response["cookies"][0]
    assert "Secure" in response["cookies"][0]


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
    monkeypatch.setattr(admin, "_credentials", lambda: {"password": "session-secret"})
    start = admin.auth_login(request("GET", "/auth/login"))
    assert start["statusCode"] == 302
    from urllib.parse import parse_qs, urlparse

    query = parse_qs(urlparse(start["headers"]["location"]).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"][0]
    state_cookie = start["cookies"][0].split(";", 1)[0]
    pending = admin._verify(state_cookie.split("=", 1)[1], kind="oidc")
    monkeypatch.setattr(admin, "_exchange_auth_code", lambda code, verifier: {"id_token": "signed-token"})
    monkeypatch.setattr(admin, "_verify_id_token", lambda token: {"nonce": pending["nonce"], **claims})
    event = request("GET", "/auth/callback", cookies=[state_cookie])
    event["queryStringParameters"] = {"code": "valid-code", "state": query["state"][0]}
    return admin.auth_callback(event)


def test_oidc_login_uses_pkce_and_google_identity_creates_session(monkeypatch):
    # Cognito emits the mapped Google attribute as the string "true", not a boolean.
    callback = begin_oidc(monkeypatch, {
        "sub": "Google_115538746644348324376", "email": "Person@DataTalks.Club",
        "email_verified": "true",
    })

    assert callback["statusCode"] == 302
    assert callback["headers"]["location"] == "/"
    session_cookie = next(value for value in callback["cookies"] if value.startswith("dapier_session="))
    session = admin._verify(session_cookie.split(";", 1)[0].split("=", 1)[1])
    assert session["sub"] == "person@datatalks.club"


def test_oidc_callback_rejects_token_without_email(monkeypatch):
    callback = begin_oidc(monkeypatch, {"sub": "Google_115538746644348324376"})

    assert callback["statusCode"] == 303
    assert callback["headers"]["location"] == "/auth/error"
    assert not any(value.startswith("dapier_session=") for value in callback["cookies"])


def test_oidc_callback_rejects_invalid_state(monkeypatch):
    configure_oidc(monkeypatch)
    monkeypatch.setattr(admin, "_credentials", lambda: {"password": "session-secret"})
    response = admin.auth_callback(request("GET", "/auth/callback"))
    assert response["statusCode"] == 303
    assert response["headers"]["location"] == "/auth/error"
    assert response["headers"]["cache-control"] == "no-store"
    assert response["headers"]["referrer-policy"] == "no-referrer"
    assert response["body"] == ""


def test_oidc_error_page_is_hardened():
    response = admin.auth_error()
    assert response["statusCode"] == 403
    assert "Dapier" in response["body"]
    assert response["headers"]["cache-control"] == "no-store"
    assert response["headers"]["referrer-policy"] == "no-referrer"
    assert "default-src 'none'" in response["headers"]["content-security-policy"]


def test_admin_api_rejects_unauthenticated_request(monkeypatch):
    monkeypatch.setattr(admin, "_credentials", lambda: {"username": "admin", "password": "correct-password"})

    response = admin.route(request("GET", "/api/admin/overview"), "GET", "/api/admin/overview")

    assert response["statusCode"] == 401


def test_save_slack_credential_is_write_only(monkeypatch):
    writes = []
    monkeypatch.setattr(admin, "put_credential", lambda credential_id, value, **kwargs: writes.append((credential_id, value, kwargs)))
    token = "xoxb-123456789012345678901234"

    response = admin.save_credential("slack", request("PUT", "/api/admin/credentials/slack", {"token": token}))

    assert response["statusCode"] == 200
    assert token not in response["body"]
    assert writes[0] == ("slack", {"token": token}, {"provider": "slack"})


def test_save_connection_keeps_client_secret_out_of_metadata(monkeypatch):
    credentials = []
    records = []

    class Table:
        def put_item(self, **kwargs):
            records.append(kwargs["Item"])

    class Dynamo:
        def Table(self, _name):
            return Table()

    monkeypatch.setenv("CONNECTIONS_TABLE", "connections")
    monkeypatch.setattr(admin, "put_credential", lambda credential_id, value, **kwargs: credentials.append((credential_id, value, kwargs)))
    monkeypatch.setattr(admin.boto3, "resource", lambda service: Dynamo())
    body = {
        "connection_id": "team-dropbox",
        "provider": "dropbox",
        "display_name": "Team Dropbox",
        "client_id": "client-id",
        "client_secret": "client-secret",
        "scopes": ["files.metadata.read"],
    }

    response = admin.save_connection(request("PUT", "/api/admin/connections", body))

    assert response["statusCode"] == 200
    assert "client-secret" not in response["body"]
    assert "client_secret" not in records[0]
    assert records[0]["credential_id"] == "oauth#team-dropbox"
    assert credentials[0] == ("oauth#team-dropbox", {"client_secret": "client-secret"}, {"provider": "dropbox"})


def test_root_serves_console_with_security_headers():
    response = ingress.handler(request("GET", "/"), None)

    assert response["statusCode"] == 200
    assert "Dapier" in response["body"]
    assert response["headers"]["content-type"].startswith("text/html")
    assert "frame-ancestors 'none'" in response["headers"]["content-security-policy"]


def test_json_response_serializes_dynamodb_numbers():
    response = admin._json_response(200, {"expires_at": Decimal("1791655833")})

    assert json.loads(response["body"])["expires_at"] == 1791655833
