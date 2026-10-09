"""Zoom API (OAuth) connections: an explicit kind on create, default read
scopes, and consent through the regular OAuth start — the same rule on the
console save (/api/admin/connections) and the CLI's /api/agent routes."""

import json
import urllib.parse

import pytest

from dapier_cli import commands, main
from src.dapier.api.admin import routes as admin_routes
from src.dapier.connections import oauth_flow, records, zoom
from src.dapier.connections.providers import oauth_clients
from src.dapier.auth import session

from tests.test_agent_api import configure as configure_agent, event as agent_event
from src.dapier.api import agent as agent_api
from tests.test_zoom import setup as setup_webhook

DEFAULTS = sorted(zoom.API_DEFAULT_SCOPES)


@pytest.fixture(autouse=True)
def clean_oauth_client_cache():
    oauth_clients.invalidate_cache()
    yield
    oauth_clients.invalidate_cache()


def _admin(monkeypatch, table):
    monkeypatch.setenv("CONNECTIONS_TABLE", "connections")
    monkeypatch.setattr(admin_routes.boto3, "resource", lambda _: type(
        "Resource", (), {"Table": lambda self, name: table})())
    monkeypatch.setattr(admin_routes.session, "_session_subject", lambda _: "operator")
    monkeypatch.setattr(admin_routes.session, "_audit_event", lambda *args, **kwargs: None)


def _save(body):
    response = admin_routes.save_connection({"body": json.dumps(body)})
    return response["statusCode"], json.loads(response["body"])


def test_defaults_are_read_scopes_with_the_identity_scope():
    assert "user:read:user" in zoom.API_DEFAULT_SCOPES
    assert all(":read:" in scope for scope in zoom.API_DEFAULT_SCOPES)


def test_console_creates_zoom_api_connection_with_default_scopes(monkeypatch):
    table, secrets = setup_webhook(monkeypatch)
    _admin(monkeypatch, table)
    status, view = _save({"provider": "zoom", "kind": "api"})
    assert status == 200, view
    item = table.items[view["connection_id"]]
    assert item["scopes"] == DEFAULTS
    assert item["status"] == "ready"
    assert not records.is_zoom_webhook(item)
    public = records.public_view(item)
    assert public["oauth_consent"] is True
    assert public["services"][0]["label"] == "Zoom API"
    # No webhook secret is written for an OAuth connection.
    assert set(secrets) == {"oauth#zoom"}


def test_console_zoom_api_keeps_explicit_scopes(monkeypatch):
    table, _ = setup_webhook(monkeypatch)
    _admin(monkeypatch, table)
    status, view = _save({"provider": "zoom", "kind": "api",
                          "scopes": ["user:read:user", "meeting:write:meeting"]})
    assert status == 200
    assert table.items[view["connection_id"]]["scopes"] == ["meeting:write:meeting", "user:read:user"]


def test_console_zoom_kind_rules(monkeypatch):
    table, secrets = setup_webhook(monkeypatch)  # stores webhook connection "zoom"
    _admin(monkeypatch, table)
    # No kind: a new scope-less Zoom save stays the webhook setup (backward compatible).
    status, payload = _save({"connection_id": "zoom-new", "provider": "zoom"})
    assert status == 400 and "Secret Token" in payload["error"]
    status, _ = _save({"connection_id": "zoom-hook", "provider": "zoom", "kind": "webhook",
                       "token": "another-secret-123456"})
    assert status == 200 and secrets["oauth#zoom-hook"]["webhook_secret"] == "another-secret-123456"
    # Contradictions are refused, not silently reinterpreted.
    assert _save({"provider": "zoom", "kind": "webhook", "scopes": ["user:read:user"],
                  "token": "another-secret-123456"})[0] == 400
    assert _save({"provider": "zoom", "kind": "meetings"})[0] == 400
    assert _save({"provider": "dropbox", "kind": "api", "scopes": ["account_info.read"]})[0] == 400
    assert _save({"connection_id": "zoom", "provider": "zoom", "kind": "api"})[0] == 409
    # Editing a Zoom API connection's scopes (Manage → Advanced) stays OAuth.
    _, view = _save({"provider": "zoom", "kind": "api"})
    api_id = view["connection_id"]
    status, edited = _save({"connection_id": api_id, "provider": "zoom",
                            "scopes": ["user:read:user", "meeting:write:meeting"]})
    assert status == 200 and edited["scopes"] == ["meeting:write:meeting", "user:read:user"]
    assert f"oauth#{api_id}" not in secrets
    assert _save({"connection_id": api_id, "provider": "zoom", "scopes": []})[0] == 400
    assert _save({"connection_id": api_id, "provider": "zoom", "kind": "webhook",
                  "token": "another-secret-123456"})[0] == 409


def test_console_zoom_api_start_redirects_to_zoom_consent(monkeypatch):
    table, _ = setup_webhook(monkeypatch)
    _admin(monkeypatch, table)
    _, view = _save({"provider": "zoom", "kind": "api"})
    connection_id = view["connection_id"]
    monkeypatch.setattr(oauth_flow, "_connection", lambda cid: table.items.get(cid))
    monkeypatch.setenv("OAUTH_CALLBACK_URL", "https://fixed.example.test/oauth/callback")
    monkeypatch.setenv("ZOOM_OAUTH_CLIENT_ID", "zoom-client")
    monkeypatch.setenv("ZOOM_OAUTH_CLIENT_SECRET", "zoom-secret")
    monkeypatch.setattr(session, "_credentials", lambda: {"password": "session-secret"})
    response = oauth_flow.oauth_start({"headers": {"host": "fixed.example.test"}, "cookies": []},
                                      connection_id)
    assert response["statusCode"] == 302
    location = response["headers"]["location"]
    assert location.startswith("https://zoom.us/oauth/authorize?")
    query = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
    assert query["client_id"] == ["zoom-client"]
    assert sorted(query["scope"][0].split()) == DEFAULTS


def _operator(monkeypatch):
    tables = configure_agent(monkeypatch, claims={"sub": "op-1", "email": "op@datatalks.club"})
    monkeypatch.setenv("OPERATOR_EMAILS", "op@datatalks.club")
    return tables


def test_agent_create_zoom_api_and_connect(monkeypatch):
    tables = _operator(monkeypatch)
    created = agent_api.route(agent_event({"connection_id": "zoom-api", "provider": "zoom",
                                           "kind": "api"}),
                              "PUT", "/api/agent/connections")
    assert created["statusCode"] == 200, created["body"]
    item = tables["connections"].items["zoom-api"]
    assert item["scopes"] == DEFAULTS and item["status"] == "ready"

    monkeypatch.setenv("OAUTH_CALLBACK_URL", "https://fixed.example.test/oauth/callback")
    monkeypatch.setenv("ZOOM_OAUTH_CLIENT_ID", "zoom-client")
    monkeypatch.setenv("ZOOM_OAUTH_CLIENT_SECRET", "zoom-secret")
    monkeypatch.setattr(session, "_credentials", lambda: {"password": "session-secret"})
    started = agent_api.route(agent_event({"agent": "zoom-agent"}),
                              "POST", "/api/agent/connections/zoom-api/connect")
    assert started["statusCode"] == 200, started["body"]
    url = json.loads(started["body"])["authorize_url"]
    assert url.startswith("https://zoom.us/oauth/authorize?")
    scope = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["scope"][0]
    assert sorted(scope.split()) == DEFAULTS


def test_agent_create_zoom_kind_rules(monkeypatch):
    tables = _operator(monkeypatch)
    route = lambda body: agent_api.route(agent_event(body), "PUT", "/api/agent/connections")
    # A webhook needs its Secret Token: created through import, not create.
    webhook = route({"provider": "zoom", "kind": "webhook"})
    assert webhook["statusCode"] == 400 and "token-file" in webhook["body"]
    assert route({"provider": "zoom"})["statusCode"] == 400
    # Older clients that name scopes without a kind still get an API connection.
    legacy = route({"connection_id": "zoom-legacy", "provider": "zoom",
                    "scopes": ["user:read:user"]})
    assert legacy["statusCode"] == 200
    assert route({"provider": "google", "kind": "api",
                  "scopes": ["https://www.googleapis.com/auth/drive.readonly"]})["statusCode"] == 400
    # A scope edit never empties a Zoom API connection into a webhook.
    emptied = agent_api.route(agent_event({"scopes": []}), "PUT",
                              "/api/agent/connections/zoom-legacy")
    assert emptied["statusCode"] == 400
    assert tables["connections"].items["zoom-legacy"]["scopes"] == ["user:read:user"]


def test_cli_create_zoom_sends_api_kind_and_server_defaults(monkeypatch, capsys):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path, body))
        return {"connection_id": "zoom-api"}

    monkeypatch.setattr(commands.api, "call", fake_call)
    args = main.build_parser().parse_args(["connections", "create", "zoom-api", "--provider", "zoom"])
    assert args.scopes is None and args.kind is None
    assert main.main(["connections", "create", "zoom-api", "--provider", "zoom"]) == 0
    assert calls[-1] == ("PUT", "/api/agent/connections",
                         {"connection_id": "zoom-api", "provider": "zoom", "kind": "api"})
    assert "dapier connections connect zoom-api" in capsys.readouterr().out
    assert main.main(["connections", "create", "--provider", "zoom", "--kind", "api",
                      "--scopes", "user:read:user", "meeting:write:meeting"]) == 0
    assert calls[-1][2] == {"connection_id": None, "provider": "zoom", "kind": "api",
                            "scopes": ["user:read:user", "meeting:write:meeting"]}
    # Other providers still name their scopes.
    calls.clear()
    assert main.main(["connections", "create", "--provider", "dropbox"]) == 2
    assert calls == []
