"""Connection import is a CLI-only operator path.

``dapier connections import`` posts to ``/api/agent/connections/import``,
which dispatches to ``importing.import_core``. The console reaches the same
outcome for token providers through its New connection dialog (the admin
save path drives ``importing.save_token_connection``), and the one-time
OAuth refresh-token migration it once offered is done — so the console's
Import dialog and its ``/api/admin/connections/import`` route are gone.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "web"


def test_the_console_no_longer_offers_import():
    html = (WEB / "index.html").read_text()
    js = (WEB / "js" / "views" / "connections.js").read_text()
    assert 'id="import-connection' not in html
    assert "/api/admin/connections/import" not in js


def test_the_admin_import_route_is_gone():
    dispatch = (ROOT / "src" / "dapier" / "api" / "admin" / "dispatch.py").read_text()
    assert "/api/admin/connections/import" not in dispatch


def test_the_cli_still_offers_import():
    cli = (ROOT / "dapier_cli" / "cli" / "connections.py").read_text()
    assert 'add_parser("import"' in cli, "the dapier connections import subcommand went missing"
