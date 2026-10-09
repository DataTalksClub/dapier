"""The console's Manage connection dialog: summary and everyday actions
first, scopes behind Advanced, destructive actions in a Danger zone, and a
footer with only Cancel / Save. No display-name field anywhere."""

import json
import re
from pathlib import Path

from py_mini_racer import MiniRacer

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "src/web/index.html").read_text()
JS = (ROOT / "src/web/js/views/connections.js").read_text()
CSS = (ROOT / "src/web/app.css").read_text()


def _dialog():
    match = re.search(r'<dialog id="edit-connection-dialog"[^>]*>.*?</dialog>', HTML, re.S)
    assert match
    return match.group(0)


def _between(markup, start, end):
    begin = markup.index(start)
    return markup[begin:markup.index(end, begin)]


def test_manage_dialog_reads_top_to_bottom():
    dialog = _dialog()
    order = ['id="edit-connection-details"', 'class="manage-actions"',
             'id="edit-slack-setup"', 'id="edit-advanced"', 'class="manage-danger"',
             'class="dialog-actions']
    positions = [dialog.index(marker) for marker in order]
    assert positions == sorted(positions)


def test_everyday_actions_are_grouped_and_destructive_ones_are_apart():
    dialog = _dialog()
    actions = _between(dialog, 'class="manage-actions"', '</div>')
    for button in ("edit-connection-test", "edit-connection-reconnect",
                   "edit-connection-discover", "edit-connection-access"):
        assert f'id="{button}"' in actions
    danger = _between(dialog, 'class="manage-danger"', '</section>')
    assert 'id="edit-connection-revoke"' in danger and 'id="edit-connection-delete"' in danger
    footer = _between(dialog, 'class="dialog-actions', '</form>')
    assert footer.count("<button") == 2
    assert "Cancel" in footer and 'id="edit-connection-save"' in footer
    assert "Revoke" not in footer and "Delete" not in footer


def test_scopes_and_cli_hint_sit_behind_advanced():
    dialog = _dialog()
    advanced = _between(dialog, '<details id="edit-advanced"', '</details>')
    assert '<textarea name="scopes"' in advanced
    assert 'id="edit-token-field"' in advanced
    assert 'id="edit-connection-cli"' in advanced


def test_provider_fields_stay_reachable():
    dialog = _dialog()
    for name in ("root_path", "signing_secret", "token", "scopes"):
        assert f'name="{name}"' in dialog
    for section in ("edit-zoom-url", "edit-slack-url", "edit-dropbox-url", "edit-youtube-url"):
        assert f'id="{section}"' in dialog


def test_no_display_name_field_in_the_console():
    assert 'name="display_name"' not in HTML
    assert "Display name" not in HTML
    assert "display_name" not in JS


def test_save_waits_for_an_edit():
    assert 'id="edit-connection-save" class="dk-button dk-button--primary" type="submit" disabled' in _dialog()
    assert "$('#edit-connection-save').disabled = false" in JS


def test_phone_width_stacks_danger_rows_and_wraps_actions():
    assert ".manage-danger-row { flex-direction: column;" in CSS
    assert ".manage-actions .dk-button { flex: 1 1 calc(50% - var(--dk-gap-control)); }" in CSS


def test_google_scopes_are_shown_short_and_saved_in_full():
    start = JS.index("const GOOGLE_SCOPE_PREFIX")
    end = JS.index("/* Manage reads top to bottom")
    with MiniRacer() as js:
        js.eval(JS[start:end])
        shown = js.eval("scopesForEditing(%s)" % json.dumps({
            "provider": "google",
            "scopes": ["https://www.googleapis.com/auth/drive.readonly", "openid"],
        }))
        assert shown == "drive.readonly\nopenid"
        saved = json.loads(js.eval("JSON.stringify(scopesFromEditing('drive.readonly\\n openid  spreadsheets', 'google'))"))
        assert saved == ["https://www.googleapis.com/auth/drive.readonly", "openid",
                         "https://www.googleapis.com/auth/spreadsheets"]
        dropbox = json.loads(js.eval("JSON.stringify(scopesFromEditing('files.metadata.read', 'dropbox'))"))
        assert dropbox == ["files.metadata.read"]


def _manage(connection):
    """Open Manage on one connection with stubbed DOM nodes; return them."""
    with MiniRacer() as js:
        js.eval(f'''
            const nodes = {{}};
            const node = () => ({{hidden: false, textContent: '', innerHTML: '', href: '',
                open: false, disabled: false, value: '', placeholder: '', dataset: {{}},
                firstChild: {{textContent: ''}}, reset() {{}}, showModal() {{}},
                addEventListener() {{}}}});
            const $ = id => nodes[id] ||= node();
            const $$ = () => [];
            const form = $('#edit-connection-form');
            for (const field of ['scopes', 'token', 'root_path', 'signing_secret']) form[field] = node();
            const state = {{data: {{connections: [{json.dumps(connection)}]}}}};
            const escapeHtml = value => String(value);
            const statusLine = value => value;
            const formatTimestamp = () => '';
            const serviceMark = value => value;
            const whenMenusClosed = (root, paint) => paint();
            const window = {{location: {{origin: 'https://dapier.test'}}}};
            function renderManageDetails() {{}}
            function usageRefs() {{ return []; }}
            function scopesForEditing(c) {{ return (c.scopes || []).join(' '); }}
        ''')
        js.eval(JS[JS.index('const CONNECT_SERVICES'):JS.index('/* Server-paged accounts register')])
        js.eval(JS[JS.index('export function openEditConnection('):JS.index('/* Save wakes up')]
                .replace('export function', 'function'))
        js.eval(f'openEditConnection({json.dumps(connection["connection_id"])})')
        return json.loads(js.eval('JSON.stringify(nodes)'))


def test_zoom_api_manage_edits_scopes_like_any_oauth_connection():
    nodes = _manage({"connection_id": "zoom-api", "provider": "zoom", "status": "connected",
                     "scopes": ["user:read:user"], "oauth_consent": True})
    form = nodes['#edit-connection-form']
    assert form['dataset'] == {'connectionId': 'zoom-api', 'provider': 'zoom',
                               'kind': 'api', 'pastesToken': ''}
    assert nodes['#edit-scopes-field']['hidden'] is False
    assert nodes['#edit-token-field']['hidden'] is True
    assert nodes['#edit-zoom-setup']['hidden'] is True
    assert nodes['#edit-connection-access']['hidden'] is False
    assert nodes['#edit-connection-revoke']['textContent'] == 'Revoke tokens'


def test_zoom_webhook_manage_keeps_its_secret_token():
    nodes = _manage({"connection_id": "zoom", "provider": "zoom", "status": "ready", "scopes": []})
    form = nodes['#edit-connection-form']
    assert form['dataset']['kind'] == 'webhook' and form['dataset']['pastesToken'] == '1'
    assert nodes['#edit-scopes-field']['hidden'] is True
    assert nodes['#edit-token-field']['hidden'] is False
    assert nodes['#edit-zoom-setup']['hidden'] is False
    assert nodes['#edit-connection-revoke']['textContent'] == 'Disable webhook'


def test_manage_save_sends_the_zoom_kind():
    save = JS[JS.index("$('#edit-connection-form').addEventListener('submit'"):]
    assert "if (form.dataset.kind) body.kind = form.dataset.kind;" in save
    assert "if (!form.dataset.pastesToken) {" in save
