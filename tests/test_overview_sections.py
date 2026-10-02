"""Section reads skip unrelated stores and preserve the complete overview."""
import json

import pytest

from src.dapier.api import overview
from dapier_cli import commands, main

SECTIONS = {
    'workflows': {'workflows', 'workflow_tags', 'workflow_folders'},
    'activity': {'executions', 'runs'},
    'usage': {'usage', 'quota'},
    'connections': {'connections'},
    'credentials': {'credentials', 'oauth_clients'},
    'tokens': {'api_tokens'},
    'emails': {'email_triggers', 'email_from'},
}


@pytest.fixture
def stores(monkeypatch):
    calls = []

    def read(section, value):
        def stub(*args, **kwargs):
            calls.append(section)
            return value
        return stub

    monkeypatch.setenv('EXECUTIONS_TABLE', 'executions')
    monkeypatch.setenv('CONNECTIONS_TABLE', 'connections')
    monkeypatch.setattr(overview.visibility, 'owners_for', lambda visible: {})
    monkeypatch.setattr(overview, '_workflows', read('workflows', [
        {'id': 'mail', 'description': 'Email routing', 'tags': ['Inbox'], 'folder': 'Messages'},
        {'id': 'other', 'tags': [], 'folder': ''},
    ]))

    def scan(table):
        section = 'activity' if table == 'executions' else 'connections'
        calls.append(section)
        return [{'workflow_id': 'mail', 'started_at': '2026-10-01'}] if section == 'activity' else []

    monkeypatch.setattr(overview, '_scan', scan)
    monkeypatch.setattr(overview, '_connection_views', lambda items: items)
    monkeypatch.setattr(overview.connection_usage, 'attach', lambda items: items)
    monkeypatch.setattr(overview.runs, 'recent', read('activity', []))
    monkeypatch.setattr(overview, '_usage', read('usage', []))
    monkeypatch.setattr(overview, '_quota', read('usage', None))
    monkeypatch.setattr(overview, '_credential_status', read('credentials', {}))
    monkeypatch.setattr(overview, '_oauth_client_status', read('credentials', {}))
    monkeypatch.setattr(overview.api_tokens, 'list_all', read('tokens', []))
    monkeypatch.setattr(overview, '_email_triggers', read('emails', {}))
    monkeypatch.setattr(overview, '_email_from', read('emails', []))
    return calls


@pytest.mark.parametrize('section', SECTIONS)
def test_only_requested_stores_are_read(stores, section):
    result = overview.overview({'queryStringParameters': {'section': section}})
    assert result['statusCode'] == 200
    assert set(json.loads(result['body'])) == SECTIONS[section] | {'service', 'region'}
    assert stores and set(stores) == {section}


def test_combined_sections_match_legacy_response(stores):
    full = json.loads(overview.overview()['body'])
    merged = {}
    for section in SECTIONS:
        merged.update(json.loads(overview.overview({'queryStringParameters': {'section': section}})['body']))
    assert full == merged


def test_workflow_filters_still_apply_without_other_reads(stores):
    data = json.loads(overview.overview({'queryStringParameters': {
        'section': 'workflows', 'q': 'routing', 'tag': 'inbox', 'folder': 'messages',
    }})['body'])
    assert [row['id'] for row in data['workflows']] == ['mail']
    assert data['workflow_tags'] == ['Inbox']
    assert set(stores) == {'workflows'}


def test_invalid_section_reads_nothing(stores):
    assert overview.overview({'queryStringParameters': {'section': 'invalid'}})['statusCode'] == 400
    assert stores == []


def test_activity_section_keeps_visibility_filter(stores):
    class Scope:
        def workflow_visible(self, workflow_id, owners):
            return False

    data = json.loads(overview.overview({'queryStringParameters': {'section': 'activity'}}, visible=Scope())['body'])
    assert data['executions'] == []


def test_cli_section_reaches_shared_agent_handler(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(commands.api, 'call', lambda url, method, path, **kw:
                        calls.append((method, path)) or {'workflows': [{'id': 'mail'}]})
    assert main.main(['overview', '--section', 'workflows']) == 0
    assert calls == [('GET', '/api/agent/overview?section=workflows')]
    assert json.loads(capsys.readouterr().out)['workflows'][0]['id'] == 'mail'
