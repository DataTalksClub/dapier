"""Adding a connection starts from its service: the Services list beside the
accounts carries one add button per service (no page-level Add connection
button, no picker that appends content at the bottom of the page), and the
Accounts list shows every connection once, with the services it covers."""
import json
import re
from pathlib import Path

from py_mini_racer import MiniRacer

ROOT = Path(__file__).resolve().parents[1] / "src" / "web"
INDEX = (ROOT / "index.html").read_text()
JS = (ROOT / "js" / "views" / "connections.js").read_text()
CSS = (ROOT / "app.css").read_text()

STUBS = '''
    const nodes = {};
    const $ = id => nodes[id] ||= {innerHTML: '', textContent: '', hidden: false, value: '',
        dataset: {}, setAttribute() {}, addEventListener() {}};
    const $$ = () => [];
    const state = {data: {oauth_clients: []}};
    const escapeHtml = value => String(value);
    const formatTimestamp = () => '';
    const statusLine = value => value;
    const serviceMark = value => `<i>${value}</i>`;
    const document = {body: {dataset: {}}};
    const connectionsLoadMoreButton = () => ({});
    const bindOAuthLinks = () => {};
    const connectionsPage = {connections: null};
    function allKnownConnections() { return []; }
'''


def _service_ids():
    block = re.search(r"const CONNECT_SERVICES = \{(.*?)\n\};", JS, re.S)
    assert block
    return re.findall(r"^  ([a-z]+): \{", block.group(1), re.M)


def _render(connections):
    with MiniRacer() as js:
        js.eval(STUBS)
        js.eval(JS[JS.index('const CONNECT_SERVICES'):JS.index('/* Server-paged accounts register')])
        js.eval(JS[JS.index('/* Services with their own add buttons'):JS.index('function allKnownConnections() {')])
        js.eval(JS[JS.index('function reusableGoogleConnections('):JS.index('async function connectService(')])
        js.eval(JS[JS.index('function renderConnections('):JS.index('function bindOAuthLinks(')])
        js.eval(f'renderConnections({json.dumps(connections)});')
        return {key: json.loads(js.eval(f'JSON.stringify(nodes["{key}"])'))
                for key in ('#connection-register', '#connect-grid', '#connect-services',
                            '#connect-services-title')}


def _offered(markup):
    return re.findall(r'connect-button" data-service="([a-z]+)"', markup)


def test_no_page_level_add_button_or_picker():
    section = INDEX.split('data-page="connections"')[1].split('data-page="credentials"')[0]
    assert 'id="add-connection"' not in INDEX
    assert 'connect-picker' not in INDEX + JS + CSS
    assert 'connect-dialog' not in INDEX + JS + CSS
    assert 'id="connection-empty"' not in INDEX
    # Accounts first, the services (each with its add button) beside them.
    assert section.index('id="connection-register"') < section.index('id="connect-services"')
    assert 'id="connect-grid"' in section


def test_empty_register_is_just_the_service_list():
    nodes = _render([])
    assert nodes['#connect-services']['hidden'] is False
    assert nodes['#connect-services-title']['textContent'] == 'Connect a service'
    assert _offered(nodes['#connect-grid']['innerHTML']) == _service_ids()
    assert nodes['#connection-register']['innerHTML'] == ''


def test_every_service_keeps_its_own_add_button_and_accounts_show_once():
    connections = [
        {"connection_id": "g1", "provider": "google", "status": "connected",
         "account_title": "a@example.com",
         "granted_scopes": ["https://www.googleapis.com/auth/gmail.readonly",
                            "https://www.googleapis.com/auth/drive.readonly"]},
        {"connection_id": "s1", "provider": "slack", "status": "connected", "account_title": "Team"},
        {"connection_id": "z1", "provider": "zoom", "status": "ready", "scopes": []},
        {"connection_id": "z2", "provider": "zoom", "status": "connected",
         "account_title": "z@example.com", "scopes": ["user:read:user"]},
    ]
    nodes = _render(connections)
    register = nodes['#connection-register']['innerHTML']
    # One row per account: the Google sign-in covering Gmail and Drive is
    # listed once, with both services on it.
    rows = re.findall(r'data-connection-row="([^"]+)"', register)
    assert rows.count('g1') == 1 and set(rows) == {'g1', 's1', 'z1', 'z2'}
    g1 = register[register.index('data-connection-row="g1"'):]
    g1 = g1[:g1.index('</li>')]
    assert 'Gmail' in g1 and 'Drive' in g1
    assert 'Zoom Webhooks' in register and 'Zoom API' in register
    # Every service keeps its own add button, connected or not.
    services = nodes['#connect-grid']['innerHTML']
    assert _offered(services) == _service_ids()
    assert 'aria-label="Add Gmail account"' in services
    assert 'aria-label="Add Zoom webhook"' in services
    gmail = services[services.index('data-service-row="gmail"'):]
    assert '1 account' in gmail[:gmail.index('</li>')]
    assert nodes['#connect-services-title']['textContent'] == 'Services'


def test_add_buttons_start_the_service_flow_in_place():
    assert "await connectService(button.dataset.service);" in JS
    render = JS[JS.index('function renderConnections('):JS.index('function bindOAuthLinks(')]
    assert 'bindConnectButtons();' in render
    assert 'scrollIntoView' not in JS[JS.index('/* Services with their own add buttons'):
                                      JS.index('function allKnownConnections() {')]


def test_phone_layout_keeps_add_buttons_tappable():
    # dakit grows every .dk-button to 44px on phones and coarse pointers.
    dakit = (ROOT / "vendor" / "dakit.css").read_text()
    assert ".dk-button, .dk-input, .dk-select, .dk-filter-chip { min-height: var(--dk-size-touch); }" in dakit
    assert 'dk-button dk-button--secondary dk-button--sm connect-button' in JS
