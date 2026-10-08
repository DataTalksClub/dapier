"""Console register groups rows one panel per service; identity on the row."""

from pathlib import Path
import json

from py_mini_racer import MiniRacer

from src.dapier.connections.records import public_view
from src.dapier.connections.services import CATALOG, services_for

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "src/web/js/views/connections.js").read_text()
HTML = (ROOT / "src/web/index.html").read_text()


def _ids(connection):
    return [entry["id"] for entry in services_for(connection)]


def test_google_scopes_split_into_products():
    scopes = [
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/userinfo.email",
    ]
    assert _ids({"provider": "google", "granted_scopes": scopes}) == [
        "gmail", "calendar", "drive",
    ]


def test_drive_does_not_count_as_docs_or_sheets():
    assert _ids({
        "provider": "google",
        "scopes": ["https://www.googleapis.com/auth/drive.readonly"],
    }) == ["drive"]


def test_docs_and_sheets_are_distinct():
    assert _ids({
        "provider": "google",
        "granted_scopes": [
            "https://www.googleapis.com/auth/documents",
            "https://www.googleapis.com/auth/spreadsheets",
        ],
    }) == ["docs", "sheets"]


def test_youtube_provider_is_youtube_service():
    assert _ids({
        "provider": "youtube",
        "granted_scopes": ["https://www.googleapis.com/auth/youtube.readonly"],
    }) == ["youtube"]


def test_google_connection_with_youtube_scopes_lists_youtube():
    assert "youtube" in _ids({
        "provider": "google",
        "granted_scopes": ["https://www.googleapis.com/auth/youtube.readonly"],
    })


def test_standalone_provider_is_one_service():
    assert _ids({"provider": "dropbox"}) == ["dropbox"]
    assert _ids({"provider": "slack"}) == ["slack"]


def test_zoom_types_have_distinct_labels_and_purposes_without_changing_refs():
    for field in ("scopes", "granted_scopes"):
        api = public_view({"provider": "zoom", "connection_id": "arbitrary", field: ["user:read:user"]})
        assert api["services"][0]["id"] == "zoom"
        assert api["services"][0]["label"] == "Zoom API"
        assert "permissions granted" in api["services"][0]["description"]
    webhook = public_view({"provider": "zoom", "connection_id": "not-a-special-name", "scopes": []})
    assert webhook["services"][0]["id"] == "zoom"
    assert webhook["services"][0]["label"] == "Zoom Webhooks"
    assert "event notifications" in webhook["services"][0]["description"]


def test_console_renders_zoom_types_in_separate_described_panels():
    connections = [
        {"provider": "zoom", "connection_id": "one", "scopes": ["user:read:user"],
         "status": "connected", "account_title": "Alexey"},
        {"provider": "zoom", "connection_id": "two", "status": "ready",
         "account_title": "opaque-id", "display_name": "AISL recordings"},
    ]
    # Exercise both the API response and the console's metadata fallback.
    for with_services in (False, True):
        if with_services:
            for connection in connections:
                connection["services"] = services_for(connection)
        with MiniRacer() as js:
            js.eval('''
                const nodes = {};
                const $ = id => nodes[id] ||= {value: '', dataset: {},
                    setAttribute() {}, addEventListener() {}};
                const $$ = () => [];
                const state = {data: {}};
                const escapeHtml = value => String(value);
                const formatTimestamp = () => '';
                const statusLine = value => value;
                const serviceMark = value => value;
                const document = {body: {dataset: {}}};
                const connectionsLoadMoreButton = () => ({});
                const renderConnectCards = () => {};
                const bindOAuthLinks = () => {};
            ''')
            js.eval(JS[JS.index('const CONNECT_SERVICES'):JS.index('let addPickerOpen')])
            js.eval('let addPickerOpen = false; const connectionsPage = {connections: null};')
            js.eval(JS[JS.index('function renderConnections('):JS.index('function bindOAuthLinks(')])
            js.eval(f'renderConnections({json.dumps(connections)});')
            markup = js.eval("nodes['#connection-register'].innerHTML")
        assert markup.count('<section ') == 2
        assert 'data-service="zoom-api"' in markup
        assert 'data-service="zoom-webhooks"' in markup
        assert 'Zoom API' in markup and 'Zoom Webhooks' in markup
        assert 'permissions granted' in markup and 'event notifications' in markup
        assert 'AISL recordings' in markup and 'opaque-id' not in markup
        assert '2 accounts' not in markup
        assert '1 connection' in markup
        assert 'Same sign-in' not in markup


def test_google_without_product_scopes_falls_back_to_google():
    assert services_for({
        "provider": "google",
        "scopes": ["https://www.googleapis.com/auth/userinfo.email"],
    }) == [{"id": "google", "label": "Google"}]


def test_public_view_includes_services():
    view = public_view({
        "connection_id": "google-calendar",
        "provider": "google",
        "status": "connected",
        "granted_scopes": [
            "https://www.googleapis.com/auth/calendar.readonly",
            "https://www.googleapis.com/auth/drive.readonly",
        ],
    })
    assert view["services"] == [
        {"id": "calendar", "label": "Google Calendar"},
        {"id": "drive", "label": "Google Drive"},
    ]


def test_console_catalog_matches_python():
    for spec in CATALOG:
        assert f"{spec['id']}:" in JS
        assert spec["connection_id"] in JS
        assert spec["label"] in JS
        for scope in spec.get("default_scopes") or ():
            assert scope in JS


def test_console_groups_rows_by_service():
    assert "connection-register" in HTML
    assert "Add a service" in HTML
    assert "One panel per service" in JS
    assert 'data-service="${escapeHtml(serviceId)}"' in JS
    assert "Google accounts" not in JS
    assert 'data-service="google-accounts"' not in JS
    assert "Not signed in" in JS
    assert "accountIdentity" in JS
    assert "verifiesIdentity" in JS
    assert "Same sign-in also covers" in JS
    assert "connection-remove" in JS
    assert "provider-group-row" not in JS
    assert "CONNECT_PROVIDERS" not in JS
    assert "dk-button--secondary connection-oauth" in JS
    assert "dk-button--primary connection-oauth" not in JS
    assert "Get token" not in JS
    assert "provider-token-button" not in JS
    assert "pull-trigger-sample" not in JS
    assert "openSamplePuller" not in JS


def test_zoom_oauth_rows_offer_reconnect():
    """Zoom meetings use OAuth start, same as YouTube; Slack/Telegram paste a token."""
    assert "usesOAuthConsent" in JS
    assert "provider !== 'slack' && provider !== 'telegram'" in JS
    assert "usesOAuthConsent(connection.provider) && status !== 'connected'" in JS


def test_console_puts_expiry_on_the_row_not_a_banner():
    """Expiring tokens are a row status + Reconnect; the daily digest emails."""
    assert "expiring soon" in JS
    assert "EXPIRY_HORIZON_HOURS = 48" in JS
    assert "tokenExpiringSoon" in JS
    assert "connection-expiry-banner" not in JS
    assert "Email re-auth reminder" not in JS
    assert "/api/admin/connections/expiry-digest" not in JS
