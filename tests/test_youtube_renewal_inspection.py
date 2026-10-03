import pytest
from botocore.exceptions import ClientError
from scripts.inspect_youtube_renewal import inspect

class Stack:
    def describe_stack_resource(self, *, StackName, LogicalResourceId):
        assert StackName == 'dapier'
        return {'StackResourceDetail': {'PhysicalResourceId': {'YouTubeSubscriptionFunction': 'renewal-function', 'YouTubeSubscriptionFunctionRenewal': 'renewal-rule'}[LogicalResourceId]}}

class Providers:
    state = 'ENABLED'
    target = 'renewal-function'
    def describe_rule(self, **kwargs):
        return {'State': self.state, 'ScheduleExpression': 'rate(4 days)'}
    def list_targets_by_rule(self, **kwargs):
        return {'Targets': [{'Arn': 'arn:aws:lambda:eu-west-1:123:function:' + self.target}]}
    def filter_log_events(self, **kwargs):
        return {'events': []}

def test_exact_stack_resources_and_verified_rule_do_not_claim_invocation():
    p = Providers()
    result = inspect(Stack(), p, p, now=1000000)
    assert result['ruleAndTargetVerified'] is True
    assert result['automaticSuccessfulInvocationVerified'] is False

@pytest.mark.parametrize('field,value', [('state', 'DISABLED'), ('target', 'wrong-function')])
def test_actual_configuration_failures_fail_inspection(field, value):
    p = Providers()
    setattr(p, field, value)
    with pytest.raises(RuntimeError, match='verified renewal'):
        inspect(Stack(), p, p)

def test_access_denied_is_explicitly_unverified_without_raw_message():
    class Denied:
        def __getattr__(self, name):
            def deny(**kwargs):
                raise ClientError({'Error': {'Code': 'AccessDeniedException', 'Message': 'private-detail'}}, name)
            return deny
    result = inspect(Stack(), Denied(), Denied())
    assert result['ruleAndTargetVerified'] is False
    assert result['rule'] == {'unverified': True, 'awsErrorCode': 'AccessDeniedException'}
    assert 'private-detail' not in str(result)
