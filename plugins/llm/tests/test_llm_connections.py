import io
import json
from unittest.mock import Mock

import pytest

from plugins.llm import plugin
from src.dapier.api import discovery
from src.dapier.engine import worker, runtime_rpc
from src.dapier.connections import importing
from src.dapier.connectors import ai


WIF = {'auth_type': 'anthropic_oidc', 'base_url': plugin.ANTHROPIC_URL,
       'model': 'claude-sonnet-5-5', 'federation_rule_id': 'fdrl_test',
       'organization_id': 'org', 'service_account_id': 'svac_test', 'workspace_id': 'wrkspc_test'}
KEY = {'auth_type': 'api_key', 'base_url': 'https://llm.example/v1', 'model': 'small', 'api_key': 'secret-key'}


@pytest.fixture(autouse=True)
def clear_cache(monkeypatch):
    monkeypatch.delenv('WORKER_FUNCTION_ARN', raising=False)
    plugin._cache.clear()
    yield
    plugin._cache.clear()


def test_oidc_exchange_and_refresh_never_use_env_key(monkeypatch):
    monkeypatch.setenv('LLM_API_KEY', 'wrong-env-key')
    sts = Mock()
    sts.get_web_identity_token.return_value = {'WebIdentityToken': 'secret-jwt'}
    http = Mock(return_value=(200, b'{"access_token":"secret-bearer","expires_in":600}'))
    assert plugin.authenticate(WIF, http=http, sts=sts, now=100) == 'secret-bearer'
    assert plugin.authenticate(WIF, http=http, sts=sts, now=200) == 'secret-bearer'
    assert http.call_count == 1
    sts.get_web_identity_token.assert_called_with(Audience=['https://api.anthropic.com'], SigningAlgorithm='RS256', DurationSeconds=900)
    request = json.loads(http.call_args.kwargs['body'])
    assert request['assertion'] == 'secret-jwt'
    assert request['federation_rule_id'] == WIF['federation_rule_id']
    assert plugin.authenticate(WIF, http=http, sts=sts, now=641) == 'secret-bearer'
    assert http.call_count == 2


def test_federation_error_redacts_assertion_and_token():
    sts = Mock()
    sts.get_web_identity_token.side_effect = ValueError('secret-jwt')
    with pytest.raises(ValueError) as error:
        plugin.authenticate(WIF, sts=sts)
    assert 'secret-jwt' not in str(error.value)
    assert 'OIDC authentication failed' in str(error.value)


@pytest.mark.parametrize('config', [{}, {**KEY, 'api_key': ''}, {**WIF, 'workspace_id': ''},
    {**WIF, 'api_key': 'secret'}, {**WIF, 'base_url': 'https://other.example/v1'},
    {**KEY, 'base_url': 'http://other.example'}, {**KEY, 'headers': {'Authorization': 'other'}}])
def test_invalid_config_refused(config):
    with pytest.raises(ValueError):
        plugin.validate(config)


def test_api_key_provider_test_checks_stored_model():
    http = Mock(return_value=(200, b'{"data":[{"id":"small"}]}'))
    result = plugin.test_connection({'_llm_config': KEY}, http=http)
    assert result['ok'] is True
    assert http.call_args.kwargs['headers']['authorization'] == 'Bearer secret-key'
    http.return_value = (200, b'{"data":[{"id":"other"}]}')
    assert plugin.test_connection({'_llm_config': KEY}, http=http)['ok'] is False


def test_anthropic_test_resolves_model_alias(monkeypatch):
    monkeypatch.setattr(plugin, 'authenticate', lambda config, **kwargs: 'oidc-bearer')
    http = Mock(return_value=(200, b'{"id":"claude-sonnet-5-5-dated"}'))
    assert plugin.test_connection({'_llm_config': WIF}, http=http)['ok']
    assert http.call_args.args[1].endswith('/models/claude-sonnet-5-5')
    assert http.call_args.kwargs['headers']['anthropic-version'] == '2023-06-01'


def test_test_runs_on_worker_identity(monkeypatch):
    monkeypatch.setenv('WORKER_FUNCTION_ARN', 'worker')
    call = Mock(return_value={'ok': True, 'detail': 'verified'})
    monkeypatch.setattr(runtime_rpc, 'call', call)
    candidate = {'provider': 'llm', '_llm_config': WIF}
    assert plugin.test_connection(candidate)['ok']
    call.assert_called_once_with('connection-test', connection=candidate)


def test_save_verifies_before_writing_and_hides_key(monkeypatch):
    monkeypatch.setattr(plugin.records, 'get_connection', lambda *a: None)
    put_secret = Mock()
    put_record = Mock()
    monkeypatch.setattr(plugin.credentials, 'put_credential', put_secret)
    monkeypatch.setattr(plugin.records, 'put_connection', put_record)
    verdict = Mock(return_value={'ok': False, 'detail': 'provider rejected key'})
    monkeypatch.setattr(plugin, 'test_connection', verdict)
    audit = Mock()
    kwargs = dict(operator_subject='operator', connections_table=Mock(), audit_event=audit, action='connect')
    for save in (plugin.save_connection, importing.save_token_connection):
        status, _ = save({'provider': 'llm', 'connection_id': 'llm', 'llm_config': KEY}, **kwargs)
        assert status == 400
    assert not put_secret.called and not put_record.called
    verdict.return_value = {'ok': True}
    status, public = importing.save_token_connection({'provider': 'llm', 'connection_id': 'llm', 'llm_config': KEY}, **kwargs)
    assert status == 200
    assert public['status'] == 'connected'
    assert 'secret-key' not in json.dumps(public)
    assert put_secret.call_args.args[1]['api_key'] == 'secret-key'
    assert not public['oauth_consent']


def test_rpc_preserves_action_result_and_does_not_record_run(monkeypatch):
    run = Mock(return_value={'ok': True, 'text': 'hello'})
    from src.dapier.connectors import registry
    monkeypatch.setitem(registry.ACTIONS, 'ai_complete', Mock(run=run))
    result = worker.handler({'dapier_rpc': 'action', 'action': {'type': 'ai_complete'}, 'event': {}}, None)
    assert result == {'ok': True, 'text': 'hello'}
    run.assert_called_once()


def test_ai_api_tests_delegate_to_worker(monkeypatch):
    monkeypatch.setenv('WORKER_FUNCTION_ARN', 'worker')
    call = Mock(return_value={'ok': True, 'text': 'hello'})
    monkeypatch.setattr(runtime_rpc, 'call', call)
    assert ai.run_ai_complete({'type': 'ai_complete', 'prompt': 'hello'}, {})['ok']
    call.assert_called_once()


def test_shared_connection_test_route(monkeypatch):
    monkeypatch.setattr(discovery, '_load', lambda *a: ({'provider': 'llm', 'connection_id': 'llm', 'status': 'revoked'}, None))
    status, verdict = discovery.test_connection('llm')
    assert status == 200 and verdict['ok'] is False


def test_cli_llm_import_calls_shared_api(tmp_path, monkeypatch):
    from dapier_cli import commands
    from dapier_cli import api
    config = tmp_path / 'config.json'
    config.write_text(json.dumps(WIF))
    call = Mock(return_value={'connection_id': 'llm'})
    monkeypatch.setattr(api, 'call', call)
    assert commands.connections_import('https://dapier.example', 'llm', 'llm', None, None, None, config_path=str(config)) == 0
    assert call.call_args.args[1:3] == ('POST', '/api/agent/connections/import')
    assert call.call_args.args[3]['llm_config'] == WIF


def test_console_and_cli_save_share_verified_connection_path(monkeypatch):
    from src.dapier.api.admin import connections as admin
    from src.dapier.api import agent
    stored, secrets = {}, {}
    monkeypatch.setenv('CONNECTIONS_TABLE', 'connections')
    monkeypatch.setattr(admin.boto3, 'resource', lambda _: Mock(Table=lambda name: stored))
    monkeypatch.setattr(plugin.records, 'get_connection', lambda table, key: stored.get(key))
    monkeypatch.setattr(plugin.records, 'put_connection', lambda table, item: stored.update({item['connection_id']: item}))
    monkeypatch.setattr(plugin.credentials, 'put_credential', lambda key, value, **kwargs: secrets.update({key: value}))
    monkeypatch.setattr(plugin, 'test_connection', lambda connection: {'ok': True})
    monkeypatch.setattr(admin.session, '_session_subject', lambda event: 'operator')
    monkeypatch.setattr(admin.session, '_audit_event', lambda *a, **kw: None)
    monkeypatch.setattr(agent, 'authenticate', lambda event: ('operator', None))
    monkeypatch.setattr(agent, '_is_operator', lambda *a: True)
    monkeypatch.setattr(agent, '_tables', lambda: (stored, {}))
    monkeypatch.setattr(agent.audit, 'emit', lambda *a, **kw: None)
    for surface, save in [('console', admin.save_connection), ('cli', agent.import_connection)]:
        result = save({'body': json.dumps({'provider': 'llm', 'connection_id': f'llm-{surface}', 'llm_config': WIF})})
        assert result['statusCode'] == 200
        assert json.loads(result['body'])['llm_config'] == WIF
        assert secrets[f'oauth#llm-{surface}'] == WIF


def test_llm_key_token_issuance_and_revoke_keep_credentials_private(monkeypatch):
    from src.dapier.connections import tokens
    connection = {'provider': 'llm', 'connection_id': 'llm-key', 'status': 'connected',
                  'verified_account_id': KEY['base_url'], 'credential_id': 'oauth#llm-key'}
    monkeypatch.setattr(tokens, '_record', lambda _: {'value': KEY, 'version': 1})
    token, info = tokens.get_access_token(connection)
    assert token == KEY['api_key'] and info['provider_account_id'] == KEY['base_url']
    with pytest.raises(tokens.TokenError):
        tokens.get_access_token({**connection, 'status': 'revoked'})
    save = Mock()
    monkeypatch.setattr(tokens, 'put_credential_if_version', save)
    revoked = tokens.revoke_connection(connection)
    assert revoked['status'] == 'revoked'
    assert save.call_args.args[1] == {}


def test_deployment_has_no_llm_key_environment_or_parameter():
    from pathlib import Path
    template = Path('template.yaml').read_text()
    workflow = Path('.github/workflows/deploy.yml').read_text()
    assert 'LLM_API_KEY' not in template + workflow
    assert 'LlmApiKey:' not in template
    assert 'ANTHROPIC_API_KEY' not in template + workflow
    assert 'lambda:InvokeFunction' in template


def test_llm_actions_use_standard_connection_reference_resolution(monkeypatch):
    from src.dapier.engine.actions import base
    resolve = Mock(return_value={'provider': 'llm', 'status': 'connected', 'credential_id': 'oauth#llm-private'})
    monkeypatch.setattr(base, '_connected_connection', resolve)
    monkeypatch.setattr(plugin.credentials, 'get_credential', lambda key: KEY)
    assert plugin.configuration('llm my-account')['token'] == KEY['api_key']
    resolve.assert_called_once_with('llm my-account')


def test_worker_rpc_serializes_dynamodb_numbers(monkeypatch):
    import boto3
    from decimal import Decimal
    monkeypatch.setenv('WORKER_FUNCTION_ARN', 'worker')
    invoke = Mock(return_value={'Payload': io.BytesIO(b'{"ok":true}')})
    monkeypatch.setattr(boto3, 'client', lambda service: Mock(invoke=invoke))
    result = runtime_rpc.call('connection-test', connection={'provider': 'llm', 'version': Decimal(1)})
    assert result['ok']
    request = json.loads(invoke.call_args.kwargs['Payload'])
    assert request['connection']['version'] == 1
