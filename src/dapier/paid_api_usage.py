"""Paid API request ledger; unknown prices remain unknown, never zero."""
import logging
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

log = logging.getLogger(__name__)
# USD per million tokens, with dated primary-source provenance; populated below.
ANTHROPIC_RATES = {
    # Standard global, non-batch inference. Cache creation is unpriced below.
    'claude-sonnet-5-5': tuple(map(Decimal, ('2', '10', '0.10'))),
    'claude-opus-5-5': tuple(map(Decimal, ('4', '20', '0.20'))),
    'claude-sonnet-4-6': tuple(map(Decimal, ('3', '15', '0.30'))),
    'claude-sonnet-4-5': tuple(map(Decimal, ('3', '15', '0.30'))),
    'claude-haiku-4-5': tuple(map(Decimal, ('1', '5', '0.10'))),
}


def _table():
    import boto3
    return boto3.resource('dynamodb').Table(os.environ['PAID_API_USAGE_TABLE'])


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and result >= 0 else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def cost_metadata(provider, model, usage):
    reported = _number(usage.get('cost'))
    if reported is not None:
        return {'cost_usd': reported, 'cost_source': 'provider-reported', 'currency': 'USD'}
    # Dated model versions have the same tariff as the named generation.
    normalized = model
    if len(model) > 9 and model[-9] == '-' and model[-8:].isdigit():
        normalized = model[:-9]
    rates = ANTHROPIC_RATES.get(normalized) if provider == 'anthropic' else None
    if usage.get('server_tool_use') or usage.get('service_tier') not in (None, 'standard'):
        rates = None
    incoming = _number(usage.get('prompt_tokens', usage.get('input_tokens')))
    outgoing = _number(usage.get('completion_tokens', usage.get('output_tokens')))
    if rates and incoming is not None and outgoing is not None:
        details = usage.get('prompt_tokens_details')
        details = details if isinstance(details, dict) else {}
        cached = _number(details.get('cached_tokens', usage.get('cache_read_input_tokens', 0)))
        created = _number(usage.get('cache_creation_input_tokens', 0))
        if cached is not None and created == 0 and ('prompt_tokens' not in usage or cached <= incoming):
            # Native Anthropic input_tokens excludes cache reads; the compatibility
            # endpoint's prompt_tokens includes them.
            uncached = incoming-cached if 'prompt_tokens' in usage else incoming
            cost = (uncached*rates[0] + cached*rates[2] + outgoing*rates[1])/Decimal(1000000)
            return {'cost_usd': cost, 'cost_source': 'estimate', 'currency': 'USD',
                    'pricing_url': 'https://platform.claude.com/docs/en/about-claude/pricing',
                    'pricing_checked_at': '2026-10-13'}
    return {'cost_usd': None, 'cost_source': 'unpriced', 'currency': 'USD'}


def record_response(*, provider, model, response, connection_id='', workflow_id='', context='workflow', service='llm', now=None):
    """Best effort: record billed HTTP successes, including invalid output.

    A repeated provider request ID is recorded once. Retried requests with new
    IDs are distinct charges. No prompts, credentials, or replies are persisted.
    """
    usage = response.get('usage') if isinstance(response.get('usage'), dict) else {}
    try:
        metadata = cost_metadata(provider, model, usage)
    except Exception:
        metadata = {'cost_usd': None, 'cost_source': 'unpriced', 'currency': 'USD'}
    if not os.environ.get('PAID_API_USAGE_TABLE'):
        return metadata
    try:
        now = now or datetime.now(timezone.utc)
        request_id = str(response.get('id') or uuid.uuid4())
        row = {'month': now.strftime('%Y%m'), 'request_key': provider+':'+request_id,
               'request_id': request_id, 'provider_request_id': bool(response.get('id')),
               'timestamp': now.isoformat(), 'service': service, 'provider': provider,
               'model': model, 'connection_id': connection_id, 'workflow_id': workflow_id,
               'context': 'connection-test' if workflow_id == 'connection-test' else context,
               'input_tokens': _number(usage.get('prompt_tokens', usage.get('input_tokens'))),
               'output_tokens': _number(usage.get('completion_tokens', usage.get('output_tokens'))),
               **metadata}
        _table().put_item(Item=row, ConditionExpression='attribute_not_exists(request_key)')
    except Exception as exc:
        if getattr(exc, 'response', {}).get('Error', {}).get('Code') != 'ConditionalCheckFailedException':
            log.warning('Paid API usage recording failed: %s', type(exc).__name__)
    return metadata


def api_costs(months=1, now=None, visible=None):
    from boto3.dynamodb.conditions import Key
    from .auth import visibility
    try:
        months = int(months)
        if not 1 <= months <= 24:
            raise ValueError()
    except (ValueError, TypeError):
        return 400, {'error': 'months must be between 1 and 24'}
    if not os.environ.get('PAID_API_USAGE_TABLE'):
        return 200, {'requests': [], 'reported_usd': 0, 'estimated_usd': 0, 'unpriced_requests': 0,
                     'truncated': False, 'tracking_enabled': False, 'months': months}
    now = now or datetime.now(timezone.utc)
    year, month = now.year, now.month
    rows, truncated = [], False
    owners = visibility.owners_for(visible)
    for _ in range(months):
        kwargs = {'KeyConditionExpression': Key('month').eq(f'{year:04d}{month:02d}')}
        while True:
            result = _table().query(**kwargs)
            rows.extend(row for row in result.get('Items', [])
                        if visible is None or (row.get('workflow_id') and visible.workflow_visible(row['workflow_id'], owners)))
            if len(rows) >= 5000:
                rows = rows[:5000]
                truncated = True
                break
            if not result.get('LastEvaluatedKey'):
                break
            kwargs['ExclusiveStartKey'] = result['LastEvaluatedKey']
        if truncated:
            break
        month -= 1
        if month == 0:
            year, month = year-1, 12
    rows.sort(key=lambda row: row.get('timestamp', ''), reverse=True)
    return 200, {'requests': rows, 'months': months, 'tracking_enabled': True, 'truncated': truncated,
                 'reported_usd': sum((row.get('cost_usd') or Decimal(0)) for row in rows if row.get('cost_source') == 'provider-reported'),
                 'estimated_usd': sum((row.get('cost_usd') or Decimal(0)) for row in rows if row.get('cost_source') == 'estimate'),
                 'unpriced_requests': sum(row.get('cost_source') == 'unpriced' for row in rows)}
