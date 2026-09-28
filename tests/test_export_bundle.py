"""All-workflows export bundle (designer_store.api_export_all): one portable
zip of every workflow's canonical YAML plus a manifest.json — no connections,
tokens, or secrets, workflow YAML only.

Covers the bundle contents against api_get (byte-identical canonical YAML and
the re-save round-trip), the manifest, the download headers on both operator
routes, agent-side operator gating, the CLI thin client, and the empty case.
"""
import base64
import io
import json
import time
import zipfile
from datetime import datetime

import pytest

from dapier_cli import commands, main
from src.dapier.api import admin
from src.dapier.api import agent as agent_api
from src.dapier.api import designer_store
from src.dapier.auth import session
from src.dapier.triggers import published_workflows


def _workflow(workflow_id, extra=""):
    return f"""\
id: {workflow_id}
enabled: true
description: test workflow
trigger:
  connector: email
  event: message.received
actions:
  - id: a1
    type: webhook
    url: https://example.test/hook
{extra}"""


@pytest.fixture
def bundle_dir(tmp_path, monkeypatch):
    """Two managed workflow definitions."""
    monkeypatch.setenv(published_workflows.TABLE_ENV, "published")
    items = [{"workflow": designer_store.parse_workflow(text), "file": f"{name}.yaml"}
             for name, text in (
                 ("invoice-alert", _workflow("invoice-alert")),
                 ("standup-digest", _workflow(
                     "standup-digest", "folder: Ops\ntags: [weekly, digest]\n")),
             )]
    monkeypatch.setattr(published_workflows, "load_items", lambda table_ref=None: items)
    monkeypatch.setattr(published_workflows, "get_item", lambda workflow_id, table_ref=None:
                        next((item for item in items if item["workflow"]["id"] == workflow_id), None))
    return tmp_path


def _bundle(payload):
    return zipfile.ZipFile(io.BytesIO(base64.b64decode(payload["b64"])))


# --- bundle contents ---------------------------------------------------------------


def test_bundle_yaml_is_byte_identical_to_api_get(bundle_dir):
    for source in ("invoice-alert.yaml", "standup-digest.yaml"):
        _, expected = designer_store.api_get(source)

        status, payload = designer_store.api_export_all()

        assert status == 200
        with _bundle(payload) as bundle:
            assert bundle.read(f"workflows/{source}").decode() == expected["yaml"]


def test_bundle_entries_reimport_byte_identical(bundle_dir):
    """The single-export round-trip holds per entry: parsing an entry and
    re-rendering it (what a save commits) reproduces the same bytes."""
    _, payload = designer_store.api_export_all()

    with _bundle(payload) as bundle:
        for name in bundle.namelist():
            if not name.endswith(".yaml"):
                continue
            entry = bundle.read(name).decode()
            assert designer_store.workflow_yaml_text(
                designer_store.parse_workflow(entry)) == entry


def test_manifest_describes_the_bundle(bundle_dir):
    _, payload = designer_store.api_export_all(now=1726867200)

    with _bundle(payload) as bundle:
        manifest = json.loads(bundle.read("manifest.json"))
    datetime.fromisoformat(manifest["exported_at"])
    assert manifest["count"] == payload["count"] == 2
    assert manifest["skipped"] == []
    assert manifest["workflows"] == [
        {"id": "invoice-alert", "source": "invoice-alert.yaml", "folder": "",
         "tags": []},
        {"id": "standup-digest", "source": "standup-digest.yaml", "folder": "Ops",
         "tags": ["weekly", "digest"]},
    ]


def test_empty_bundle_is_valid(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKFLOWS_DIR", str(tmp_path))
    monkeypatch.delenv(published_workflows.TABLE_ENV, raising=False)

    status, payload = designer_store.api_export_all()

    assert status == 200
    assert payload["count"] == 0
    assert payload["skipped"] == []
    with _bundle(payload) as bundle:
        assert bundle.namelist() == ["manifest.json"]
        manifest = json.loads(bundle.read("manifest.json"))
    assert manifest["count"] == 0
    assert manifest["workflows"] == []


# --- routes ------------------------------------------------------------------------


def _admin_session(monkeypatch):
    monkeypatch.setattr(session, "_credentials",
                        lambda: {"username": "admin", "password": "pw"})
    monkeypatch.setenv("OPERATOR_EMAILS", "op@datatalks.club")
    cookie = session._sign({"sub": "op@datatalks.club", "subject": "op-sub",
                            "exp": int(time.time()) + 600})
    return [f"dapier_session={cookie}"]


def _admin_request(path, cookies):
    return {
        "requestContext": {"http": {"method": "GET", "path": path}},
        "headers": {"host": "dapier.example.test", "origin": "https://dapier.example.test"},
        "cookies": cookies,
        "queryStringParameters": None,
        "body": None,
    }


def test_admin_route_serves_the_bundle_with_download_headers(bundle_dir, monkeypatch):
    cookies = _admin_session(monkeypatch)

    response = admin.route(
        _admin_request("/api/admin/designer/workflows/export-all", cookies),
        "GET", "/api/admin/designer/workflows/export-all")

    assert response["statusCode"] == 200
    assert response["headers"]["content-type"] == "application/json"
    body = json.loads(response["body"])
    assert body["filename"].startswith("dapier-workflows-")
    with _bundle(body) as bundle:
        assert bundle.namelist() == ["manifest.json",
                                     "workflows/invoice-alert.yaml",
                                     "workflows/standup-digest.yaml"]


def _configure_agent(monkeypatch):
    monkeypatch.setenv("OPERATOR_EMAILS", "op@datatalks.club")
    monkeypatch.setenv("AUTH_CLI_CLIENT_ID", "cli-client")
    monkeypatch.delenv("DAPIER_RATE_LIMIT", raising=False)
    monkeypatch.setattr(agent_api, "verify_id_token",
                        lambda token, audience=None: {"sub": "op-1",
                                                      "email": "op@datatalks.club"})


def _bearer_event():
    return {
        "headers": {"host": "dapier.example.test", "authorization": "Bearer dtc-token"},
        "cookies": [],
        "queryStringParameters": None,
    }


def test_agent_route_serves_the_same_bundle_with_download_headers(bundle_dir, monkeypatch):
    _configure_agent(monkeypatch)

    response = agent_api.route(_bearer_event(), "GET",
                               "/api/agent/designer/workflows/export-all")

    assert response["statusCode"] == 200
    assert response["headers"]["content-type"] == "application/json"
    # Like the audit CSV export: a fresh download, never a cached one.
    assert response["headers"]["cache-control"] == "no-store"
    body = json.loads(response["body"])
    assert body["filename"].startswith("dapier-workflows-")
    with _bundle(body) as bundle:
        assert bundle.namelist() == ["manifest.json",
                                     "workflows/invoice-alert.yaml",
                                     "workflows/standup-digest.yaml"]


def test_agent_route_requires_operator(bundle_dir, monkeypatch):
    _configure_agent(monkeypatch)
    monkeypatch.setenv("OPERATOR_EMAILS", "someone-else@datatalks.club")

    response = agent_api.route(_bearer_event(), "GET",
                               "/api/agent/designer/workflows/export-all")

    assert response["statusCode"] == 403


# --- CLI ---------------------------------------------------------------------------


@pytest.fixture
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("DAPIER_API_URL", raising=False)
    return tmp_path


def test_cli_export_all_writes_the_file(isolated_home, monkeypatch, tmp_path, capsys):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path))
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as bundle:
            bundle.writestr("manifest.json", "{}\n")
            bundle.writestr("workflows/one.yaml", "id: one\n")
        return {"filename": "dapier-workflows-20260928.zip", "count": 1,
                "skipped": [], "b64": base64.b64encode(buffer.getvalue()).decode()}

    monkeypatch.setattr(commands.api, "call", fake_call)
    target = tmp_path / "bundle.zip"

    rc = main.main(["workflows", "export-all", "--out", str(target)])

    assert rc == 0
    assert calls == [("GET", "/api/agent/designer/workflows/export-all")]
    with zipfile.ZipFile(target) as bundle:
        assert bundle.namelist() == ["manifest.json", "workflows/one.yaml"]
    assert "Exported 1 workflow(s) to" in capsys.readouterr().out
