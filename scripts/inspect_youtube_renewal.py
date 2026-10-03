"""Bounded read-only renewal evidence; access denial is explicitly unverified."""
import json
import time
import boto3
from botocore.exceptions import ClientError


def inspect(cf, events, logs, *, now=None):
    proof = {}
    # DescribeStackResources truncates large stacks; resolve exact resources.
    def physical(logical):
        return cf.describe_stack_resource(StackName='dapier', LogicalResourceId=logical)['StackResourceDetail']['PhysicalResourceId']
    fn = physical('YouTubeSubscriptionFunction')
    rule = physical('YouTubeSubscriptionFunctionRenewal')
    proof['stackResources'] = {'function': fn, 'rule': rule}

    def read(name, callback):
        try:
            proof[name] = callback()
        except ClientError as error:
            code = error.response['Error']['Code']
            if code not in ('AccessDenied', 'AccessDeniedException', 'UnauthorizedOperation'):
                raise RuntimeError(f'{name}: AWS {code}') from None
            proof[name] = {'unverified': True, 'awsErrorCode': code}

    read('rule', lambda: {k: v for k, v in events.describe_rule(Name=rule).items() if k in ('State', 'ScheduleExpression')})
    read('target', lambda: {'matchesFunction': any(t['Arn'].endswith(':function:' + fn) for t in events.list_targets_by_rule(Rule=rule)['Targets'])})
    if not proof['rule'].get('unverified') and proof['rule'] != {'State': 'ENABLED', 'ScheduleExpression': 'rate(4 days)'}:
        raise RuntimeError('verified renewal rule configuration is incorrect')
    if not proof['target'].get('unverified') and not proof['target']['matchesFunction']:
        raise RuntimeError('verified renewal target does not match the function')
    proof['ruleAndTargetVerified'] = not any(proof[key].get('unverified') for key in ('rule', 'target'))
    start = int(((time.time() if now is None else now) - 10 * 86400) * 1000)

    def recent():
        result = logs.filter_log_events(logGroupName='/aws/lambda/' + fn, startTime=start, limit=50, filterPattern='"AccessDeniedException"')
        return {'accessDeniedEventsInBoundedPage': len(result.get('events', [])), 'morePages': bool(result.get('nextToken'))}
    read('recentFailureLogs', recent)
    # Read evidence alone does not prove a successful scheduled execution.
    proof['automaticSuccessfulInvocationVerified'] = False
    return proof


if __name__ == '__main__':
    print('YOUTUBE_RENEWAL_READONLY_PROOF=' + json.dumps(inspect(boto3.client('cloudformation'), boto3.client('events'), boto3.client('logs')), sort_keys=True))
