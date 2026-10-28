"""The console's connections-import surface vs the operator import endpoint.

``dapier connections import`` and ``POST /api/{admin,agent}/connections/import``
all dispatch to ``importing.import_core``; the Connections view's Import
dialog is the console's path to the same outcome (the UI/CLI/API parity
rule in AGENTS.md). These pins keep the dialog honest against drift: a
provider the server can import must appear in the dialog's Provider select,
the dialog must keep calling the admin route, and the CLI subcommand must
stay registered.
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "web"


def _import_select_providers():
    html = (WEB / "index.html").read_text()
    match = re.search(r'id="import-connection-form"(.*?)</form>', html, re.S)
    assert match, "the import-connection dialog form is missing"
    select = re.search(r'name="provider">(.*?)</select>', match.group(1), re.S)
    assert select, "the import dialog's Provider select is missing"
    return set(re.findall(r'<option value="([^"]*)"', select.group(1))) - {""}


def _importable_providers():
    """What ``import_core`` accepts, with the plugins loaded: the OAuth
    providers (authorized-user refresh transfer) plus every registered
    token provider (pasted credential) and connection saver (``llm``)."""
    from src.dapier import plugins
    from src.dapier.connections import importing, records
    from src.dapier.connections.providers import oauth_providers

    plugins.load_all()
    return set(oauth_providers.PROVIDERS) | set(records.TOKEN_PROVIDERS)


def test_the_import_dialog_offers_every_importable_provider():
    importable = _importable_providers()
    offered = _import_select_providers()
    missing = importable - offered
    assert not missing, f"importable providers missing from the console dialog: {sorted(missing)}"
    unknown = offered - importable
    assert not unknown, f"the dialog offers providers the server cannot import: {sorted(unknown)}"


def test_the_import_dialog_posts_to_the_admin_import_route():
    html = (WEB / "index.html").read_text()
    assert 'id="import-connection"' in html, "the Import button is missing"
    js = (WEB / "js" / "views" / "connections.js").read_text()
    assert "api('/api/admin/connections/import'" in js, (
        "the import dialog must POST to /api/admin/connections/import — "
        "the route the console talks to, not a console-side reimplementation")
    assert "authorized_user" in js and "refresh_token" in js, (
        "the OAuth import path must send the authorized-user credential "
        "the server's import_core expects")
    assert "llm_config" in js, "the LLM import path must send the connection config"


def test_the_admin_import_route_wraps_import_core():
    dispatch = (ROOT / "src" / "dapier" / "api" / "admin" / "dispatch.py").read_text()
    assert '"/api/admin/connections/import"' in dispatch, (
        "the admin import route went missing — the console dialog has no endpoint")
    admin = (ROOT / "src" / "dapier" / "api" / "admin" / "connections.py").read_text()
    assert "importing.import_core" in admin, (
        "the admin import handler must wrap import_core, not reimplement it")


def test_the_cli_still_offers_import_for_the_same_outcome():
    cli = (ROOT / "dapier_cli" / "cli" / "connections.py").read_text()
    assert 'add_parser("import"' in cli, "the dapier connections import subcommand went missing"


# --- the admin import route (the dialog's endpoint) ------------------------------


def _admin(monkeypatch, table):
    from src.dapier.api.admin import routes as admin_routes

    monkeypatch.setenv("CONNECTIONS_TABLE", "connections")
    monkeypatch.setattr(admin_routes.boto3, "resource", lambda _: type(
        "Resource", (), {"Table": lambda self, name: table})())
    monkeypatch.setattr(admin_routes.session, "_session_subject", lambda _: "operator")
    monkeypatch.setattr(admin_routes.session, "_audit_event", lambda *args, **kwargs: None)
    return admin_routes


def test_the_admin_import_route_imports_a_zoom_webhook(monkeypatch):
    from src.dapier.connections import importing
    from tests.test_zoom import setup as setup_webhook

    table, secrets = setup_webhook(monkeypatch)
    admin_routes = _admin(monkeypatch, table)
    monkeypatch.setattr(importing.audit, "emit", lambda *args, **kwargs: None)
    response = admin_routes.import_connection(
        {"body": json.dumps({"connection_id": "zoom-import", "provider": "zoom",
                             "token": "imported-secret-123456"})}, "operator")
    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["connection_id"] == "zoom-import"
    assert table.items["zoom-import"]["status"] == "ready"
    assert secrets["oauth#zoom-import"] == {"webhook_secret": "imported-secret-123456"}
    assert "imported-secret" not in response["body"]


def test_the_admin_import_route_rejects_an_invalid_body(monkeypatch):
    admin_routes = _admin(monkeypatch, {})
    response = admin_routes.import_connection({"body": "not json"}, "operator")
    assert response["statusCode"] == 400
    assert json.loads(response["body"]) == {"error": "Invalid request"}


def test_the_admin_import_route_maps_import_failures(monkeypatch):
    from src.dapier.connections import importing
    from tests.test_zoom import setup as setup_webhook

    table, _ = setup_webhook(monkeypatch)
    admin_routes = _admin(monkeypatch, table)
    monkeypatch.setattr(importing.audit, "emit", lambda *args, **kwargs: None)
    # An OAuth provider without an authorized-user object fails closed.
    response = admin_routes.import_connection(
        {"body": json.dumps({"connection_id": "google-bad", "provider": "google"})}, "operator")
    assert response["statusCode"] == 400
    assert "authorized-user" in json.loads(response["body"])["error"]
