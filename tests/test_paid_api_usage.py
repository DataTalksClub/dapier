"""Paid inference requests count even when replies fail workflow validation."""
import json
from datetime import datetime, timezone
from decimal import Decimal

import boto3
import pytest
from moto import mock_aws

from src.dapier import paid_api_usage as costs
from src.dapier.connectors import ai


@pytest.fixture
def ledger(monkeypatch):
    with mock_aws():
        monkeypatch.setenv('PAID_API_USAGE_TABLE', 'paid-usage')
        table = boto3.resource('dynamodb').create_table(
            TableName='paid-usage', BillingMode='PAY_PER_REQUEST',
            KeySchema=[{'AttributeName': 'month', 'KeyType': 'HASH'}, {'AttributeName': 'request_key', 'KeyType': 'RANGE'}],
            AttributeDefinitions=[{'AttributeName': 'month', 'AttributeType': 'S'}, {'AttributeName': 'request_key', 'AttributeType': 'S'}])
        yield table


def test_reported_zero_is_distinct_from_unknown():
    assert costs.cost_metadata('openrouter', 'jev', {'cost': 0})['cost_source'] == 'provider-reported'
    for value in (None, -1, 'nan', 'infinity', 'invalid', True):
        result = costs.cost_metadata('openrouter', 'jev', {'cost': value})
        assert result['cost_usd'] is None
        assert result['cost_source'] == 'unpriced'


def test_estimate_cache_and_unknown_tariff():
    usage = {'prompt_tokens': 1000, 'completion_tokens': 100,
             'prompt_tokens_details': {'cached_tokens': 500}}
    result = costs.cost_metadata('anthropic', 'claude-sonnet-4-6', usage)
    assert result['cost_usd'] == Decimal('0.00315')
    assert result['cost_source'] == 'estimate'
    native = costs.cost_metadata('anthropic', 'claude-sonnet-4-6', {'input_tokens': 10, 'output_tokens': 10, 'cache_read_input_tokens': 100})
    assert native['cost_usd'] == Decimal('0.00021')
    assert costs.cost_metadata('anthropic', 'unknown-model', usage)['cost_usd'] is None
    assert costs.cost_metadata('anthropic', 'claude-sonnet-4-6', {**usage, 'cache_creation_input_tokens': 10})['cost_usd'] is None
    assert costs.cost_metadata('anthropic', 'claude-sonnet-4-6', {**usage, 'service_tier': 'fast'})['cost_usd'] is None
    assert costs.cost_metadata('anthropic', 'claude-sonnet-4-6', {**usage, 'prompt_tokens_details': ['malformed']})['cost_source'] == 'estimate'


def test_duplicate_response_counted_once_new_retry_is_separate(ledger):
    now = datetime.now(timezone.utc)
    for request_id in ('reply-1', 'reply-1', 'reply-2'):
        costs.record_response(provider='openrouter', model='jev', response={'id': request_id, 'usage': {'cost': .01}}, workflow_id='flow', now=now)
    status, data = costs.api_costs()
    assert status == 200
    assert len(data['requests']) == 2
    assert data['reported_usd'] == Decimal('.02')
    assert data['estimated_usd'] == 0


def test_paid_invalid_reply_and_probes_recorded(ledger, monkeypatch):
    monkeypatch.setattr(ai, '_connection_resolver', lambda _: {'token': 'test', 'base_url': 'https://openrouter.ai/api/v1', 'model': 'test-model'})
    response = {'id': 'request-invalid', 'model': 'test-model', 'choices': [{'message': {'content': 'not JSON'}}], 'usage': {'cost': .1, 'prompt_tokens': 1, 'completion_tokens': 2}}
    result = ai.run_ai_complete({'prompt': 'test', 'json_mode': True, 'connection_id': 'llm-test'}, {}, transport=lambda *a, **k: (200, json.dumps(response).encode()), workflow_id='connection-test')
    assert result['ok'] is False
    row = ledger.scan()['Items'][0]
    assert row['context'] == 'connection-test'
    assert row['connection_id'] == 'llm-test'
    assert row['cost_usd'] == Decimal('.1')
    assert not any(key in row for key in ('prompt', 'text', 'token', 'api_key'))


def test_failed_http_not_recorded_tracking_failure_does_not_break_completion(ledger, monkeypatch):
    monkeypatch.setattr(ai, '_connection_resolver', lambda _: {'token': 'test', 'base_url': 'https://openrouter.ai/api/v1'})
    with pytest.raises(Exception, match='HTTP 429'):
        ai.run_ai_complete({'prompt': 'test'}, {}, transport=lambda *a, **k: (429, b'{}'))
    assert ledger.scan()['Items'] == []
    monkeypatch.setattr(costs, '_table', lambda: (_ for _ in ()).throw(RuntimeError('db down')))
    response = {'choices': [{'message': {'content': 'hello'}}], 'usage': {'cost': .1}}
    assert ai.run_ai_complete({'prompt': 'test'}, {}, transport=lambda *a, **k: (200, json.dumps(response).encode()))['text'] == 'hello'


def test_decision_with_invalid_answer_still_billed(ledger):
    response = {'id': 'decision-1', 'usage': {'input_tokens': 2, 'output_tokens': 1}}
    result = ai.run_decisions(state='test', instructions='test', criteria={'yes': 'yes'}, credential={'api_key': 'test'}, transport=lambda *a, **k: (200, json.dumps(response).encode()), workflow_id='decision-flow')
    assert result['ok'] is False
    row = ledger.scan()['Items'][0]
    assert row['workflow_id'] == 'decision-flow'
    assert row['cost_source'] == 'unpriced'
    assert row['cost_usd'] is None


def test_api_surfaces_share_behavior_and_cli(monkeypatch):
    from src.dapier.api.admin import insights
    from src.dapier.api.agent import ops
    import importlib
    usage = importlib.import_module("dapier_cli.commands.usage")
    calls = []
    monkeypatch.setattr(costs, 'api_costs', lambda months, visible=None: (calls.append(months) or (200, {'requests': [], 'reported_usd': 0, 'estimated_usd': 0, 'unpriced_requests': 0})))
    monkeypatch.setattr(ops, 'require_operator', lambda *a: ('owner', None))
    monkeypatch.setattr(ops, '_visibility', lambda *a: None)
    event = {'queryStringParameters': {'months': '3'}}
    assert insights.costs(event)['statusCode'] == 200
    assert ops.costs_api(event)['statusCode'] == 200
    assert calls == ['3', '3']
    monkeypatch.setattr(usage.api, 'call', lambda base, method, path, **kw: calls.append((method, path)) or {})
    assert usage.costs('https://example.com', months=3) == 0
    assert calls[-1] == ('GET', '/api/agent/costs?months=3')


def test_months_validation(ledger):
    for months in (0, 25, 'bad'):
        assert costs.api_costs(months)[0] == 400


def test_metadata_failure_cannot_break_response(ledger, monkeypatch):
    monkeypatch.setattr(costs, 'cost_metadata', lambda *args: (_ for _ in ()).throw(ValueError('unknown provider usage shape')))
    result = costs.record_response(provider='anthropic', model='model', response={'id': 'unknown-usage', 'usage': {'cost': []}})
    assert result['cost_source'] == 'unpriced'
    assert ledger.scan()['Items'][0]['cost_usd'] is None


def test_paginated_reads_include_all_costs_and_mark_request_cap(monkeypatch):
    monkeypatch.setenv('PAID_API_USAGE_TABLE', 'paid-usage')
    class Pages:
        def query(self, **kwargs):
            if kwargs.get('ExclusiveStartKey'):
                return {'Items': [{'timestamp': '2026-10-13', 'cost_usd': Decimal('.02'), 'cost_source': 'estimate'}]}
            return {'Items': [{'timestamp': '2026-10-12', 'cost_usd': Decimal('.01'), 'cost_source': 'provider-reported'}], 'LastEvaluatedKey': {'month': '202610', 'request_key': 'one'}}
    monkeypatch.setattr(costs, '_table', lambda: Pages())
    status, result = costs.api_costs()
    assert status == 200
    assert result['reported_usd'] == Decimal('.01')
    assert result['estimated_usd'] == Decimal('.02')
    assert len(result['requests']) == 2
    assert result['truncated'] is False
    class Many:
        def query(self, **kwargs):
            return {'Items': [{'timestamp': '2026-10-12', 'cost_usd': None, 'cost_source': 'unpriced'}]*5001}
    monkeypatch.setattr(costs, '_table', lambda: Many())
    _, result = costs.api_costs()
    assert result['truncated'] is True
    assert len(result['requests']) == 5000
    assert result['unpriced_requests'] == 5000


def test_cost_ledger_permissions_cover_api_and_worker():
    import yaml
    from pathlib import Path
    class CloudFormationLoader(yaml.SafeLoader):
        pass
    def intrinsic(loader, tag, node):
        value = loader.construct_scalar(node) if isinstance(node, yaml.ScalarNode) else loader.construct_sequence(node, deep=True)
        return {tag: value}
    CloudFormationLoader.add_multi_constructor('!', intrinsic)
    template = yaml.load(Path('template.yaml').read_text(), Loader=CloudFormationLoader)
    resources = template['Resources']
    api_statements = [statement for policy in resources['ApiExecutionRole']['Properties']['Policies'] if 'PolicyDocument' in policy for statement in policy['PolicyDocument']['Statement']]
    assert any(statement.get('Action') == ['dynamodb:PutItem'] and statement.get('Resource') == {'GetAtt': 'PaidApiUsageTable.Arn'} for statement in api_statements)
    worker_policies = resources['WorkerFunction']['Properties']['Policies']
    assert {'Statement': {'Effect': 'Allow', 'Action': ['dynamodb:PutItem'], 'Resource': {'GetAtt': 'PaidApiUsageTable.Arn'}}} in worker_policies
    assert {'DynamoDBCrudPolicy': {'TableName': {'Ref': 'PaidApiUsageTable'}}} not in worker_policies
    assert template['Globals']['Function']['Environment']['Variables']['PAID_API_USAGE_TABLE'] == {'Ref': 'PaidApiUsageTable'}
