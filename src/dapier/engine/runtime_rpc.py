"""Run identity-bound operations on the workflow worker via IAM, not HTTP."""
import json
import os

from ..http import _json_default


def call(operation, **payload):
    import boto3

    response = boto3.client('lambda').invoke(
        FunctionName=os.environ['WORKER_FUNCTION_ARN'],
        InvocationType='RequestResponse',
        Payload=json.dumps({'dapier_rpc': operation, **payload}, default=_json_default).encode(),
    )
    result = json.loads(response['Payload'].read())
    if response.get('FunctionError') or not isinstance(result, dict):
        raise ValueError('Worker could not complete the connection operation')
    if result.get('rpc_error'):
        raise ValueError(result['rpc_error'])
    return result


def dispatch(event):
    from ..connectors import registry

    operation = event.get('dapier_rpc')
    if operation == 'connection-test':
        connection = event['connection']
        test = registry.connection_test_for(connection['provider'])
        if test is None:
            raise ValueError('Provider has no connection test')
        return test.run(connection)
    if operation == 'connection-token':
        import boto3
        from ..connections import records, tokens
        table = boto3.resource('dynamodb').Table(os.environ['CONNECTIONS_TABLE'])
        connection = records.get_connection(table, event['connection_id'])
        if not connection or connection.get('provider') != 'llm':
            raise ValueError('LLM connection not found')
        token, info = tokens.get_access_token(connection)
        return {'token': token, 'info': info}
    if operation == 'action':
        action = event['action']
        if action.get('type') != 'ai_complete':
            raise ValueError('Unsupported worker test action')
        from ..connectors.ai import run_ai_complete
        return run_ai_complete(
            action, event['event'], steps=event.get('steps'),
            workflow_id=event.get('workflow_id') or 'connection-test',
            usage_context=event.get('usage_context') or 'connection-test')
    raise ValueError('Unknown worker operation')
