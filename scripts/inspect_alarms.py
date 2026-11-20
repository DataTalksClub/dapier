"""Inspect the event-queue and operator alarms (read-only).

The deploy workflow and the dispatchable backup-inspection workflow run this
after the backup checks. It prints ALARMS_* evidence lines: each alarm's
current state, its recent state transitions (the verbatim "Threshold
Crossed" datapoints), the event queue's oldest-message age peaks, the
dead-letter queue depth, the telegram webhook-health alarm, and the
browser client-check failure counts the operator topic also pages on. An
alarm email says when a threshold was crossed; these lines say whether it
recovered and whether it came back.

Uses only the cloudformation/cloudwatch grants in
infra/backup-inspection-access.yaml (tests/test_backup.py keeps the two in
lockstep); a missing grant prints ALARMS_UNVERIFIED instead of failing.
"""
import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import ClientError

# Logical alarm ids in the dapier stack, plus alarms other stacks publish to
# the same operator topic by name.
STACK_ALARMS = ('EventQueueAgeAlarm', 'EventQueueDepthAlarm', 'EventDeadLetterAlarm',
                'TelegramWebhookUnhealthyAlarm')
NAMED_ALARMS = ('dataops-v1-client-check-failed',)
HISTORY_DAYS = 3


@contextmanager
def inspect(label):
    try:
        yield
    except ClientError as exc:
        print('ALARMS_UNVERIFIED', label, exc.response['Error']['Code'])


cf = boto3.client('cloudformation')
cw = boto3.client('cloudwatch')


def physical(logical):
    return cf.describe_stack_resource(StackName='dapier', LogicalResourceId=logical)['StackResourceDetail']['PhysicalResourceId']


def queue_name(logical):
    # An SQS queue's physical id is its URL; metrics key on the name.
    return physical(logical).rsplit('/', 1)[-1]


def peaks(namespace, metric, dimensions, statistic, period, floor, days=HISTORY_DAYS):
    """Datapoints at or above `floor` over the last `days`, oldest first.
    GetMetricStatistics caps one call at 1440 datapoints, so day windows."""
    now = datetime.now(timezone.utc)
    points = []
    for offset in range(days):
        end = now - timedelta(days=offset)
        points.extend(
            (str(point['Timestamp']), point[statistic])
            for point in cw.get_metric_statistics(
                Namespace=namespace, MetricName=metric, Dimensions=dimensions,
                StartTime=end - timedelta(days=1), EndTime=end, Period=period,
                Statistics=[statistic])['Datapoints']
            if point[statistic] >= floor)
    return sorted(points)


def history(name):
    since = datetime.now(timezone.utc) - timedelta(days=HISTORY_DAYS)
    items = []
    kwargs = {'AlarmName': name, 'HistoryItemType': 'StateUpdate',
              'StartDate': since, 'MaxRecords': 100}
    while True:
        page = cw.describe_alarm_history(**kwargs)
        items.extend(page.get('AlarmHistoryItems', []))
        if not page.get('NextToken'):
            break
        kwargs['NextToken'] = page['NextToken']
    return [{'timestamp': str(item.get('Timestamp')), 'summary': item.get('HistorySummary')}
            for item in sorted(items, key=lambda item: item.get('Timestamp'))]


def main():
    names = []
    for logical in STACK_ALARMS:
        with inspect(f'resolve {logical}'):
            names.append(physical(logical))
    names.extend(NAMED_ALARMS)
    with inspect('alarm-states'):
        found = cw.describe_alarms(AlarmNames=names)['MetricAlarms']
        print('ALARMS_STATE', json.dumps([
            {k: alarm.get(k) for k in ('AlarmName', 'StateValue', 'StateUpdatedTimestamp', 'StateReason')}
            for alarm in found], default=str))
        missing = sorted(set(names) - {alarm['AlarmName'] for alarm in found})
        if missing:
            print('ALARMS_NOT_VISIBLE', json.dumps(missing))
    for name in names:
        with inspect(f'history {name}'):
            print('ALARMS_HISTORY', json.dumps({'alarm': name, 'items': history(name)}))
    with inspect('event-queue-age'):
        # Five-minute maxima of the oldest message's age, only those past
        # five minutes: an empty list is a queue that never backed up.
        queue = [{'Name': 'QueueName', 'Value': queue_name('EventQueue')}]
        print('ALARMS_QUEUE_AGE', json.dumps({'over_300s': peaks(
            'AWS/SQS', 'ApproximateAgeOfOldestMessage', queue, 'Maximum', 300, 300)}))
    with inspect('dead-letter-depth'):
        dlq = [{'Name': 'QueueName', 'Value': queue_name('DeadLetterQueue')}]
        print('ALARMS_DLQ_VISIBLE', json.dumps({'hours_nonzero': peaks(
            'AWS/SQS', 'ApproximateNumberOfMessagesVisible', dlq, 'Maximum', 3600, 1)}))
    with inspect('client-check-failures'):
        print('ALARMS_CLIENT_CHECK_FAILED', json.dumps({'five_minute_sums': peaks(
            'DataOps/Client', 'ClientCheckFailed', [], 'Sum', 300, 1)}))


if __name__ == '__main__':
    main()
