"""Adding a connection starts from the service group it lands in: every
register panel carries its own + button, and services with no group yet are
listed below the groups. No page-level Add connection button or picker that
appends content at the bottom of the page."""
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
    const whenMenusClosed = (root, paint) => paint();
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
        js.eval(JS[JS.index('/* Services with no register group yet'):JS.index('function allKnownConnections() {')])
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
    # The not-yet-connected list sits right below the register groups.
    assert section.index('id="connection-register"') < section.index('id="connect-services"')
    assert 'id="connect-grid"' in section


def test_empty_register_is_just_the_service_list():
    nodes = _render([])
    assert nodes['#connect-services']['hidden'] is False
    assert nodes['#connect-services-title']['textContent'] == 'Connect a service'
    assert _offered(nodes['#connect-grid']['innerHTML']) == _service_ids()
    assert nodes['#connection-register']['innerHTML'] == ''


def test_every_group_gets_its_own_add_button():
    connections = [
        {"connection_id": "g1", "provider": "google", "status": "connected",
         "account_title": "a@example.com",
         "granted_scopes": ["https://www.googleapis.com/auth/gmail.readonly"]},
        {"connection_id": "s1", "provider": "slack", "status": "connected", "account_title": "Team"},
        {"connection_id": "z1", "provider": "zoom", "status": "ready", "scopes": []},
        {"connection_id": "z2", "provider": "zoom", "status": "connected",
         "account_title": "z@example.com", "scopes": ["user:read:user"]},
    ]
    nodes = _render(connections)
    register = nodes['#connection-register']['innerHTML']
    panels = dict(re.findall(r'<section class="data-panel service-panel" data-service="([a-z-]+)">(.*?)</section>',
                             register, re.S))
    assert set(panels) == {'gmail', 'slack', 'zoom-webhooks', 'zoom-api'}
    assert _offered(panels['gmail']) == ['gmail']
    assert 'aria-label="Add Gmail account"' in panels['gmail']
    assert _offered(panels['slack']) == ['slack']
    # The webhook group's + runs the webhook (secret token) setup.
    assert _offered(panels['zoom-webhooks']) == ['zoom']
    assert 'aria-label="Add Zoom webhook"' in panels['zoom-webhooks']
    # The console has no Zoom API (OAuth) setup flow, so that group has no +.
    assert _offered(panels['zoom-api']) == []
    # Services with a group drop out of the list; the rest stay connectable.
    listed = _offered(nodes['#connect-grid']['innerHTML'])
    assert 'gmail' not in listed and 'slack' not in listed and 'zoom' not in listed
    assert set(listed) | {'gmail', 'slack', 'zoom'} == set(_service_ids())
    assert nodes['#connect-services-title']['textContent'] == 'Connect another service'


def test_add_buttons_start_the_service_flow_in_place():
    assert "await connectService(button.dataset.service);" in JS
    render = JS[JS.index('function renderConnections('):JS.index('function bindOAuthLinks(')]
    assert 'bindConnectButtons();' in render
    assert 'scrollIntoView' not in JS[JS.index('/* Services with no register group yet'):
                                      JS.index('function allKnownConnections() {')]


def test_shared_sign_in_shows_once_with_compact_references():
    """A Google grant covering Gmail and Drive is one full row (under its
    first service); Drive lists it as a one-line reference, not a repeat."""
    connections = [{"connection_id": "g1", "provider": "google", "status": "connected",
                    "account_title": "a@example.com",
                    "granted_scopes": ["https://www.googleapis.com/auth/gmail.readonly",
                                       "https://www.googleapis.com/auth/drive.readonly"]}]
    register = _render(connections)['#connection-register']['innerHTML']
    assert register.count('class="service-account"') == 1
    assert register.count('class="service-ref"') == 1
    assert 'Same sign-in as Gmail' in register
    assert 'Same sign-in also covers Drive' in register


def test_phone_layout_keeps_add_buttons_tappable():
    assert 'width: var(--dk-size-touch); height: var(--dk-size-touch);' in CSS[CSS.index('  .service-add { display: inline-grid;'):]
    # Labelled buttons carry no icon; the + appears only icon-only on phones.
    assert '.service-add svg { display: none; }' in CSS
    assert 'PLUS_ICON' not in JS[JS.index('function renderConnectList('):JS.index('function bindConnectButtons(')]
    assert '.connect-card { grid-template-columns: minmax(0, 1fr) auto;' in CSS
