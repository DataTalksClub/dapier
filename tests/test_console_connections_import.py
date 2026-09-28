"""The console's connections-import surface vs the operator import endpoint.

``dapier connections import`` and ``POST /api/{admin,agent}/connections/import``
all dispatch to ``importing.import_core``; the Connections view's Import
dialog is the console's path to the same outcome (the gap this closed:
import existed on the CLI and the API but the console could not reach it).
These pins keep the dialog honest against drift: a provider the server can
import must appear in the dialog's Provider select, the dialog must keep
calling the admin route, and the CLI subcommand must stay registered.
"""
import re
from pathlib import Path

from src.dapier.connections.providers import oauth_providers
from src.dapier.connections.records import TOKEN_PROVIDERS

WEB = Path(__file__).resolve().parents[1] / "src" / "web"


def _import_select_providers():
    html = (WEB / "index.html").read_text()
    match = re.search(r'id="import-connection-form"(.*?)</form>', html, re.S)
    assert match, "the import-connection dialog form is missing"
    select = re.search(r'name="provider">(.*?)</select>', match.group(1), re.S)
    assert select, "the import dialog's Provider select is missing"
    return set(re.findall(r'<option value="([^"]*)"', select.group(1))) - {""}


def test_the_import_dialog_offers_every_importable_provider():
    importable = set(oauth_providers.PROVIDERS) | set(TOKEN_PROVIDERS)
    missing = importable - _import_select_providers()
    assert not missing, f"importable providers missing from the console dialog: {sorted(missing)}"
    unknown = _import_select_providers() - importable
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


def test_the_cli_still_offers_import_for_the_same_outcome():
    cli = (Path(__file__).resolve().parents[1] / "dapier_cli" / "main.py").read_text()
    assert 'add_parser("import"' in cli, "the dapier connections import subcommand went missing"
