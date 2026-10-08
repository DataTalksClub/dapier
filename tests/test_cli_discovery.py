import pytest

from dapier_cli import commands, main
from dapier_cli.api import ApiError


@pytest.fixture(autouse=True)
def literal_connection_refs(monkeypatch):
    """These tests pin discover/test routing; the reference resolver has
    its own tests (test_connection_refs)."""
    monkeypatch.setattr(commands, "resolve_connection_id",
                        lambda api_url, words, agent=None, debug=False: " ".join(words))


@pytest.fixture
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("DAPIER_API_URL", raising=False)
    return tmp_path


def test_discover_without_resource_lists_resources(isolated_home, monkeypatch, capsys):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path))
        return {"connection": "sheets-team", "resources": [
            {"name": "spreadsheets", "label": "Spreadsheets",
             "description": "Spreadsheets shared with the connection", "params": []},
            {"name": "worksheets", "label": "Worksheets", "description": "Tabs in one spreadsheet",
             "params": [{"name": "spreadsheet_id", "required": True}]},
            {"name": "columns", "label": "Columns", "description": "Headers of one worksheet",
             "params": [{"name": "spreadsheet_id", "required": True},
                        {"name": "worksheet", "required": False}]},
        ]}

    monkeypatch.setattr(commands.api, "call", fake_call)

    assert main.main(["connections", "discover", "sheets-team"]) == 0

    assert calls == [("GET", "/api/agent/connections/sheets-team/discover")]
    out = capsys.readouterr().out
    assert "spreadsheets" in out and "worksheets" in out and "columns" in out
    assert "Spreadsheets shared with the connection" in out
    assert "spreadsheet_id*" in out  # required mark
    assert "worksheet" in out


def test_discover_with_resource_fetches_items_and_renders_table(isolated_home, monkeypatch, capsys):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path))
        assert path == ("/api/agent/connections/sheets-team/discover/worksheets"
                        "?spreadsheet_id=abc123")
        return {"connection": "sheets-team", "resource": "worksheets",
                "params": {"spreadsheet_id": "abc123"},
                "items": [
                    {"id": "sheet-invoices", "name": "Invoices", "rowCount": 42},
                    {"id": "sheet-todo", "name": "Todo"},
                ]}

    monkeypatch.setattr(commands.api, "call", fake_call)

    assert main.main(["connections", "discover", "sheets-team", "worksheets",
                      "--param", "spreadsheet_id=abc123"]) == 0

    assert len(calls) == 1
    out = capsys.readouterr().out
    assert "sheet-invoices" in out and "Invoices" in out
    assert "rowCount=42" in out
    assert "sheet-todo" in out


def test_discover_accepts_repeated_and_grouped_params(isolated_home, monkeypatch):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path))
        return {"connection": "sheets-team", "resource": "columns",
                "params": {}, "items": []}

    monkeypatch.setattr(commands.api, "call", fake_call)

    assert main.main(["connections", "discover", "sheets-team", "columns",
                      "--param", "spreadsheet_id=abc", "--param", "worksheet=Invoices"]) == 0
    assert main.main(["connections", "discover", "sheets-team", "columns",
                      "--param", "spreadsheet_id=abc", "worksheet=Invoices"]) == 0

    assert calls == [
        ("GET", "/api/agent/connections/sheets-team/discover/columns"
                "?spreadsheet_id=abc&worksheet=Invoices"),
        ("GET", "/api/agent/connections/sheets-team/discover/columns"
                "?spreadsheet_id=abc&worksheet=Invoices"),
    ]


def test_discover_rejects_malformed_params_without_calling(isolated_home, monkeypatch, capsys):
    monkeypatch.setattr(
        commands.api, "call",
        lambda *a, **k: pytest.fail("no request expected for a malformed param"))

    assert main.main(["connections", "discover", "sheets-team", "worksheets",
                      "--param", "spreadsheet_id"]) == 2

    assert "KEY=VALUE" in capsys.readouterr().out


def test_discover_reports_unknown_connection_through_the_error_path(isolated_home, monkeypatch, capsys):
    def fake_call(api_url, method, path, body=None, **kwargs):
        raise ApiError("Unknown connection 'nope'", status=404)

    monkeypatch.setattr(commands.api, "call", fake_call)

    assert main.main(["connections", "discover", "nope"]) == 4
    assert "Unknown connection" in capsys.readouterr().out


def test_discover_reports_unknown_resource_through_the_error_path(isolated_home, monkeypatch, capsys):
    def fake_call(api_url, method, path, body=None, **kwargs):
        raise ApiError("Unknown resource 'bogus' for google; known: spreadsheets, worksheets",
                       status=404)

    monkeypatch.setattr(commands.api, "call", fake_call)

    assert main.main(["connections", "discover", "sheets-team", "bogus"]) == 4
    assert "known: spreadsheets, worksheets" in capsys.readouterr().out


def test_connections_test_prints_ok_and_exits_zero(isolated_home, monkeypatch, capsys):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path, body))
        return {"ok": True, "detail": "Refreshed the Google access token",
                "identity": {"email": "op@datatalks.club", "id": "UC1"}}

    monkeypatch.setattr(commands.api, "call", fake_call)

    assert main.main(["connections", "test", "sheets-team"]) == 0

    assert calls == [("POST", "/api/agent/connections/sheets-team/test", {})]
    out = capsys.readouterr().out
    assert "OK — Refreshed the Google access token" in out
    assert "email=op@datatalks.club" in out and "id=UC1" in out


def test_connections_test_prints_failed_and_exits_one(isolated_home, monkeypatch, capsys):
    def fake_call(api_url, method, path, body=None, **kwargs):
        assert (method, path) == ("POST", "/api/agent/connections/sheets-team/test")
        return {"ok": False, "detail": "Google returned 401 for token refresh"}

    monkeypatch.setattr(commands.api, "call", fake_call)

    assert main.main(["connections", "test", "sheets-team"]) == 1

    out = capsys.readouterr().out
    assert "FAILED — Google returned 401 for token refresh" in out
    assert "Identity:" not in out  # no identity summary when the test fails


def test_connections_test_reports_unknown_connection(isolated_home, monkeypatch, capsys):
    def fake_call(api_url, method, path, body=None, **kwargs):
        raise ApiError("Unknown connection 'nope'", status=404)

    monkeypatch.setattr(commands.api, "call", fake_call)

    assert main.main(["connections", "test", "nope"]) == 4
    assert "Unknown connection" in capsys.readouterr().out
