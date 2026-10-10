import json
import pytest
from src.dapier.connectors import registry
from src.dapier.connections import importing, credentials, discovery, records
from src.dapier.api.admin import connections as admin
from src.dapier.api import agent
from dapier_cli.commands import connections as cli

TOKEN = 'a' * 40


def verifier(token, *, transport=None):
    return importing.TOKEN_PROVIDER_VERIFIERS['cmp'](token, transport=transport)


def test_verification_health_and_safe_errors(monkeypatch):
    seen = []
    def transport(method, url, **kwargs):
        seen.append((method, url, kwargs['headers']))
        return 200, json.dumps({'courses': []}).encode()
    assert verifier(TOKEN, transport=transport) == ('courses.datatalks.club', 'Course Management Platform production')
    assert seen[0][:2] == ('GET', 'https://courses.datatalks.club/api/courses/')
    assert seen[0][2] == {'authorization': 'Token ' + TOKEN}
    monkeypatch.setattr(credentials, 'get_credential', lambda _: {'token': TOKEN})
    verdict = registry.CONNECTION_TESTS['cmp'].run({'provider': 'cmp', 'credential_id': 'oauth#cmp'}, transport=transport)
    assert verdict['ok'] and verdict['identity']['id'] == 'courses.datatalks.club'
    for status, raw in [(401, b'{}'), (200, b'{'), (200, b'{"user":{"id":"operator"}}'), (200, b'{"service":{"id":"courses.datatalks.club","scopes":["admin"]}}')]:
        with pytest.raises(ValueError) as error:
            verifier(TOKEN, transport=lambda *a, **kw: (status, raw))
        assert TOKEN not in str(error.value)
    with pytest.raises(ValueError): verifier('invalid')


def test_console_and_agent_store_the_same_verified_scoped_connection(monkeypatch):
    stored = {}
    secrets = {}
    monkeypatch.setenv('CONNECTIONS_TABLE', 'connections')
    monkeypatch.setattr(admin.boto3, 'resource', lambda _: type('Dynamo', (), {'Table': lambda self, name: stored})())
    monkeypatch.setattr(records, 'get_connection', lambda table, key: stored.get(key))
    monkeypatch.setattr(records, 'put_connection', lambda table, item: stored.update({item['connection_id']: item}))
    monkeypatch.setattr(credentials, 'put_credential', lambda key, value, **kwargs: secrets.update({key: value}))
    monkeypatch.setattr(importing, 'verify_token_provider', lambda provider, token: ('courses.datatalks.club', 'Course Management Platform production'))
    monkeypatch.setattr(admin.session, '_session_subject', lambda event: 'operator')
    monkeypatch.setattr(admin.session, '_audit_event', lambda *a, **kw: None)
    monkeypatch.setattr(agent, 'authenticate', lambda event: ('operator', None))
    monkeypatch.setattr(agent, '_is_operator', lambda *a: True)
    monkeypatch.setattr(agent, '_tables', lambda: (stored, {}))
    monkeypatch.setattr(agent.audit, 'emit', lambda *a, **kw: None)
    for name, save in [('console', admin.save_connection), ('cli', agent.import_connection)]:
        body = {'connection_id': f'cmp-{name}', 'provider': 'cmp', 'token': TOKEN, 'scopes': ['admin']}
        result = save({'body': json.dumps(body)})
        assert result['statusCode'] == 200, result
        public = json.loads(result['body'])
        assert public['status'] == 'connected'
        assert public['granted_scopes'] == []
        assert TOKEN not in result['body']
        assert secrets[f'oauth#cmp-{name}'] == {'token': TOKEN}


def test_cli_import_reads_private_file_and_uses_existing_api(monkeypatch, tmp_path, capsys):
    token_file = tmp_path / 'token'
    token_file.write_text(TOKEN)
    calls = []
    monkeypatch.setattr(cli.api, 'call', lambda *args, **kw: calls.append(args) or {'connection_id': 'cmp', 'account_title': 'Course Management Platform production'})
    assert cli.connections_import('https://dapier.example.test', 'cmp', 'cmp', None, None, None, token_path=str(token_file)) == 0
    assert calls[0][1:3] == ('POST', '/api/agent/connections/import')
    assert calls[0][3]['token'] == TOKEN
    assert TOKEN not in capsys.readouterr().out


def test_static_token_issuance_verifies_and_enforces_binding(monkeypatch):
    from src.dapier.connections import tokens
    connection = {'connection_id': 'cmp-prod', 'provider': 'cmp', 'status': 'connected',
                  'verified_account_id': 'courses.datatalks.club', 'granted_scopes': []}
    monkeypatch.setattr(tokens, '_record', lambda _: {'value': {'token': TOKEN}})
    transport = lambda *a, **kw: (200, b'{"courses": []}')
    token, info = tokens.get_access_token(connection, transport=transport)
    assert token == TOKEN and info['provider_account_id'] == 'courses.datatalks.club'
    assert not info['refreshed'] and info['expires_at'] is None
    with pytest.raises(tokens.TokenError) as error:
        tokens.get_access_token(connection, transport=lambda *a, **kw: (401, TOKEN.encode()))
    assert TOKEN not in str(error.value)
    with pytest.raises(records.BindingError):
        tokens.get_access_token({**connection, 'verified_account_id': 'wrong'}, transport=transport)
    with pytest.raises(tokens.TokenError):
        tokens.get_access_token({**connection, 'status': 'revoked'}, transport=transport)


def test_console_has_cmp_create_and_maintenance_parity():
    from pathlib import Path
    js = Path('src/web/js/views/connections.js').read_text()
    assert "provider: 'cmp'" in js
    assert "connectionId: 'cmp-production'" in js
    assert "['slack', 'telegram', 'cmp', 'zoom']" in js
