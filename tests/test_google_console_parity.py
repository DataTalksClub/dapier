"""Console Google aliases share operator-gated handlers with the CLI."""
import json

import pytest

from src.dapier.api import agent
from src.dapier.api.admin import dispatch


@pytest.mark.parametrize('service,route_name', [
    ('gmail', 'route_gmail'), ('gdrive', 'route_gdrive'), ('calendar', 'route_calendar')])
def test_console_google_dispatch_reuses_handler(monkeypatch, service, route_name):
    seen = []
    module = getattr(agent, service)
    monkeypatch.setattr(module, route_name, lambda event, method, path:
                        seen.append((event, method, path)) or {'statusCode': 200})
    payload = {'email': 'operator@example.com'}
    response = dispatch._route_google({'body': '{}'}, 'POST', f'/api/admin/{service}/test',
                                      payload, 'operator-sub')
    assert response['statusCode'] == 200
    event, method, path = seen[0]
    assert path == f'/api/agent/{service}/test' and method == 'POST'
    assert event['_console_google_subject'] == 'operator-sub'
    assert event['_console_google_payload'] == payload


def test_console_google_requires_session_and_csrf(monkeypatch):
    monkeypatch.setattr(dispatch.session, 'authenticated', lambda e: False)
    assert dispatch.route({}, 'POST', '/api/admin/gmail/send')['statusCode'] == 401
    monkeypatch.setattr(dispatch.session, 'authenticated', lambda e: True)
    monkeypatch.setattr(dispatch.session, '_csrf_ok', lambda e, m: False)
    assert dispatch.route({}, 'POST', '/api/admin/gmail/send')['statusCode'] == 403


def test_console_google_action_gate(monkeypatch):
    from src.dapier.api.agent import common
    event = {'_console_google_payload': {'email': 'operator@example.com'},
             '_console_google_subject': 'operator-sub'}
    monkeypatch.setattr(common.actors, 'action_allowed', lambda p, a: a == 'gmail.reply')
    monkeypatch.setattr(common.audit, 'emit', lambda *a, **k: None)
    assert common.require_operator(event, 'gmail.reply') == ('operator-sub', None)
    assert common.require_operator(event, 'gmail.send')[1]['statusCode'] == 403


def test_google_tools_browser_preview_and_shared_request():
    pytest.importorskip('playwright.sync_api')
    from playwright.sync_api import sync_playwright
    from test_console_boot import console_server, _chromium
    with console_server() as origin, sync_playwright() as playwright:
        browser = _chromium(playwright)
        page = browser.new_page()
        calls = []
        def reply(route):
            calls.append(route.request)
            route.fulfill(json={'message_id': 'mock-send', 'thread_id': 'mock-thread'})
        page.route('**/api/admin/gmail/reply', reply)
        page.goto(origin + '/')
        page.locator('#app').wait_for(state='visible')
        page.evaluate("""async () => {
            const {openGoogleTools} = await import('/assets/js/views/google-tools.js');
            openGoogleTools({connection_id: 'google-explicit', verified_account_id: 'chosen@example.com'});
        }""")
        dialog = page.locator('#google-tools-dialog')
        dialog.locator('[name=operation]').select_option(label='Create reply draft')
        dialog.locator('[name=parameters]').fill(json.dumps({
            'message_id': 'm1', 'text': 'Reply ü', 'draft': True,
            'account': 'must-not-override-the-selected-account'}))
        dialog.locator('[name=files]').set_input_files({
            'name': 'report.bin', 'mimeType': 'application/octet-stream', 'buffer': b'\xff\x00'})
        dialog.locator('[data-preview]').click()
        page.wait_for_function("() => document.querySelector('[data-result]').textContent.includes('google-explicit')")
        assert not calls
        assert 'report.bin' in dialog.locator('[data-result]').inner_text()
        assert '/wA=' not in dialog.locator('[data-result]').inner_text()
        dialog.locator('[data-run]').click()
        page.wait_for_function("() => document.querySelector('[data-result]').textContent.includes('mock-send')")
        body = calls[0].post_data_json
        assert body['account'] == 'google-explicit'
        assert body['draft'] is True and body['message_id'] == 'm1'
        assert body['attachments'][0]['content_b64'] == '/wA='
        browser.close()
