"""Connection discovery: resource catalog, live listing, and health test.

Unit tests drive the domain module with a fake transport (the same
injectable seam oauth_providers uses); API tests cover both surfaces —
/api/agent/* with a bearer identity and /api/admin/* with a session cookie —
proving the console and the CLI reach the same domain behavior.
"""
import json
import time

import pytest

from src.dapier.api import admin
from src.dapier.api import agent as agent_api
from src.dapier.auth import session
from src.dapier.connections import credentials as credentials_module
from src.dapier.connections import discovery
from src.dapier.connections import tokens
from src.dapier.connections.providers import oauth_providers


class FakeTransport:
    """Route provider calls by URL substring to canned JSON responses."""

    def __init__(self, *routes):
        self.routes = routes  # (substring, status, payload) tuples
        self.calls = []

    def __call__(self, method, url, *, headers=None, body=None, timeout=15):
        self.calls.append({"method": method, "url": url, "headers": headers,
                           "body": body})
        for substring, status, payload in self.routes:
            if substring in url:
                return status, json.dumps(payload).encode()
        raise AssertionError(f"unexpected provider call: {method} {url}")


def paged_transport(pages):
    """Serve one canned JSON payload per call, recording each request."""
    calls = []

    def transport(method, url, *, headers=None, body=None, timeout=15):
        calls.append({"method": method, "url": url, "headers": headers,
                      "body": body})
        return 200, json.dumps(pages[len(calls) - 1]).encode()

    transport.calls = calls
    return transport


GOOGLE_CONNECTION = {
    "connection_id": "sheets-team", "provider": "google", "status": "connected",
    "credential_id": "oauth#sheets-team",
}
DROPBOX_CONNECTION = {
    "connection_id": "dbx", "provider": "dropbox", "status": "connected",
    "credential_id": "oauth#dbx", "root_path": "/team/shared",
}
SLACK_CONNECTION = {
    "connection_id": "slack-main", "provider": "slack", "status": "connected",
    "credential_id": "oauth#slack-main",
}
TELEGRAM_CONNECTION = {
    "connection_id": "tg", "provider": "telegram", "status": "connected",
    "credential_id": "oauth#tg",
}


def live_token(monkeypatch, refreshed=False):
    monkeypatch.setattr(
        discovery.tokens, "get_access_token",
        lambda connection, transport=None: ("tok", {"refreshed": refreshed}))


# --- domain: catalog and validation ---


def test_catalog_lists_resources_with_params():
    view = discovery.catalog_view(GOOGLE_CONNECTION)
    assert view["connection"] == "sheets-team"
    assert [resource["name"] for resource in view["resources"]] == \
        ["spreadsheets", "files", "folders", "worksheets", "columns", "rows",
         "calendars", "events", "labels"]
    worksheets = view["resources"][3]
    assert worksheets["params"][0] == {
        "name": "spreadsheet_id", "required": True,
        "description": "Spreadsheet ID from the spreadsheets list"}
    assert discovery.catalog_view({"provider": "google", "connection_id": "x"})["resources"]


def test_provider_without_discovery_lists_no_resources():
    assert discovery.catalog_view(
        {"connection_id": "hook", "provider": "webhook"})["resources"] == []


def test_discover_unknown_resource_is_404_naming_the_known_ones():
    with pytest.raises(discovery.DiscoveryError) as excinfo:
        discovery.discover(GOOGLE_CONNECTION, "bogus", {})
    assert excinfo.value.status == 404
    assert ("known: spreadsheets, files, folders, worksheets, columns, rows"
            in str(excinfo.value))


def test_discover_rejects_missing_required_param():
    with pytest.raises(discovery.DiscoveryError) as excinfo:
        discovery.discover(GOOGLE_CONNECTION, "worksheets", {})
    assert excinfo.value.status == 400
    assert "worksheets needs spreadsheet_id" in str(excinfo.value)


def test_discover_rejects_unknown_param():
    with pytest.raises(discovery.DiscoveryError) as excinfo:
        discovery.discover(GOOGLE_CONNECTION, "spreadsheets", {"spreadsheet_id": "x"})
    assert excinfo.value.status == 400
    assert "Unknown parameter 'spreadsheet_id'" in str(excinfo.value)


# --- domain: per-provider fetchers ---


def test_google_spreadsheets_lists_drive_files_with_mime_filter(monkeypatch):
    live_token(monkeypatch)
    transport = FakeTransport(
        ("drive/v3/files", 200, {"files": [
            {"id": "s1", "name": "Invoices", "mimeType": discovery.SPREADSHEET_MIME,
             "modifiedTime": "2026-09-01T10:00:00Z"},
            {"id": "s2", "name": "Todo"},
        ]}))
    items = discovery.discover(GOOGLE_CONNECTION, "spreadsheets", {}, transport=transport)
    assert items[0] == {"id": "s1", "name": "Invoices",
                        "mimeType": discovery.SPREADSHEET_MIME,
                        "modified": "2026-09-01T10:00:00Z"}
    assert items[1]["name"] == "Todo"
    assert "mimeType" in transport.calls[0]["url"]
    assert "authorization" in transport.calls[0]["headers"]


def test_drive_files_follow_the_next_page_token_across_pages(monkeypatch):
    live_token(monkeypatch)
    transport = paged_transport([
        {"files": [
            {"id": "f1", "name": "First", "mimeType": "text/plain",
             "modifiedTime": "2026-09-01T10:00:00Z"},
        ], "nextPageToken": "tok1"},
        {"files": [
            {"id": "f2", "name": "Second", "mimeType": "text/plain"},
        ]},
    ])
    items = discovery.discover(GOOGLE_CONNECTION, "files", {}, transport=transport)
    assert [item["id"] for item in items] == ["f1", "f2"]
    assert len(transport.calls) == 2
    assert "pageToken" not in transport.calls[0]["url"]
    assert "pageToken=tok1" in transport.calls[1]["url"]


def test_google_columns_reads_the_header_row(monkeypatch):
    live_token(monkeypatch)
    transport = FakeTransport(
        ("/values/", 200, {"values": [["When", "What"], ["2026-01-01", "kickoff"]]}))
    items = discovery.discover(GOOGLE_CONNECTION, "columns",
                               {"spreadsheet_id": "s1", "worksheet": "Sheet1", "limit": "zz"},
                               transport=transport)
    assert items == [{"id": "col1", "name": "When"}, {"id": "col2", "name": "What"}]
    assert "/values/Sheet1%21A1" in transport.calls[0]["url"]


def test_google_worksheets_lists_tabs(monkeypatch):
    live_token(monkeypatch)
    transport = FakeTransport(
        ("/spreadsheets/", 200, {"sheets": [
            {"properties": {"sheetId": 7, "title": "Todo",
                            "gridProperties": {"rowCount": 100, "columnCount": 9}}},
            {"properties": {"sheetId": 8, "title": "Done"}},
        ]}))
    items = discovery.discover(GOOGLE_CONNECTION, "worksheets",
                               {"spreadsheet_id": "s1"}, transport=transport)
    assert items[0] == {"id": "7", "name": "Todo", "rowCount": 100, "columnCount": 9}
    assert items[1]["name"] == "Done"


def test_drive_folders_filter_on_the_folder_mime_type(monkeypatch):
    live_token(monkeypatch)
    transport = FakeTransport(
        ("drive/v3/files", 200, {"files": [
            {"id": "f1", "name": "Invoices",
             "mimeType": discovery.FOLDER_MIME,
             "modifiedTime": "2026-09-01T10:00:00Z"},
        ]}))
    items = discovery.discover(GOOGLE_CONNECTION, "folders", {}, transport=transport)
    assert items == [{"id": "f1", "name": "Invoices",
                      "mimeType": discovery.FOLDER_MIME,
                      "modified": "2026-09-01T10:00:00Z"}]
    assert "mimeType%3D%27application%2Fvnd.google-apps.folder%27" \
        in transport.calls[0]["url"]


def test_drive_folders_provider_error_is_a_502(monkeypatch):
    live_token(monkeypatch)
    transport = FakeTransport(
        ("drive/v3/files", 403, {"error": {"message": "insufficient permissions"}}))
    with pytest.raises(discovery.DiscoveryError) as excinfo:
        discovery.discover(GOOGLE_CONNECTION, "folders", {}, transport=transport)
    assert excinfo.value.status == 502
    assert "insufficient permissions" in str(excinfo.value)


def test_sheets_rows_read_the_first_rows_and_trim_empty_cells(monkeypatch):
    live_token(monkeypatch)
    transport = FakeTransport(
        ("/values/", 200, {"values": [
            ["When", "What", ""],
            ["2026-01-01", "kickoff", "", ""],
            [],
        ]}))
    items = discovery.discover(GOOGLE_CONNECTION, "rows",
                               {"spreadsheet_id": "s1"}, transport=transport)
    assert items == [
        {"id": "1", "row": 1, "values": ["When", "What"]},
        {"id": "2", "row": 2, "values": ["2026-01-01", "kickoff"]},
        {"id": "3", "row": 3, "values": []},
    ]
    assert "/values/Sheet1%21A1" in transport.calls[0]["url"]


def test_sheets_rows_respect_the_limit_in_the_range(monkeypatch):
    live_token(monkeypatch)
    transport = FakeTransport(("/values/", 200, {"values": [["a"], ["b"], ["c"]]}))
    items = discovery.discover(GOOGLE_CONNECTION, "rows",
                               {"spreadsheet_id": "s1", "worksheet": "Todo"},
                               limit=3, transport=transport)
    assert len(items) == 3
    assert "/values/Todo%21A1%3AZZ3" in transport.calls[0]["url"]


def test_sheets_rows_need_a_spreadsheet_id(monkeypatch):
    with pytest.raises(discovery.DiscoveryError) as excinfo:
        discovery.discover(GOOGLE_CONNECTION, "rows", {})
    assert excinfo.value.status == 400
    assert "rows needs spreadsheet_id" in str(excinfo.value)


def test_slack_channels_use_the_stored_token(monkeypatch):
    monkeypatch.setattr(discovery.credentials, "get_credential",
                        lambda credential_id: {"token": "xoxb-fake-token"})
    transport = FakeTransport(
        ("conversations.list", 200, {"ok": True, "channels": [
            {"id": "C1", "name": "general", "is_private": False},
            {"id": "C2", "name": "secret", "is_private": True},
        ]}))
    items = discovery.discover(SLACK_CONNECTION, "channels", {}, transport=transport)
    assert items == [{"id": "C1", "name": "general", "type": "channel"},
                     {"id": "C2", "name": "secret", "type": "private"}]
    assert transport.calls[0]["headers"]["authorization"] == "Bearer xoxb-fake-token"


def test_slack_channels_follow_the_cursor_and_stop_at_limit(monkeypatch):
    monkeypatch.setattr(discovery.credentials, "get_credential",
                        lambda credential_id: {"token": "xoxb-fake-token"})
    transport = paged_transport([
        {"ok": True, "channels": [
            {"id": "C1", "name": "general", "is_private": False},
            {"id": "C2", "name": "secret", "is_private": True},
        ], "response_metadata": {"next_cursor": "cur1"}},
        {"ok": True, "channels": [
            {"id": "C3", "name": "help", "is_private": False},
            {"id": "C4", "name": "random", "is_private": False},
        ], "response_metadata": {"next_cursor": "cur2"}},
    ])
    items = discovery.discover(SLACK_CONNECTION, "channels", {}, limit=3,
                               transport=transport)
    assert [item["id"] for item in items] == ["C1", "C2", "C3"]
    assert len(transport.calls) == 2
    assert "cursor" not in json.loads(transport.calls[0]["body"])
    assert json.loads(transport.calls[1]["body"])["cursor"] == "cur1"


def test_slack_users_follow_the_cursor_and_stop_when_it_runs_out(monkeypatch):
    monkeypatch.setattr(discovery.credentials, "get_credential",
                        lambda credential_id: {"token": "xoxb-fake-token"})
    transport = paged_transport([
        {"ok": True, "members": [
            {"id": "U1", "name": "one",
             "profile": {"real_name": "Person One", "email": "one@example.test"}},
            {"id": "U2", "name": "two", "profile": {"real_name": "Person Two"}},
        ], "response_metadata": {"next_cursor": "cur1"}},
        {"ok": True, "members": [
            {"id": "U3", "name": "three", "profile": {}},
        ]},
    ])
    items = discovery.discover(SLACK_CONNECTION, "users", {}, transport=transport)
    assert [item["id"] for item in items] == ["U1", "U2", "U3"]
    assert [item["name"] for item in items] == ["Person One", "Person Two", "three"]
    # Email rides along only when Slack reveals it: slack_find_user's email
    # field picks from this listing.
    assert items[0].get("email") == "one@example.test"
    assert "email" not in items[1] and "email" not in items[2]
    assert len(transport.calls) == 2
    assert json.loads(transport.calls[1]["body"])["cursor"] == "cur1"


def test_slack_error_becomes_a_502(monkeypatch):
    monkeypatch.setattr(discovery.credentials, "get_credential",
                        lambda credential_id: {"token": "xoxb-fake-token"})
    transport = FakeTransport(("conversations.list", 200, {"ok": False, "error": "missing_scope"}))
    with pytest.raises(discovery.DiscoveryError) as excinfo:
        discovery.discover(SLACK_CONNECTION, "channels", {}, transport=transport)
    assert excinfo.value.status == 502
    assert "missing_scope" in str(excinfo.value)


def test_dropbox_folder_defaults_to_the_connection_root_path(monkeypatch):
    live_token(monkeypatch)
    transport = FakeTransport(
        ("list_folder", 200, {"entries": [
            {"id": "id:1", "name": "notes.txt", ".tag": "file",
             "path_display": "/team/shared/notes.txt"},
            {"id": "id:2", "name": "Archive", ".tag": "folder",
             "path_display": "/team/shared/Archive"},
        ]}))
    items = discovery.discover(DROPBOX_CONNECTION, "folder", {}, transport=transport)
    assert items[1] == {"id": "id:2", "name": "Archive", "type": "folder",
                        "path": "/team/shared/Archive"}
    assert json.loads(transport.calls[0]["body"])["path"] == "/team/shared"


def test_dropbox_folder_without_a_root_path_lists_the_drive_root(monkeypatch):
    live_token(monkeypatch)
    connection = {"connection_id": "dbx", "provider": "dropbox",
                  "status": "connected", "credential_id": "oauth#dbx"}
    transport = paged_transport([{"entries": [], "has_more": False}])
    items = discovery.discover(connection, "folder", {}, transport=transport)
    assert items == []
    assert json.loads(transport.calls[0]["body"])["path"] == ""


def test_dropbox_folder_sends_a_root_slash_root_path_verbatim(monkeypatch):
    live_token(monkeypatch)
    connection = {"connection_id": "dbx", "provider": "dropbox",
                  "status": "connected", "credential_id": "oauth#dbx",
                  "root_path": "/"}
    transport = paged_transport([{"entries": [], "has_more": False}])
    items = discovery.discover(connection, "folder", {}, transport=transport)
    assert items == []
    assert json.loads(transport.calls[0]["body"])["path"] == "/"


def test_dropbox_search_unwraps_the_metadata_nesting(monkeypatch):
    live_token(monkeypatch)
    transport = FakeTransport(
        ("search_v2", 200, {"matches": [
            {"metadata": {"metadata": {
                "id": "id:1", "name": "invoice-4137.pdf", ".tag": "file",
                "path_display": "/team/shared/invoice-4137.pdf"}}},
            {"metadata": {"metadata": {
                "id": "id:2", "name": "Archive", ".tag": "folder",
                "path_display": "/team/shared/Archive"}}},
        ]}))
    items = discovery.discover(DROPBOX_CONNECTION, "search",
                               {"query": "invoice", "limit": "2"},
                               transport=transport)
    assert items == [
        {"id": "id:1", "name": "invoice-4137.pdf", "type": "file",
         "path": "/team/shared/invoice-4137.pdf"},
        {"id": "id:2", "name": "Archive", "type": "folder",
         "path": "/team/shared/Archive"},
    ]
    body = json.loads(transport.calls[0]["body"])
    assert body == {"query": "invoice",
                    "options": {"max_results": 2},
                    "include_highlights": False}


def test_dropbox_search_tolerates_unnested_metadata(monkeypatch):
    live_token(monkeypatch)
    transport = FakeTransport(
        ("search_v2", 200, {"matches": [
            {"metadata": {"id": "id:9", "name": "direct.pdf", ".tag": "file",
                          "path_display": "/direct.pdf"}},
            {"metadata": {}},
        ]}))
    items = discovery.discover(DROPBOX_CONNECTION, "search",
                               {"query": "direct"}, transport=transport)
    assert items == [{"id": "id:9", "name": "direct.pdf", "type": "file",
                      "path": "/direct.pdf"}]


def test_dropbox_search_needs_a_query(monkeypatch):
    with pytest.raises(discovery.DiscoveryError) as excinfo:
        discovery.discover(DROPBOX_CONNECTION, "search", {})
    assert excinfo.value.status == 400
    assert "search needs query" in str(excinfo.value)


def test_telegram_updates_normalize_chats(monkeypatch):
    monkeypatch.setattr(discovery.credentials, "get_credential",
                        lambda credential_id: {"token": "12:ABC"})
    transport = FakeTransport(
        ("getUpdates", 200, {"ok": True, "result": [
            {"update_id": 5, "message": {"chat": {"id": -10022, "title": "Bots",
                                                  "type": "channel"},
                                         "text": "hello there"}},
        ]}))
    items = discovery.discover(TELEGRAM_CONNECTION, "updates", {}, transport=transport)
    assert items == [{"id": "5", "name": "chat Bots", "chat_id": -10022,
                      "chat_type": "channel", "text": "hello there"}]


def test_zoom_meetings_list_upcoming(monkeypatch):
    live_token(monkeypatch)
    transport = FakeTransport(
        ("users/me/meetings", 200, {"meetings": [
            {"id": 999, "topic": "Standup", "start_time": "2026-09-28T09:00:00Z",
             "join_url": "https://zoom.us/j/999"},
        ]}))
    items = discovery.discover(
        {"connection_id": "z", "provider": "zoom", "status": "connected",
         "credential_id": "oauth#z"},
        "meetings", {}, transport=transport)
    assert items[0]["name"] == "Standup"


def test_provider_error_becomes_a_502_with_detail(monkeypatch):
    live_token(monkeypatch)
    transport = FakeTransport(
        ("drive/v3/files", 403, {"error": {"message": "The grant has no drive scope"}}))
    with pytest.raises(discovery.DiscoveryError) as excinfo:
        discovery.discover(GOOGLE_CONNECTION, "spreadsheets", {}, transport=transport)
    assert excinfo.value.status == 502
    assert "drive scope" in str(excinfo.value)


# --- domain: test_connection ---


def test_connection_reports_refreshed_token_and_identity(monkeypatch):
    live_token(monkeypatch, refreshed=True)
    transport = FakeTransport(("userinfo", 200, {"email": "op@datatalks.club",
                                                 "name": "Operator", "email_verified": True}))
    payload = discovery.test_connection(GOOGLE_CONNECTION, transport=transport)
    assert payload["ok"] is True
    assert payload["detail"] == "Refreshed the Google access token"
    assert payload["identity"] == {"id": "op@datatalks.club", "name": "Operator"}


def test_connection_failure_answers_ok_false_instead_of_raising(monkeypatch):
    def broken(connection, transport=None):
        raise tokens.TokenError("refresh token expired")

    monkeypatch.setattr(discovery.tokens, "get_access_token", broken)
    payload = discovery.test_connection(GOOGLE_CONNECTION)
    assert payload["ok"] is False
    assert "refresh token expired" in payload["detail"]
    assert "identity" not in payload


def test_slack_connection_test_verifies_the_workspace(monkeypatch):
    monkeypatch.setattr(discovery.credentials, "get_credential",
                        lambda credential_id: {"token": "xoxb-fake-token"})
    transport = FakeTransport(("auth.test", 200, {"ok": True, "team_id": "T1",
                                                  "team": "DataTalks"}))
    payload = discovery.test_connection(SLACK_CONNECTION, transport=transport)
    assert payload["ok"] is True
    assert payload["identity"] == {"id": "T1", "name": "DataTalks"}


# --- API surface: /api/agent/* (CLI bearer) ---


class Table:
    def __init__(self, items=None):
        self.items = items or {}

    def get_item(self, **kwargs):
        item = self.items.get(kwargs["Key"].get("connection_id"))
        return {"Item": dict(item)} if item else {}

    def put_item(self, **kwargs):
        self.items[kwargs["Item"]["connection_id"]] = kwargs["Item"]

    def scan(self, **kwargs):
        return {"Items": list(self.items.values())}


def configure_agent(monkeypatch, *, claims=None, connections=None):
    agent_api.reset_rate_limits()
    tables = {"connections": Table(connections or {}), "grants": Table(),
              "credentials": Table(), "api-tokens": Table()}

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


def agent_event(query=None, token="dtc-id-token", body=None):
    headers = {"host": "dapier.example.test"}
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    request = {"headers": headers, "cookies": []}
    if query is not None:
        request["queryStringParameters"] = query
    if body is not None:
        request["body"] = json.dumps(body)
    return request


def test_agent_discover_requires_an_operator(monkeypatch):
    configure_agent(monkeypatch, claims={"sub": "subject-1", "email": "op@datatalks.club"})
    monkeypatch.setenv("OPERATOR_EMAILS", "boss@example.test")
    response = agent_api.route(
        agent_event(), "GET", "/api/agent/connections/sheets-team/discover")
    assert response["statusCode"] == 403


def test_agent_discover_catalog(monkeypatch):
    configure_agent(monkeypatch, claims={"sub": "subject-1", "email": "op@datatalks.club"},
                    connections={"sheets-team": GOOGLE_CONNECTION})
    response = agent_api.route(
        agent_event(), "GET", "/api/agent/connections/sheets-team/discover")
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["provider"] == "google"
    assert "spreadsheets" in [resource["name"] for resource in body["resources"]]


def test_agent_discover_items(monkeypatch):
    configure_agent(monkeypatch, claims={"sub": "subject-1", "email": "op@datatalks.club"},
                    connections={"sheets-team": GOOGLE_CONNECTION})
    live_token(monkeypatch)
    monkeypatch.setattr(discovery, "_default_transport", FakeTransport(
        ("spreadsheets/s1", 200, {"sheets": [
            {"properties": {"sheetId": 7, "title": "Todo"}}]})))
    response = agent_api.route(
        agent_event(query={"spreadsheet_id": "s1", "limit": "5"}),
        "GET", "/api/agent/connections/sheets-team/discover/worksheets")
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["params"] == {"spreadsheet_id": "s1"}
    assert body["items"][0]["name"] == "Todo"


def test_agent_discover_unknown_connection_is_404(monkeypatch):
    configure_agent(monkeypatch, claims={"sub": "subject-1", "email": "op@datatalks.club"})
    response = agent_api.route(
        agent_event(), "GET", "/api/agent/connections/nope/discover")
    assert response["statusCode"] == 404
    assert "Unknown connection 'nope'" in json.loads(response["body"])["error"]


def test_agent_discover_unknown_resource_is_404(monkeypatch):
    configure_agent(monkeypatch, claims={"sub": "subject-1", "email": "op@datatalks.club"},
                    connections={"sheets-team": GOOGLE_CONNECTION})
    response = agent_api.route(
        agent_event(), "GET", "/api/agent/connections/sheets-team/discover/bogus")
    assert response["statusCode"] == 404
    assert "known:" in json.loads(response["body"])["error"]


def test_agent_connection_test(monkeypatch):
    configure_agent(monkeypatch, claims={"sub": "subject-1", "email": "op@datatalks.club"},
                    connections={"sheets-team": GOOGLE_CONNECTION})
    live_token(monkeypatch)
    monkeypatch.setattr(oauth_providers, "verify_account",
                        lambda provider, token, transport=None: ("UC1", "My Channel"))
    response = agent_api.route(
        agent_event(body={}), "POST", "/api/agent/connections/sheets-team/test")
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["ok"] is True
    assert body["identity"] == {"id": "UC1", "name": "My Channel"}


# --- API surface: /api/admin/* (console session cookie) ---


def configure_admin(monkeypatch, connections=None):
    tables = {"connections": Table(connections or {}), "credentials": Table()}

    class Dynamo:
        def Table(self, name):
            return tables[name]

    import boto3

    monkeypatch.setenv("CONNECTIONS_TABLE", "connections")
    monkeypatch.setenv("CREDENTIALS_TABLE", "credentials")
    monkeypatch.delenv("AUDIT_TABLE", raising=False)
    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())
    monkeypatch.setattr(session, "_credentials",
                        lambda: {"username": "admin", "password": "pw"})
    cookie = session._sign({"sub": "op@datatalks.club", "subject": "op-sub",
                            "exp": int(time.time()) + 600})
    return [f"dapier_session={cookie}"]


def admin_request(method, path, query=None, cookies=None, body=None):
    event = {
        "requestContext": {"http": {"method": method, "path": path}},
        "headers": {"host": "dapier.example.test",
                    "origin": "https://dapier.example.test"},
        "cookies": cookies or [],
    }
    if query is not None:
        event["queryStringParameters"] = query
    if body is not None:
        event["body"] = json.dumps(body)
    return event


def test_admin_discover_catalog_and_items(monkeypatch):
    cookies = configure_admin(monkeypatch, connections={"sheets-team": GOOGLE_CONNECTION})
    live_token(monkeypatch)
    monkeypatch.setattr(discovery, "_default_transport", FakeTransport(
        ("spreadsheets/s1", 200, {"sheets": [
            {"properties": {"sheetId": 7, "title": "Todo"}}]})))
    catalog = admin.route(
        admin_request("GET", "/api/admin/connections/sheets-team/discover", cookies=cookies),
        "GET", "/api/admin/connections/sheets-team/discover")
    assert catalog["statusCode"] == 200
    items = admin.route(
        admin_request("GET", "/api/admin/connections/sheets-team/discover/worksheets",
                      query={"spreadsheet_id": "s1"}, cookies=cookies),
        "GET", "/api/admin/connections/sheets-team/discover/worksheets")
    assert items["statusCode"] == 200
    assert json.loads(items["body"])["items"][0]["name"] == "Todo"


def test_admin_connection_test(monkeypatch):
    cookies = configure_admin(monkeypatch, connections={"sheets-team": GOOGLE_CONNECTION})
    live_token(monkeypatch)
    monkeypatch.setattr(oauth_providers, "verify_account",
                        lambda provider, token, transport=None: ("UC1", "My Channel"))
    response = admin.route(
        admin_request("POST", "/api/admin/connections/sheets-team/test", body={},
                      cookies=cookies),
        "POST", "/api/admin/connections/sheets-team/test")
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["ok"] is True


def test_admin_discover_requires_signin(monkeypatch):
    configure_admin(monkeypatch, connections={"sheets-team": GOOGLE_CONNECTION})
    response = admin.route(
        admin_request("GET", "/api/admin/connections/sheets-team/discover"),
        "GET", "/api/admin/connections/sheets-team/discover")
    assert response["statusCode"] == 401


# --- registry contract: the connector registry is the discovery catalog of
# record; api.discovery serves the resources it lists, and GET /api/catalog
# publishes the metadata (with each action's field-level ``discover`` hint)
# so the console and CLI can offer the same pickers.


def test_registry_discovers_are_keyed_sorted_and_described():
    from src.dapier.connectors import registry

    for key, entry in registry.DISCOVERIES.items():
        assert key == f"{entry.connector}.{entry.name}"
        assert entry.label and entry.description
        assert callable(entry.run)
        for param in entry.params:
            assert param.get("key")
    assert registry.discoveries() == [
        registry.DISCOVERIES[key] for key in sorted(registry.DISCOVERIES)]


def test_registry_provider_view_groups_the_google_sources():
    from src.dapier.connectors import registry

    google = [entry.name for entry in registry.discoveries_for_provider("google")]
    assert {"spreadsheets", "worksheets", "columns", "files", "folders", "rows"} <= set(google)
    assert registry.discoveries_for_provider("webhook") == []
    assert registry.discoveries_for_provider("dropbox") == [
        registry.DISCOVERIES["dropbox.files"], registry.DISCOVERIES["dropbox.folders"],
        registry.DISCOVERIES["dropbox.search"]]


def test_catalog_publishes_discoveries_and_field_discover_metadata():
    from src.dapier.connectors import registry

    catalog = registry.catalog()
    listed = {f"{entry['connector']}.{entry['name']}" for entry in catalog["discoveries"]}
    assert "google-sheets.worksheets" in listed
    assert "slack.channels" in listed
    assert "s3.buckets" in listed
    worksheets = next(entry for entry in catalog["discoveries"]
                      if entry["name"] == "worksheets")
    assert worksheets["connector"] == "google-sheets"
    assert worksheets["params"][0]["required"] is True

    action = next(action for action in catalog["actions"]
                  if action["type"] == "sheets_append_row")
    spreadsheet_id = next(field for field in action["fields"]
                          if field.get("key") == "spreadsheet_id")
    assert spreadsheet_id["discover"] == {"resource": "google-sheets.spreadsheets"}
    assert spreadsheet_id.get("help")
    sheet_name = next(field for field in action["fields"]
                      if field.get("key") == "sheet_name")
    assert sheet_name["discover"] == {
        "resource": "google-sheets.worksheets",
        "params": {"spreadsheet_id": "spreadsheet_id"},
    }


def test_registry_runners_list_live_items(monkeypatch):
    from src.dapier.connectors import registry

    live_token(monkeypatch)

    def fake_transport(method, url, *, headers=None, body=None, timeout=15):
        assert headers["authorization"] == "Bearer tok"
        return 200, json.dumps({"sheets": [
            {"properties": {"sheetId": 7, "title": "Todo"}}]}).encode()

    monkeypatch.setattr(discovery, "_default_transport", fake_transport)
    items = registry.DISCOVERIES["google-sheets.worksheets"].run(
        GOOGLE_CONNECTION, {"spreadsheet_id": "s1"})
    assert items == [{"id": "7", "name": "Todo", "rowCount": None, "columnCount": None}]


class FakeS3Objects:
    """One canned list_objects_v2 response, with the call kwargs recorded."""

    def __init__(self, contents):
        self.contents = contents
        self.calls = []

    def list_objects_v2(self, **kwargs):
        self.calls.append(kwargs)
        return {"Contents": self.contents}


def test_s3_objects_discovery_passes_the_prefix_through(monkeypatch):
    import boto3

    from src.dapier.connectors import registry

    fake = FakeS3Objects([{"Key": "reports/2026/report.pdf", "Size": 9}])
    monkeypatch.setattr(discovery.credentials, "get_credential",
                        lambda credential_id: {"access_key_id": "AKIAX",
                                               "secret_access_key": "b" * 40})
    monkeypatch.setattr(boto3, "client", lambda service, **kwargs: fake)
    items = registry.DISCOVERIES["s3.objects"].run(
        {"credential_id": "aws"}, {"bucket": "backups", "prefix": "reports/2026/"})
    assert items == [{"id": "reports/2026/report.pdf", "name": "report.pdf",
                      "size": 9, "modified": ""}]
    assert fake.calls == [{"Bucket": "backups", "MaxKeys": 100,
                           "Prefix": "reports/2026/"}]


def test_s3_objects_discovery_omits_the_prefix_when_absent(monkeypatch):
    import boto3

    from src.dapier.connectors import registry

    fake = FakeS3Objects([])
    monkeypatch.setattr(discovery.credentials, "get_credential",
                        lambda credential_id: {"access_key_id": "AKIAX",
                                               "secret_access_key": "b" * 40})
    monkeypatch.setattr(boto3, "client", lambda service, **kwargs: fake)
    registry.DISCOVERIES["s3.objects"].run(
        {"credential_id": "aws"}, {"bucket": "backups"})
    assert "Prefix" not in fake.calls[0]
