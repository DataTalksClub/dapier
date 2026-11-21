import json
from unittest.mock import Mock

import pytest
from src.dapier.api.agent import gdocs
from dapier_cli import api
from dapier_cli.commands import gdocs as cli
from dapier_cli.cli import build_parser


@pytest.fixture
def route(monkeypatch):
    monkeypatch.setattr(gdocs, "require_operator", lambda event, action: ("operator", None))
    monkeypatch.setattr(gdocs, "audit", Mock())
    return lambda method, body=None: gdocs.route_gdocs(
        {"body": json.dumps(body), "queryStringParameters": {"account": "a@gmail.com"}},
        method, "/api/agent/gdocs/documents/doc" + ("/batch-update" if method == "POST" else ""))


def test_full_structure_and_guarded_write(route, monkeypatch):
    calls = []
    monkeypatch.setattr(gdocs, "_call", lambda *args: calls.append(args) or {"tabs": [{"documentTab": {"body": {"content": [{"table": {}}]}}}], "revisionId": "r1"})
    result = route("GET")
    assert json.loads(result["body"])["tabs"][0]["documentTab"]["body"]["content"][0] == {"table": {}}
    assert calls[-1] == ("a@gmail.com", "doc", "GET", None)
    requests = [{"insertText": {"location": {"index": 1}, "text": "hi"}}]
    assert route("POST", {"account": "b@gmail.com", "requests": requests, "required_revision_id": "r1"})["statusCode"] == 200
    assert calls[-1] == ("b@gmail.com", "doc", "POST", {"requests": requests, "writeControl": {"requiredRevisionId": "r1"}})


@pytest.mark.parametrize("body", [[], {}, {"requests": []}, {"requests": [{}], "required_revision_id": "r"}, {"requests": [{"insertText": {}}]}])
def test_rejects_unguarded_or_invalid_write(route, monkeypatch, body):
    provider = Mock()
    monkeypatch.setattr(gdocs, "_call", provider)
    assert route("POST", body)["statusCode"] == 400
    provider.assert_not_called()


def test_operator_gate(monkeypatch):
    monkeypatch.setattr(gdocs, "require_operator", lambda *args: (None, {"statusCode": 403}))
    provider = Mock()
    monkeypatch.setattr(gdocs, "_call", provider)
    assert gdocs.route_gdocs({}, "GET", "/api/agent/gdocs/documents/doc")["statusCode"] == 403
    provider.assert_not_called()


def test_server_account_and_tabs(monkeypatch):
    from src.dapier.api import discovery
    resolver = Mock(return_value="selected")
    monkeypatch.setattr(gdocs.google_accounts, "resolve_account", resolver)
    monkeypatch.setattr(discovery, "_load", lambda cid, table: ({"connection_id": cid}, None))
    monkeypatch.setattr(gdocs.tokens, "get_access_token", lambda conn: ("secret", {}))
    provider = Mock(return_value={"revisionId": "r"})
    monkeypatch.setattr(gdocs.provider, "_request", provider)
    assert gdocs._call("specific@gmail.com", "doc", "GET") == {"revisionId": "r"}
    resolver.assert_called_once_with("docs", "specific@gmail.com")
    provider.assert_called_once_with("GET", "https://docs.googleapis.com/v1/documents/doc?includeTabsContent=true", "secret", None)


def test_cli_thin_client_and_parser(monkeypatch, tmp_path, capsys):
    provider = Mock(return_value={"revisionId": "next"})
    monkeypatch.setattr(api, "call", provider)
    file = tmp_path / "requests.json"
    file.write_text(json.dumps({"requests": [{"insertText": {"text": "hello"}}]}))
    args = build_parser().parse_args(["gdocs", "batch-update", "doc", "--account", "a", "--file", str(file), "--revision", "r"])
    assert args.revision == "r"
    cli.gdocs_batch_update("api", "doc", str(file), "a", "r")
    assert provider.call_args.kwargs["body"] == {"account": "a", "requests": [{"insertText": {"text": "hello"}}], "required_revision_id": "r"}
    cli.gdocs_structure("api", "doc", "a")
    assert provider.call_args.args[2] == "/api/agent/gdocs/documents/doc?account=a"


def test_provider_revision_error_preserved(route, monkeypatch):
    def fail(*args):
        raise gdocs.provider.DiscoveryError("HTTP 400: required revision is not latest")
    monkeypatch.setattr(gdocs, "_call", fail)
    result = route("POST", {"account": "a", "requests": [{"insertText": {"text": "hi"}}], "required_revision_id": "stale"})
    assert result["statusCode"] == 400
    assert "required revision" in json.loads(result["body"])["error"]
