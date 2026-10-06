"""The operator-gated traceback on unhandled API exceptions.

An unhandled exception in either dispatch used to escape as a bare
API-Gateway 500 whose only evidence lived in the Lambda log — unreadable
without AWS access. Authenticated operators now get the traceback in the
response body (both surfaces dispatch through these two entry points);
every other caller keeps the old bare failure.
"""

import json
import json
import pytest

from src.dapier import http
from src.dapier.api import admin
from src.dapier.api import agent as agent_api
from src.dapier.api.admin import dispatch

BOOM = RuntimeError("boom-marker")


def admin_request(method, path, query=None):
    return {
        "requestContext": {"http": {"method": method, "path": path}},
        "headers": {"host": "dapier.example.test"},
        "cookies": [],
        "queryStringParameters": query,
        "body": None,
    }


def agent_request(method, path, query=None, token="dtc-token"):
    request = admin_request(method, path, query)
    request["headers"]["authorization"] = f"Bearer {token}"
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
def operator_session(monkeypatch):
    monkeypatch.setattr(admin.session, "authenticated", lambda event: True)
    monkeypatch.setattr(admin.session, "_csrf_ok", lambda event, method: True)
    monkeypatch.setattr(admin.session, "require_operator",
                        lambda event: ({"sub": "op-1"}, None))


def test_agent_route_echoes_traceback_to_operators(monkeypatch, agent_identity):
    def raising_config():
        raise BOOM

    monkeypatch.setattr(agent_api, "public_config", raising_config)
    response = agent_api.route(agent_request("GET", "/api/agent/config"),
                               "GET", "/api/agent/config")
    assert response["statusCode"] == 500
    body = json.loads(response["body"])
    assert body["error"] == "boom-marker"
    assert "boom-marker" in body["traceback"]
    assert "RuntimeError" in body["traceback"]


def test_agent_route_keeps_the_bare_failure_for_non_operators(monkeypatch, agent_identity):
    monkeypatch.setenv("OPERATOR_SUBJECTS", "someone-else")

    def raising_config():
        raise BOOM

    monkeypatch.setattr(agent_api, "public_config", raising_config)
    with pytest.raises(RuntimeError):
        agent_api.route(agent_request("GET", "/api/agent/config"),
                        "GET", "/api/agent/config")


def test_agent_route_keeps_the_bare_failure_for_anonymous(monkeypatch, agent_identity):
    def raising_config():
        raise BOOM

    monkeypatch.setattr(agent_api, "public_config", raising_config)
    request = agent_request("GET", "/api/agent/config")
    del request["headers"]["authorization"]
    with pytest.raises(RuntimeError):
        agent_api.route(request, "GET", "/api/agent/config")


def test_admin_route_echoes_traceback_to_the_gated_operator(monkeypatch, operator_session):
    def raising_router(event, method, path, operator_payload, operator_subject):
        raise BOOM

    monkeypatch.setattr(dispatch, "_route_overview_runs", raising_router)
    response = admin.route(admin_request("GET", "/api/admin/overview"),
                           "GET", "/api/admin/overview")
    assert response["statusCode"] == 500
    body = json.loads(response["body"])
    assert body["error"] == "boom-marker"
    assert "boom-marker" in body["traceback"]


def test_pre_gate_paths_keep_the_bare_failure(monkeypatch):
    def raising_login(event):
        raise BOOM

    monkeypatch.setattr(dispatch.login, "auth_login", raising_login)
    with pytest.raises(RuntimeError):
        admin.route(admin_request("GET", "/auth/login"), "GET", "/auth/login")


def test_operator_error_500_without_traceback_stays_plain():
    response = http._operator_error_500({"requestContext": {"requestId": "req-1"}}, BOOM)
    assert response["statusCode"] == 500
    body = json.loads(response["body"])
    assert body == {"error": "boom-marker", "request_id": "req-1"}
