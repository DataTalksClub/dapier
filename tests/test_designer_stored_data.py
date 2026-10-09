"""The designer's Stored data panel replaces the console's Data page.

A workflow's key-value state (the store behind its storage_* steps) only
means something next to that workflow, so the console shows it inside the
designer — offered only when the workflow uses storage steps — and drives
the same /api/admin/storage/<workflow> endpoints the CLI's
``dapier storage find|set|delete`` reach through /api/agent/storage. These
pin the wiring in the source (designer/src) and in the shipped bundle
(src/web/designer.js, rebuilt by ``make designer-console``).
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PANEL_TSX = ROOT / "designer" / "src" / "StoredData.tsx"
APP_TSX = ROOT / "designer" / "src" / "App.tsx"
STYLES = ROOT / "designer" / "src" / "styles.css"
BUNDLE_JS = ROOT / "src" / "web" / "designer.js"
BUNDLE_CSS = ROOT / "src" / "web" / "designer.css"


def test_panel_lists_sets_and_deletes_through_the_storage_api():
    panel = PANEL_TSX.read_text()
    assert "`/storage/${encodeURIComponent(workflowId)}`" in panel
    assert "?prefix=${encodeURIComponent(nextPrefix)}" in panel
    assert 'method: "POST"' in panel and "ttl_seconds" in panel
    assert "?key=${encodeURIComponent(itemKey)}`, { method: \"DELETE\" }" in panel
    # Expiry and the last write show on every listed key.
    assert "item.expires" in panel and "item.updated_at" in panel


def test_app_offers_the_panel_only_for_saved_workflows_with_storage_steps():
    app = APP_TSX.read_text()
    assert 'import { StoredDataPanel, workflowUsesStorage } from "./StoredData";' in app
    assert re.search(
        r'config\.mode === "console" && !!sourceName && workflowUsesStorage\(', app)
    # The panel talks to /api/admin (not the designer's own prefix) and is
    # keyed by the saved workflow id.
    assert 'api<T>(config, path, init, "/api/admin")' in app
    assert "<StoredDataPanel workflowId={savedId}" in app
    assert "{storedDataAvailable && (" in app


def test_storage_detection_covers_canvas_and_flow_steps():
    panel = PANEL_TSX.read_text()
    assert "/^storage_(get|set|find|delete)$/" in panel
    assert '/"type":"storage_(get|set|find|delete)"/' in panel


def test_bundle_ships_the_panel():
    bundle = BUNDLE_JS.read_text()
    assert "Stored data" in bundle
    assert "stored-panel" in bundle
    assert "Store a value" in bundle
    css = BUNDLE_CSS.read_text()
    assert ".stored-panel" in css


def test_panel_is_a_full_screen_sheet_on_phones():
    styles = STYLES.read_text()
    phone = styles[styles.rindex("@media (max-width: 720px)"):]
    assert ".stored-panel" in phone and "inset: 0" in phone
    assert "var(--dk-size-touch)" in phone
