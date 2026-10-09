"""The API-token grant picker offers Zoom API connections, not Zoom webhooks."""
from pathlib import Path

JS = (Path(__file__).resolve().parents[1] / "src" / "web" / "js" / "views" / "tokens.js").read_text()


def test_grant_picker_excludes_only_zoom_webhooks():
    assert "connection.provider !== 'zoom')" not in JS
    assert "isZoomWebhook" in JS and "oauth_consent" in JS
