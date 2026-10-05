"""Google connections group by product (Gmail, Calendar, Drive…), not provider."""

from pathlib import Path

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
