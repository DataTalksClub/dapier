"""Export-all: every workflow's canonical YAML as one zip — the domain bundle
(designer_store.api_export_all), both operator routes over it, and the CLI
thin client.

Saves already commit YAML to git and backup.py snapshots the tables, so the
bundle is convenience: one portable zip instead of per-workflow fetches. The
parity wiring mirrors the audit CSV export — admin + agent routes share the
domain function, the CLI only decodes and writes.
"""
import base64
import io
import json
import time
import zipfile

import pytest

from dapier_cli import commands, main
from src.dapier.api import admin
from src.dapier.api import agent as agent_api
from src.dapier.api import designer_store
from src.dapier.auth import session
from src.dapier.triggers import published_workflows


def _workflow(workflow_id, description="test workflow"):
    return f"""\
id: {workflow_id}
enabled: true
description: {description}
trigger:
  connector: email
  event: message.received
actions:
  - id: a1
    type: webhook
    url: https://example.test/hook
"""


@pytest.fixture
def bundle_dir(tmp_path, monkeypatch):
    """A WORKFLOWS_DIR with two valid workflow files; nothing published."""
    monkeypatch.setenv("WORKFLOWS_DIR", str(tmp_path))
    monkeypatch.delenv(published_workflows.TABLE_ENV, raising=False)
    (tmp_path / "invoice-alert.yaml").write_text(_workflow("invoice-alert"))
    (tmp_path / "standup-digest.yaml").write_text(_workflow("standup-digest"))
    return tmp_path


# --- domain -----------------------------------------------------------------------


def test_export_all_zips_one_canonical_yaml_per_workflow(bundle_dir):
    status, payload = designer_store.api_export_all(now=1726867200)

    assert status == 200
    assert payload["filename"] == "dapier-workflows-20240920.zip"
    assert payload["count"] == 2
    assert payload["skipped"] == []
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(payload["b64"]))) as bundle:
        assert bundle.namelist() == ["manifest.json",
                                     "workflows/invoice-alert.yaml",
                                     "workflows/standup-digest.yaml"]
        # The same canonical render api_get serves, so a re-save round-trips.
        workflow = designer_store.bundled_yaml("standup-digest.yaml")
        assert bundle.read("workflows/standup-digest.yaml").decode() == \
            designer_store.workflow_yaml_text(workflow)


def test_export_all_prefers_the_published_state(bundle_dir, monkeypatch):
    """Same merge rule as api_list: the published state wins by id, so a save
    the deploy pipeline has not picked up yet still exports live."""
    monkeypatch.setenv(published_workflows.TABLE_ENV, "published")
    items = [{
        "workflow": designer_store.parse_workflow(_workflow("invoice-alert", "live now")),
        "file": "invoice-alert.yaml",
    }]
    monkeypatch.setattr(published_workflows, "load_items", lambda table_ref=None: items)

    status, payload = designer_store.api_export_all()

    assert status == 200
    assert payload["count"] == 2
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(payload["b64"]))) as bundle:
        entry = bundle.read("workflows/invoice-alert.yaml").decode()
    assert "live now" in entry


def test_export_all_skips_workflows_without_a_source_file(bundle_dir, monkeypatch):
    monkeypatch.setenv(published_workflows.TABLE_ENV, "published")
    items = [
        {"workflow": {"id": "no-file"}, "file": None},
        {"workflow": {"id": "odd"}, "file": "not a name.yaml"},
        {"workflow": designer_store.parse_workflow(_workflow("good-one")),
         "file": "good-one.yaml"},
    ]
    monkeypatch.setattr(published_workflows, "load_items", lambda table_ref=None: items)

    status, payload = designer_store.api_export_all()

    assert status == 200
    # The two bundled workflows plus the one publishable item; the two
    # source-less items are skipped but do not fail the bundle.
    assert payload["count"] == 3
    assert payload["skipped"] == ["no-file", "odd"]
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(payload["b64"]))) as bundle:
        assert bundle.namelist() == ["manifest.json",
                                     "workflows/good-one.yaml",
                                     "workflows/invoice-alert.yaml",
                                     "workflows/standup-digest.yaml"]


def test_export_all_refuses_more_than_the_cap(monkeypatch):
    monkeypatch.delenv(published_workflows.TABLE_ENV, raising=False)
    monkeypatch.setattr(
        designer_store, "_bundled_workflows",
        lambda: [({"id": f"w{i:03d}"}, f"w{i:03d}.yaml")
                 for i in range(designer_store.MAX_EXPORT_WORKFLOWS + 1)])

    status, payload = designer_store.api_export_all()

    assert status == 400
    assert "cap" in payload["error"]


# --- routes -----------------------------------------------------------------------


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


def test_admin_export_all_route_serves_the_zip(bundle_dir, monkeypatch):
    cookies = _admin_session(monkeypatch)

    response = admin.route(
        _admin_request("/api/admin/designer/workflows/export-all", cookies),
        "GET", "/api/admin/designer/workflows/export-all")

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["count"] == 2
    assert body["skipped"] == []
    assert body["filename"].startswith("dapier-workflows-")
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(body["b64"]))) as bundle:
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


def test_agent_export_all_route_serves_the_same_zip(bundle_dir, monkeypatch):
    _configure_agent(monkeypatch)

    response = agent_api.route(_bearer_event(), "GET",
                               "/api/agent/designer/workflows/export-all")

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["count"] == 2
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(body["b64"]))) as bundle:
        assert bundle.namelist() == ["manifest.json",
                                     "workflows/invoice-alert.yaml",
                                     "workflows/standup-digest.yaml"]


def test_agent_export_all_route_requires_operator(bundle_dir, monkeypatch):
    _configure_agent(monkeypatch)
    monkeypatch.setenv("OPERATOR_EMAILS", "someone-else@datatalks.club")

    response = agent_api.route(_bearer_event(), "GET",
                               "/api/agent/designer/workflows/export-all")

    assert response["statusCode"] == 403


def test_agent_export_all_route_refuses_over_the_cap(monkeypatch):
    monkeypatch.delenv(published_workflows.TABLE_ENV, raising=False)
    monkeypatch.setattr(
        designer_store, "_bundled_workflows",
        lambda: [({"id": f"w{i:03d}"}, f"w{i:03d}.yaml")
                 for i in range(designer_store.MAX_EXPORT_WORKFLOWS + 1)])
    _configure_agent(monkeypatch)

    response = agent_api.route(_bearer_event(), "GET",
                               "/api/agent/designer/workflows/export-all")

    assert response["statusCode"] == 400
    assert "cap" in json.loads(response["body"])["error"]


# --- CLI --------------------------------------------------------------------------


@pytest.fixture
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("DAPIER_API_URL", raising=False)
    return tmp_path


def _zip_bytes(names=("invoice-alert.yaml", "standup-digest.yaml")):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        for name in names:
            bundle.writestr(name, f"id: {name.removesuffix('.yaml')}\n")
    return buffer.getvalue()


def test_cli_export_all_writes_the_zip(isolated_home, monkeypatch, tmp_path, capsys):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path))
        return {"filename": "dapier-workflows-20260928.zip", "count": 2,
                "skipped": ["odd"],
                "b64": base64.b64encode(_zip_bytes()).decode()}

    monkeypatch.setattr(commands.api, "call", fake_call)
    target = tmp_path / "bundle.zip"

    rc = main.main(["workflows", "export-all", "--out", str(target)])

    assert rc == 0
    assert calls == [("GET", "/api/agent/designer/workflows/export-all")]
    assert target.read_bytes() == _zip_bytes()
    out = capsys.readouterr().out
    assert "Exported 2 workflow(s) to" in out
    assert "skipped: odd" in out


def test_cli_export_all_defaults_to_the_server_suggested_name(isolated_home, monkeypatch,
                                                              tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(commands.api, "call",
                        lambda *args, **kwargs: {
                            "filename": "dapier-workflows-20260928.zip",
                            "count": 1, "skipped": [],
                            "b64": base64.b64encode(_zip_bytes(("one.yaml",))).decode()})

    rc = main.main(["workflows", "export-all"])

    assert rc == 0
    with zipfile.ZipFile(tmp_path / "dapier-workflows-20260928.zip") as bundle:
        assert bundle.namelist() == ["one.yaml"]
