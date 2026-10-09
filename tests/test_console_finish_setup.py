"""Finish setup leads where setup actually finishes, per connection kind.

A Zoom webhook connection (provider zoom, no scopes) stays 'ready' until Zoom
validates its callback URL; OAuth consent cannot finish it. Its row used to
render the OAuth start link, so Finish setup went nowhere useful.
"""

import json
from pathlib import Path

from py_mini_racer import MiniRacer

from src.dapier.connections import oauth_flow
from src.dapier.connections import records
from src.dapier.connections.records import public_view

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "src/web/js/views/connections.js").read_text()

ZOOM_WEBHOOK = {"connection_id": "zoom-aisl", "provider": "zoom", "status": "ready",
                "display_name": "Zoom (AISL recordings)"}
ZOOM_API = {"connection_id": "zoom-api", "provider": "zoom", "status": "ready",
            "scopes": ["user:read:user"]}
GOOGLE = {"connection_id": "google", "provider": "google", "status": "ready",
          "scopes": ["https://www.googleapis.com/auth/drive.readonly"]}
DROPBOX = {"connection_id": "dropbox", "provider": "dropbox", "status": "expired",
           "scopes": ["files.content.read"]}
SLACK = {"connection_id": "slack", "provider": "slack", "status": "revoked"}


def _row(connection):
    """Render one register row through the console's real accountRow."""
    with MiniRacer() as js:
        js.eval('''
            const escapeHtml = value => String(value);
    const whenMenusClosed = (root, paint) => paint();
            const formatTimestamp = () => '';
            const statusLine = value => value;
            const state = {data: {}};
        ''')
        js.eval(JS[JS.index('const CONNECT_SERVICES'):JS.index('/* Server-paged accounts register')])
        start = JS.index('function usageRefs(')
        js.eval(JS[start:JS.index("$('#connection-search')", start)])
        return js.eval(f'accountRow({json.dumps(connection)}, "x")')


def test_public_view_says_whether_setup_uses_oauth_consent():
    assert public_view(ZOOM_WEBHOOK)["oauth_consent"] is False
    assert public_view(SLACK)["oauth_consent"] is False
    assert public_view(ZOOM_API)["oauth_consent"] is True
    assert public_view(GOOGLE)["oauth_consent"] is True


def test_zoom_webhook_finish_setup_opens_manage_not_oauth():
    for connection in (ZOOM_WEBHOOK, public_view(ZOOM_WEBHOOK)):
        markup = _row(connection)
        assert "connection-finish" in markup
        assert "Finish setup" in markup
        assert "/api/admin/oauth/" not in markup
        assert "connection-oauth" not in markup


def test_oauth_connections_still_finish_or_reconnect_through_consent():
    for connection, label in ((ZOOM_API, "Finish setup"), (GOOGLE, "Finish setup"), (DROPBOX, "Reconnect")):
        for view in (connection, public_view(connection)):
            markup = _row(view)
            assert "connection-oauth" in markup and label in markup
            assert f"/api/admin/oauth/{connection['connection_id']}/start" in markup
            assert "connection-finish" not in markup


def test_token_connections_get_no_consent_button():
    plugin_token = {"connection_id": "dataops", "provider": "dataops", "status": "revoked",
                    "oauth_consent": False}
    for connection in (SLACK, public_view(SLACK), plugin_token):
        markup = _row(connection)
        assert "connection-oauth" not in markup
        assert "connection-finish" not in markup


def test_finish_setup_click_focuses_the_zoom_callback_url():
    assert "$$('.connection-finish').forEach" in JS
    with MiniRacer() as js:
        js.eval('''
            const calls = [];
            const nodes = {
              '#edit-zoom-setup': {hidden: false, scrollIntoView(opts) { calls.push('scroll'); }},
              '#edit-zoom-url': {focus() { calls.push('focus'); }},
            };
            const $ = id => nodes[id];
            const window = {getSelection: () => ({selectAllChildren() { calls.push('select'); }})};
            function openEditConnection(id) { calls.push('manage:' + id); }
        ''')
        start = JS.index('function openZoomWebhookSetup(')
        js.eval(JS[start:JS.index('function bindOAuthLinks(')])
        js.eval('openZoomWebhookSetup("zoom-aisl")')
        assert json.loads(js.eval('JSON.stringify(calls)')) == [
            "manage:zoom-aisl", "scroll", "focus", "select"]


def test_oauth_start_rejects_zoom_webhook(monkeypatch):
    monkeypatch.setattr(oauth_flow, "_connection", lambda connection_id: dict(ZOOM_WEBHOOK))
    event = {"requestContext": {"http": {"method": "GET", "path": "/api/admin/oauth/zoom-aisl/start"}},
             "headers": {"host": "dapier.example.test"}, "cookies": []}

    response = oauth_flow.oauth_start(event, "zoom-aisl")

    assert response["statusCode"] == 400
    assert "validates the connection's callback URL" in response["body"]
    assert records.uses_oauth_consent(ZOOM_API)
