"""The designer bundle export (designer_store.api_export): one zip of every
workflow's canonical YAML plus a manifest.json, served by both operator
routes — /api/admin/designer/export (the console's Export-all button) and
/api/agent/designer/export (`workflows export --all`).

Follows the established export test conventions (test_export_all.py,
test_designer.py): a WORKFLOWS_DIR with a couple of files for the domain
bundle, the signed session/bearer events for the two dispatchers. The
assertions that matter: each zip entry's bytes equal what api_get serves for
that file, the manifest rows carry file/enabled/tags/folder/version, and the
tag/folder filters narrow the bundle exactly like the designer list.
"""
import base64
import io
import json
import time
import zipfile
from datetime import datetime, timezone

import pytest

from src.dapier.api import admin, designer_store
from src.dapier.api import agent as agent_api
from src.dapier.auth import session
from src.dapier.triggers import published_workflows


def _workflow_yaml(workflow_id, *, enabled=True, tags=None, folder=None):
    lines = [f"id: {workflow_id}"]
    if not enabled:
        lines.append("enabled: false")
    lines.append("description: test workflow")
    if tags is not None:
        lines.append("tags:")
        lines.extend(f"  - {tag}" for tag in tags)
    if folder:
        lines.append(f"folder: {folder}")
    lines.extend([
        "trigger:",
        "  connector: email",
        "  event: message.received",
        "actions:",
        "  - id: a1",
        "    type: webhook",
        "    url: https://example.test/hook",
    ])
    return "\n".join(lines) + "\n"


@pytest.fixture
def bundle_dir(tmp_path, monkeypatch):
    """A WORKFLOWS_DIR with two workflow files (one tagged/foldered and off);
    nothing published and no git sync."""
    monkeypatch.setenv("WORKFLOWS_DIR", str(tmp_path))
    monkeypatch.delenv(published_workflows.TABLE_ENV, raising=False)
    monkeypatch.delenv(designer_store.TOKEN_SECRET_ENV, raising=False)
    (tmp_path / "invoice-alert.yaml").write_text(_workflow_yaml(
        "invoice-alert", enabled=False, tags=["billing", "Finance"], folder="Money"))
    (tmp_path / "standup-digest.yaml").write_text(_workflow_yaml(
        "standup-digest", tags=["internal"]))
    return tmp_path


# --- domain -----------------------------------------------------------------------


def test_export_zips_one_canonical_yaml_per_workflow_matching_api_get(bundle_dir):
    status, payload = designer_store.api_export(now=1726867200)

    assert status == 200
    assert payload["filename"] == "dapier-workflows-20240920.zip"
    assert payload["count"] == 2
    assert payload["skipped"] == []
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(payload["b64"]))) as bundle:
        assert bundle.namelist() == ["manifest.json",
                                     "workflows/invoice-alert.yaml",
                                     "workflows/standup-digest.yaml"]
        # The same canonical render api_get serves, so each entry re-saves
        # through `workflows save` byte-identical.
        for source in ("invoice-alert.yaml", "standup-digest.yaml"):
            assert bundle.read(f"workflows/{source}").decode() == \
                designer_store.api_get(source)[1]["yaml"]


def test_export_manifest_lists_file_enabled_tags_folder_version(bundle_dir):
    status, payload = designer_store.api_export(now=1726867200)

    assert status == 200
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(payload["b64"]))) as bundle:
        manifest = json.loads(bundle.read("manifest.json"))
    assert manifest["exported_at"] == datetime.fromtimestamp(
        1726867200, timezone.utc).isoformat()
    assert manifest["count"] == 2
    assert manifest["skipped"] == []
    assert manifest["workflows"] == [
        {"file": "invoice-alert.yaml", "enabled": False,
         "tags": ["billing", "Finance"], "folder": "Money", "version": 0},
        {"file": "standup-digest.yaml", "enabled": True,
         "tags": ["internal"], "folder": "", "version": 0},
    ]


def test_export_prefers_the_published_state_and_records_its_version(bundle_dir,
                                                                    monkeypatch):
    """Same merge rule as api_list: the published state wins by id, and the
    manifest's version is that item's latest revision."""
    monkeypatch.setenv(published_workflows.TABLE_ENV, "published")
    items = [{
        "workflow": designer_store.parse_workflow(
            _workflow_yaml("invoice-alert", tags=["billing"], folder="Money")),
        "file": "invoice-alert.yaml",
        "revision": 7,
    }]
    monkeypatch.setattr(published_workflows, "load_items", lambda table_ref=None: items)

    status, payload = designer_store.api_export()

    assert status == 200
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(payload["b64"]))) as bundle:
        entry = bundle.read("workflows/invoice-alert.yaml").decode()
        manifest = json.loads(bundle.read("manifest.json"))
    assert "test workflow" in entry  # the published description, not the bundle's
    assert manifest["count"] == 2
    rows = {row["file"]: row for row in manifest["workflows"]}
    assert rows["invoice-alert.yaml"]["version"] == 7
    assert rows["standup-digest.yaml"]["version"] == 0


def test_export_tag_filter_narrows_like_the_list(bundle_dir):
    status, payload = designer_store.api_export(tag="FINANCE")

    assert status == 200
    assert payload["count"] == 1
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(payload["b64"]))) as bundle:
        assert bundle.namelist() == ["manifest.json", "workflows/invoice-alert.yaml"]

    nothing = designer_store.api_export(tag="no-such-tag")
    assert nothing[0] == 200
    assert nothing[1]["count"] == 0
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(nothing[1]["b64"]))) as bundle:
        assert bundle.namelist() == ["manifest.json"]


def test_export_folder_filter_narrows_like_the_list(bundle_dir):
    status, payload = designer_store.api_export(folder="money")

    assert status == 200
    assert payload["count"] == 1
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(payload["b64"]))) as bundle:
        assert bundle.namelist() == ["manifest.json", "workflows/invoice-alert.yaml"]

    nothing = designer_store.api_export(folder="Archive")
    assert nothing[0] == 200
    assert nothing[1]["count"] == 0


def test_export_skips_workflows_without_a_source_file(bundle_dir, monkeypatch):
    monkeypatch.setenv(published_workflows.TABLE_ENV, "published")
    items = [
        {"workflow": {"id": "no-file"}, "file": None},
        {"workflow": {"id": "odd"}, "file": "not a name.yaml"},
        {"workflow": designer_store.parse_workflow(_workflow_yaml("good-one")),
         "file": "good-one.yaml"},
    ]
    monkeypatch.setattr(published_workflows, "load_items", lambda table_ref=None: items)

    status, payload = designer_store.api_export()

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


def test_export_refuses_more_than_the_cap(monkeypatch):
    monkeypatch.delenv(published_workflows.TABLE_ENV, raising=False)
    monkeypatch.setattr(
        designer_store, "_bundled_workflows",
        lambda: [({"id": f"w{i:03d}"}, f"w{i:03d}.yaml")
                 for i in range(designer_store.MAX_EXPORT_WORKFLOWS + 1)])

    status, payload = designer_store.api_export()

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


def _admin_request(path, cookies, query=None):
    return {
        "requestContext": {"http": {"method": "GET", "path": path}},
        "headers": {"host": "dapier.example.test", "origin": "https://dapier.example.test"},
        "cookies": cookies,
        "queryStringParameters": query,
        "body": None,
    }


def test_admin_export_route_serves_the_zip_and_audits(bundle_dir, monkeypatch):
    cookies = _admin_session(monkeypatch)
    audits = []
    monkeypatch.setattr(session, "_audit_event",
                        lambda *args, **kwargs: audits.append((args, kwargs)) or None)

    response = admin.route(
        _admin_request("/api/admin/designer/export", cookies),
        "GET", "/api/admin/designer/export")

    assert response["statusCode"] == 200
    disposition = response["headers"]["content-disposition"]
    assert disposition.startswith('attachment; filename="dapier-workflows-')
    assert disposition.endswith('.zip"')
    body = json.loads(response["body"])
    assert body["count"] == 2
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(body["b64"]))) as bundle:
        assert "workflows/standup-digest.yaml" in bundle.namelist()
    assert audits[0][0][1] == "workflow.export"


def test_admin_export_route_forwards_the_tag_filter(bundle_dir, monkeypatch):
    cookies = _admin_session(monkeypatch)

    response = admin.route(
        _admin_request("/api/admin/designer/export", cookies, query={"tag": "internal"}),
        "GET", "/api/admin/designer/export")

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["count"] == 1
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(body["b64"]))) as bundle:
        assert bundle.namelist() == ["manifest.json", "workflows/standup-digest.yaml"]


def test_admin_export_route_requires_session(bundle_dir, monkeypatch):
    monkeypatch.setattr(session, "authenticated", lambda event: False)

    response = admin.route(
        _admin_request("/api/admin/designer/export", []),
        "GET", "/api/admin/designer/export")

    assert response["statusCode"] == 401


def _configure_agent(monkeypatch, audits=None):
    monkeypatch.setenv("OPERATOR_EMAILS", "op@datatalks.club")
    monkeypatch.setenv("AUTH_CLI_CLIENT_ID", "cli-client")
    monkeypatch.setattr(agent_api, "verify_id_token",
                        lambda token, audience=None: {"sub": "op-1",
                                                      "email": "op@datatalks.club"})
    if audits is not None:
        monkeypatch.setattr(agent_api, "audit", type("Audit", (), {
            "emit": staticmethod(lambda *args, **kwargs: audits.append((args, kwargs))),
        }))


def _bearer_event(query=None):
    return {
        "headers": {"host": "dapier.example.test", "authorization": "Bearer dtc-token"},
        "cookies": [],
        "queryStringParameters": query,
    }


def test_agent_export_route_serves_the_same_bundle(bundle_dir, monkeypatch):
    audits = []
    _configure_agent(monkeypatch, audits)

    response = agent_api.route(_bearer_event(), "GET", "/api/agent/designer/export")

    assert response["statusCode"] == 200
    assert "attachment; filename=" in response["headers"]["content-disposition"]
    body = json.loads(response["body"])
    assert body["count"] == 2
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(body["b64"]))) as bundle:
        assert bundle.read("workflows/invoice-alert.yaml").decode() == \
            designer_store.api_get("invoice-alert.yaml")[1]["yaml"]
    assert audits[-1][0][:2] == ("workflows", "workflow.export")


def test_agent_export_route_forwards_the_folder_filter(bundle_dir, monkeypatch):
    _configure_agent(monkeypatch)

    response = agent_api.route(_bearer_event(query={"folder": "Money"}),
                               "GET", "/api/agent/designer/export")

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["count"] == 1
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(body["b64"]))) as bundle:
        assert bundle.namelist() == ["manifest.json", "workflows/invoice-alert.yaml"]


def test_agent_export_route_requires_operator(bundle_dir, monkeypatch):
    _configure_agent(monkeypatch)
    monkeypatch.setenv("OPERATOR_EMAILS", "someone-else@datatalks.club")

    response = agent_api.route(_bearer_event(), "GET", "/api/agent/designer/export")

    assert response["statusCode"] == 403
