"""Verified LLM connections; keys stay in the credential store, WIF uses IAM."""
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from src.dapier.connections import credentials, records
from src.dapier.connections.importing import register_connection_saver
from src.dapier.connectors import ai
from src.dapier.connectors.registry import ConnectionTest, register_connection_test

ANTHROPIC_URL = 'https://api.anthropic.com/v1'
FEDERATION_FIELDS = ('federation_rule_id', 'organization_id', 'service_account_id', 'workspace_id')
PUBLIC_FIELDS = ('auth_type', 'base_url', 'model', *FEDERATION_FIELDS)
_cache = {}


def transport(method, url, *, headers, body=None, timeout=25):
    request = urllib.request.Request(url, headers=headers, data=body, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, response.read()


def validate(config):
    if not isinstance(config, dict):
        raise ValueError('LLM configuration is required')
    kind = config.get('auth_type')
    if kind not in ('anthropic_oidc', 'api_key'):
        raise ValueError('Choose Anthropic OIDC or an API key connection')
    base = str(config.get('base_url') or (ANTHROPIC_URL if kind == 'anthropic_oidc' else '')).rstrip('/')
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('LLM base URL must be an HTTPS API endpoint')
    model = str(config.get('model') or '').strip()
    if not model:
        raise ValueError('Choose a default model for the connection')
    value = {'auth_type': kind, 'base_url': base, 'model': model}
    if kind == 'anthropic_oidc':
        if base != ANTHROPIC_URL:
            raise ValueError('Anthropic OIDC requires the official Anthropic endpoint')
        if config.get('api_key') or config.get('headers'):
            raise ValueError('Anthropic OIDC does not use an API key or custom headers')
        for field in FEDERATION_FIELDS:
            item = str(config.get(field) or '').strip()
            if not item:
                raise ValueError(f'Anthropic OIDC requires {field}')
            value[field] = item
    else:
        key = str(config.get('api_key') or '').strip()
        if not re.fullmatch(r'[!-~]{1,4096}', key):
            raise ValueError('A provider API key is required')
        value['api_key'] = key
        headers = config.get('headers') or {}
        if not isinstance(headers, dict) or not all(isinstance(k, str) and isinstance(v, str)
                and '\n' not in k + v and '\r' not in k + v for k, v in headers.items()):
            raise ValueError('Custom headers must be string pairs')
        if any(k.lower() in ('authorization', 'x-api-key', 'host') for k in headers):
            raise ValueError('Custom headers cannot override authentication or Host')
        value['headers'] = headers
    return value


def authenticate(config, *, http=None, sts=None, now=None):
    """Mint and cache short-lived tokens in worker memory; never persist JWTs."""
    if config['auth_type'] == 'api_key':
        return config['api_key']
    stamp = time.time() if now is None else now
    cache_key = tuple(config[field] for field in FEDERATION_FIELDS)
    cached = _cache.get(cache_key)
    if cached and cached[1] > stamp + 60:
        return cached[0]
    try:
        if sts is None:
            import boto3
            sts = boto3.client('sts', region_name=os.environ.get('AWS_REGION') or 'eu-west-1')
        jwt = sts.get_web_identity_token(Audience=['https://api.anthropic.com'],
            SigningAlgorithm='RS256', DurationSeconds=900)['WebIdentityToken']
        status, raw = (http or transport)('POST', f'{ANTHROPIC_URL}/oauth/token',
            headers={'content-type': 'application/json'}, body=json.dumps({
                'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer',
                'assertion': jwt, **{field: config[field] for field in FEDERATION_FIELDS},
            }).encode(), timeout=15)
        data = json.loads(raw) if status == 200 else {}
        token = data.get('access_token')
        lifetime = int(data.get('expires_in', 0))
        if not token or lifetime <= 0:
            raise ValueError()
    except Exception:
        # STS/HTTP exceptions can include tokens or assertions. Do not echo them.
        raise ValueError('Anthropic OIDC authentication failed; check the federation rule and worker identity') from None
    _cache[cache_key] = (token, stamp + lifetime)
    return token


def issue_token(connection, stored, *, transport=None):
    from src.dapier.connections.tokens import TokenError
    from src.dapier.engine.runtime_rpc import call
    if connection.get('status') != 'connected':
        raise TokenError('LLM connection is not connected')
    try:
        config = validate(stored)
        if os.environ.get('WORKER_FUNCTION_ARN') and config['auth_type'] == 'anthropic_oidc':
            result = call('connection-token', connection_id=connection['connection_id'])
            return result['token'], result['info']
        token = authenticate(config, http=transport)
        account_id = config.get('workspace_id') or config['base_url']
        records.check_binding(connection, account_id)
        cached = _cache.get(tuple(config.get(field) for field in FEDERATION_FIELDS))
        return token, {'expires_at': int(cached[1]) if cached else None,
            'scope': 'workspace:developer' if config['auth_type'] == 'anthropic_oidc' else '',
            'provider_account_id': account_id, 'account_title': connection.get('account_title'),
            'refreshed': config['auth_type'] == 'anthropic_oidc'}
    except ValueError as exc:
        raise TokenError(str(exc)) from None


def configuration(connection_id):
    import boto3
    table = boto3.resource('dynamodb').Table(os.environ['CONNECTIONS_TABLE'])
    connection = records.get_connection(table, connection_id)
    if not connection or connection.get('provider') != 'llm' or connection.get('status') != 'connected':
        raise ValueError(f'LLM connection {connection_id} is not connected')
    try:
        config = validate(credentials.get_credential(connection['credential_id']))
    except KeyError:
        raise ValueError(f'LLM connection {connection_id} has no stored configuration') from None
    return {**config, 'token': authenticate(config)}


def test_connection(connection, *, http=None):
    """Test stored auth and selected model from the worker's IAM identity."""
    from src.dapier.engine.runtime_rpc import call
    if os.environ.get('WORKER_FUNCTION_ARN') and http is None:
        return call('connection-test', connection=connection)
    try:
        config = connection.get('_llm_config')
        if config is None:
            if connection.get('status') != 'connected':
                return {'ok': False, 'detail': 'LLM connection is not connected'}
            config = credentials.get_credential(connection['credential_id'])
        config = validate(config)
        token = authenticate(config, http=http)
        headers = {'authorization': f'Bearer {token}', **config.get('headers', {})}
        if urllib.parse.urlsplit(config['base_url']).hostname == 'api.anthropic.com':
            headers['anthropic-version'] = '2023-06-01'
        anthropic = urllib.parse.urlsplit(config['base_url']).hostname == 'api.anthropic.com'
        url = (f"{config['base_url']}/models/{urllib.parse.quote(config['model'], safe='')}"
               if anthropic else f"{config['base_url']}/models")
        status, raw = (http or transport)('GET', url, headers=headers, timeout=15)
        data = json.loads(raw) if status == 200 else {}
        if anthropic:
            available = isinstance(data, dict) and bool(data.get('id'))
        else:
            models = data.get('data') if isinstance(data, dict) else None
            available = isinstance(models, list) and config['model'] in {
                item.get('id') for item in models if isinstance(item, dict)}
        if not available:
            return {'ok': False, 'detail': 'Authentication failed or the default model is unavailable'}
        return {'ok': True, 'detail': f"Authenticated; model {config['model']} is available"}
    except Exception:
        return {'ok': False, 'detail': 'LLM authentication or model check failed'}


def save_connection(body, *, operator_subject, connections_table, audit_event,
                    action, reuse_stored_token=False):
    try:
        fields = records.validate_new_connection(body)
        previous = records.get_connection(connections_table, fields['connection_id'])
        if previous and previous.get('provider') != 'llm':
            return 409, {'error': 'Connection belongs to another provider'}
        supplied = body.get('llm_config')
        if supplied is None and reuse_stored_token and previous:
            supplied = credentials.get_credential(previous['credential_id'])
        elif isinstance(supplied, dict) and reuse_stored_token and previous and supplied.get('auth_type') == 'api_key' and not supplied.get('api_key'):
            saved = credentials.get_credential(previous['credential_id'])
            if saved.get('auth_type') == 'api_key' and saved.get('base_url') == supplied.get('base_url'):
                supplied = {**supplied, 'api_key': saved.get('api_key')}
        config = validate(supplied)
        verdict = test_connection({'provider': 'llm', '_llm_config': config})
        if not verdict.get('ok'):
            return 400, {'error': verdict.get('detail') or 'LLM verification failed'}
        account_id = (config['workspace_id'] if config['auth_type'] == 'anthropic_oidc'
                      else config['base_url'])
        item = records.build_item(fields, owner_subject=operator_subject, previous=previous)
        records.check_binding(item, account_id)
        item = records.mark_connected(item, verified_account_id=account_id,
            account_title=('Anthropic · ' + config['workspace_id'] if config['auth_type'] == 'anthropic_oidc'
                           else urllib.parse.urlsplit(config['base_url']).hostname),
            granted_scopes=[], connected_by=operator_subject)
        item['llm_config'] = {key: config[key] for key in PUBLIC_FIELDS if key in config}
    except records.BindingError as exc:
        return 409, {'error': str(exc)}
    except (ValueError, KeyError) as exc:
        return 400, {'error': str(exc) if isinstance(exc, ValueError) else 'Stored LLM configuration is missing'}
    credentials.put_credential(item['credential_id'], config, provider='llm')
    records.put_connection(connections_table, item)
    audit_event(item['connection_id'], action, operator_subject or 'unknown', outcome='ok')
    return 200, records.public_view(item)


register_connection_saver('llm', save_connection)
register_connection_test(ConnectionTest(connector='llm', run=test_connection))
ai.register_connection_resolver(configuration)
from src.dapier.connections.tokens import register_token_handler
register_token_handler('llm', issue_token)
