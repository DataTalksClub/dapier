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


def test_save_aws_credential_stores_the_key_pair(monkeypatch):
    writes = []
    monkeypatch.setattr(credentials_module, "put_credential",
                        lambda credential_id, value, **kwargs: writes.append((credential_id, value, kwargs)))
    body = {"access_key_id": "AKIAIOSFODNN7EXAMPLE",
            "secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"}

    response = admin.save_credential("aws", request("PUT", "/api/admin/credentials/aws", body))

    assert response["statusCode"] == 200
    assert writes[0] == ("aws", body, {"provider": "aws"})


def test_save_aws_credential_rejects_bad_keys(monkeypatch):
    writes = []
    monkeypatch.setattr(credentials_module, "put_credential",
                        lambda credential_id, value, **kwargs: writes.append((credential_id, value, kwargs)))

    short_secret = admin.save_credential("aws", request(
        "PUT", "/api/admin/credentials/aws",
        {"access_key_id": "AKIAIOSFODNN7EXAMPLE", "secret_access_key": "too-short"}))
    bad_access_key = admin.save_credential("aws", request(
        "PUT", "/api/admin/credentials/aws",
        {"access_key_id": "not a key!", "secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"}))

    assert short_secret["statusCode"] == 400
    assert bad_access_key["statusCode"] == 400
    assert writes == []


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


def test_save_telegram_connection_names_itself_after_the_verified_bot(monkeypatch):
    records = []
    _fake_connections_table(monkeypatch, records)
    monkeypatch.setattr(telegram_api, "get_me", lambda token: ("987654321", "@dtc_alerts_bot"))
    monkeypatch.setattr(credentials_module, "put_credential", lambda *args, **kwargs: None)

    response = admin.save_connection(request("PUT", "/api/admin/connections", {
        "connection_id": "telegram-bot-2",
        "provider": "telegram",
        "token": "987654321:" + "A" * 35,
    }))

    assert response["statusCode"] == 200
    # No display name was supplied, so the verified bot identity becomes it —
    # that is what tells several bots of one provider apart in the console.
    assert records[0]["account_title"] == "@dtc_alerts_bot"
    assert records[0]["display_name"] == "@dtc_alerts_bot"


def test_save_telegram_connection_keeps_operator_rename(monkeypatch):
    records = []
    _fake_connections_table(monkeypatch, records)
    monkeypatch.setattr(telegram_api, "get_me", lambda token: ("987654321", "@dtc_alerts_bot"))
    monkeypatch.setattr(credentials_module, "put_credential", lambda *args, **kwargs: None)

    response = admin.save_connection(request("PUT", "/api/admin/connections", {
        "connection_id": "telegram-bot-2",
        "provider": "telegram",
        "display_name": "Announcements bot",
        "token": "987654321:" + "A" * 35,
    }))

    assert response["statusCode"] == 200
    assert records[0]["display_name"] == "Announcements bot"


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
from src.dapier.connections.providers import slack_tokens, telegram_api
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


def test_admin_connection_revoke_route(monkeypatch):
    from src.dapier.auth import session
    from src.dapier.connections import tokens as connection_tokens

    connection = {
        "connection_id": "youtube-personal",
        "provider": "youtube",
        "credential_id": "oauth#youtube-personal",
        "status": "connected",
    }
    stored = []

    class Table:
        def get_item(self, **kwargs):
            return {"Item": connection}

        def put_item(self, **kwargs):
            stored.append(kwargs["Item"])

    class Dynamo:
        def Table(self, _name):
            return Table()

    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())
    monkeypatch.setenv("CONNECTIONS_TABLE", "connections")
    monkeypatch.delenv("AUDIT_TABLE", raising=False)
    monkeypatch.setenv("OPERATOR_EMAILS", "op@datatalks.club")
    monkeypatch.setattr(session, "_credentials", lambda: {"password": "session-secret"})
    monkeypatch.setattr(connection_tokens, "revoke_connection",
                        lambda item: {**item, "status": "revoked"})
    cookie = session._sign({
        "sub": "op@datatalks.club", "subject": "op-1", "exp": int(time.time()) + 600,
    })
    path = "/api/admin/connections/youtube-personal/tokens"

    response = admin.route(
        operator_request("DELETE", path, cookies=[f"dapier_session={cookie}"]),
        "DELETE", path,
    )

    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {
        "connection_id": "youtube-personal", "status": "revoked",
    }
    assert stored[0]["status"] == "revoked"



def _configure_runs(monkeypatch, items):
    """Operator session plus a fake executions table for the runs routes."""
    monkeypatch.setattr(session, "_credentials", lambda: {"username": "admin", "password": "pw"})
    cookie = session._sign({"sub": "op@datatalks.club", "subject": "op-sub",
                            "exp": int(time.time()) + 600})

    class RunsTable:
        def scan(self, **kwargs):
            return {"Items": items}

        def query(self, **kwargs):
            values = list((kwargs.get("ExpressionAttributeValues") or {}).values())
            wanted = values[0] if values else None
            return {"Items": [item for item in items if item.get("run_id") == wanted]}

    class Dynamo:
        def Table(self, _name):
            return RunsTable()

    monkeypatch.setenv("EXECUTIONS_TABLE", "executions")
    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())
    return [f"dapier_session={cookie}"]


def test_admin_runs_list_groups_executions_into_runs(monkeypatch):
    cookies = _configure_runs(monkeypatch, [{
        "execution_id": "wf-1:post:evt-1", "run_id": "wf-1:evt-1",
        "workflow_id": "wf-1", "action_id": "post", "action_type": "webhook",
        "connector": "email", "event_type": "message.received", "status": "completed",
        "started_at": "2026-09-25T10:00:00+00:00", "finished_at": "2026-09-25T10:00:01+00:00",
    }])

    listed = admin.route(operator_request("GET", "/api/admin/runs", cookies=cookies),
                         "GET", "/api/admin/runs")

    assert listed["statusCode"] == 200
    body = json.loads(listed["body"])
    assert body["runs"][0]["run_id"] == "wf-1:evt-1"
    assert body["runs"][0]["status"] == "completed"


def test_admin_run_detail_returns_step_flow(monkeypatch):
    cookies = _configure_runs(monkeypatch, [{
        "execution_id": "wf-1:post:evt-1", "run_id": "wf-1:evt-1",
        "workflow_id": "wf-1", "action_id": "post", "action_type": "webhook",
        "connector": "email", "event_type": "message.received", "status": "failed",
        "started_at": "2026-09-25T10:00:00+00:00", "finished_at": "2026-09-25T10:00:01+00:00",
        "error": "webhook returned HTTP 500", "duration_ms": 980,
        "input": {"subject": "invoice"}, "output": None,
    }])

    detail = admin.route(operator_request("GET", "/api/admin/runs/wf-1%3Aevt-1", cookies=cookies),
                         "GET", "/api/admin/runs/wf-1%3Aevt-1")

    assert detail["statusCode"] == 200
    body = json.loads(detail["body"])
    assert body["run"]["status"] == "failed"
    assert body["steps"][0]["error"] == "webhook returned HTTP 500"
    assert body["steps"][0]["input"] == {"subject": "invoice"}


def test_admin_run_detail_unknown_run_is_404(monkeypatch):
    cookies = _configure_runs(monkeypatch, [])

    detail = admin.route(operator_request("GET", "/api/admin/runs/wf-1:missing", cookies=cookies),
                         "GET", "/api/admin/runs/wf-1:missing")

    assert detail["statusCode"] == 404


def _configure_runs(monkeypatch, items):
    """Operator session plus a fake executions table for the runs routes."""
    monkeypatch.setattr(session, "_credentials", lambda: {"username": "admin", "password": "pw"})
    cookie = session._sign({"sub": "op@datatalks.club", "subject": "op-sub",
                            "exp": int(time.time()) + 600})

    class RunsTable:
        def scan(self, **kwargs):
            return {"Items": items}

        def query(self, **kwargs):
            values = list((kwargs.get("ExpressionAttributeValues") or {}).values())
            wanted = values[0] if values else None
            return {"Items": [item for item in items if item.get("run_id") == wanted]}

    class Dynamo:
        def Table(self, _name):
            return RunsTable()

    monkeypatch.setenv("EXECUTIONS_TABLE", "executions")
    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())
    return [f"dapier_session={cookie}"]


def test_admin_runs_list_groups_executions_into_runs(monkeypatch):
    cookies = _configure_runs(monkeypatch, [{
        "execution_id": "wf-1:post:evt-1", "run_id": "wf-1:evt-1",
        "workflow_id": "wf-1", "action_id": "post", "action_type": "webhook",
        "connector": "email", "event_type": "message.received", "status": "completed",
        "started_at": "2026-09-25T10:00:00+00:00", "finished_at": "2026-09-25T10:00:01+00:00",
    }])

    listed = admin.route(operator_request("GET", "/api/admin/runs", cookies=cookies),
                         "GET", "/api/admin/runs")

    assert listed["statusCode"] == 200
    body = json.loads(listed["body"])
    assert body["runs"][0]["run_id"] == "wf-1:evt-1"
    assert body["runs"][0]["status"] == "completed"


def test_admin_run_detail_returns_step_flow(monkeypatch):
    cookies = _configure_runs(monkeypatch, [{
        "execution_id": "wf-1:post:evt-1", "run_id": "wf-1:evt-1",
        "workflow_id": "wf-1", "action_id": "post", "action_type": "webhook",
        "connector": "email", "event_type": "message.received", "status": "failed",
        "started_at": "2026-09-25T10:00:00+00:00", "finished_at": "2026-09-25T10:00:01+00:00",
        "error": "webhook returned HTTP 500", "duration_ms": 980,
        "input": {"subject": "invoice"}, "output": None,
    }])

    detail = admin.route(operator_request("GET", "/api/admin/runs/wf-1%3Aevt-1", cookies=cookies),
                         "GET", "/api/admin/runs/wf-1%3Aevt-1")

    assert detail["statusCode"] == 200
    body = json.loads(detail["body"])
    assert body["run"]["status"] == "failed"
    assert body["steps"][0]["error"] == "webhook returned HTTP 500"
    assert body["steps"][0]["input"] == {"subject": "invoice"}


def test_admin_run_detail_unknown_run_is_404(monkeypatch):
    cookies = _configure_runs(monkeypatch, [])

    detail = admin.route(operator_request("GET", "/api/admin/runs/wf-1:missing", cookies=cookies),
                         "GET", "/api/admin/runs/wf-1:missing")

    assert detail["statusCode"] == 404


def _configure_replay_queue(monkeypatch):
    monkeypatch.setenv("EVENT_QUEUE_URL", "https://sqs.test/events")
    calls = []

    class Queue:
        def send_message(self, **kwargs):
            calls.append(kwargs)
            return {"MessageId": "sqsm-1"}

    monkeypatch.setattr(admin.routes.runs, "_queue", lambda: Queue())
    return calls


def test_admin_run_replay_reinjects_the_original_event(monkeypatch):
    cookies = _configure_runs(monkeypatch, [{
        "execution_id": "wf-1:post:evt-1", "run_id": "wf-1:evt-1",
        "workflow_id": "wf-1", "action_id": "post", "action_type": "webhook",
        "connector": "email", "event_type": "message.received", "status": "failed",
        "started_at": "2026-09-25T10:00:00+00:00", "finished_at": "2026-09-25T10:00:01+00:00",
        "error": "webhook returned HTTP 500", "input": {"subject": "invoice"},
    }])
    calls = _configure_replay_queue(monkeypatch)

    replayed = admin.route(operator_request("POST", "/api/admin/runs/wf-1%3Aevt-1/replay", cookies=cookies),
                           "POST", "/api/admin/runs/wf-1%3Aevt-1/replay")

    assert replayed["statusCode"] == 202
    body = json.loads(replayed["body"])
    assert body["accepted"] is True
    assert body["replayed_from"] == "wf-1:evt-1"
    assert body["run_id"].startswith("wf-1:replay-")
    envelope = json.loads(calls[0]["MessageBody"])
    assert envelope["id"].startswith("replay-")
    assert envelope["correlation_id"] == "evt-1"
    assert envelope["data"] == {"subject": "invoice"}

    missing = admin.route(operator_request("POST", "/api/admin/runs/wf-1:missing/replay", cookies=cookies),
                          "POST", "/api/admin/runs/wf-1:missing/replay")
    assert missing["statusCode"] == 404


def test_admin_run_replay_requires_authentication(monkeypatch):
    _configure_runs(monkeypatch, [])

    replayed = admin.route(operator_request("POST", "/api/admin/runs/wf-1:evt-1/replay", cookies=[]),
                           "POST", "/api/admin/runs/wf-1:evt-1/replay")

    assert replayed["statusCode"] == 401


def _configure_replay_queue(monkeypatch):
    monkeypatch.setenv("EVENT_QUEUE_URL", "https://sqs.test/events")
    calls = []

    class Queue:
        def send_message(self, **kwargs):
            calls.append(kwargs)
            return {"MessageId": "sqsm-1"}

    monkeypatch.setattr(admin.routes.runs, "_queue", lambda: Queue())
    return calls


def test_admin_run_replay_reinjects_the_original_event(monkeypatch):
    cookies = _configure_runs(monkeypatch, [{
        "execution_id": "wf-1:post:evt-1", "run_id": "wf-1:evt-1",
        "workflow_id": "wf-1", "action_id": "post", "action_type": "webhook",
        "connector": "email", "event_type": "message.received", "status": "failed",
        "started_at": "2026-09-25T10:00:00+00:00", "finished_at": "2026-09-25T10:00:01+00:00",
        "error": "webhook returned HTTP 500", "input": {"subject": "invoice"},
    }])
    calls = _configure_replay_queue(monkeypatch)

    replayed = admin.route(operator_request("POST", "/api/admin/runs/wf-1%3Aevt-1/replay", cookies=cookies),
                           "POST", "/api/admin/runs/wf-1%3Aevt-1/replay")

    assert replayed["statusCode"] == 202
    body = json.loads(replayed["body"])
    assert body["accepted"] is True
    assert body["replayed_from"] == "wf-1:evt-1"
    assert body["run_id"].startswith("wf-1:replay-")
    envelope = json.loads(calls[0]["MessageBody"])
    assert envelope["id"].startswith("replay-")
    assert envelope["correlation_id"] == "evt-1"
    assert envelope["data"] == {"subject": "invoice"}

    missing = admin.route(operator_request("POST", "/api/admin/runs/wf-1:missing/replay", cookies=cookies),
                          "POST", "/api/admin/runs/wf-1:missing/replay")
    assert missing["statusCode"] == 404


def test_admin_run_replay_requires_authentication(monkeypatch):
    _configure_runs(monkeypatch, [])

    replayed = admin.route(operator_request("POST", "/api/admin/runs/wf-1:evt-1/replay", cookies=[]),
                           "POST", "/api/admin/runs/wf-1:evt-1/replay")

    assert replayed["statusCode"] == 401
