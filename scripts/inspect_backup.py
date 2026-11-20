"""Inspect DynamoDB backup health (read-only unless --invoke).

The deploy workflow's "Inspect DynamoDB backup health" step and the
dispatchable backup-inspection workflow both run this script. It verifies
the daily snapshots with the grants in infra/backup-inspection-access.yaml
(alarm state, alarm history, error metrics,
run logs, table sizes, manifest days) and
prints BACKUP_* evidence lines; missing permissions are reported as
BACKUP_UNVERIFIED without hiding the deployment result. Every boto3 call
here must stay covered by that policy —
tests/test_backup.py::test_backup_inspection_grants_match_inspection_script
keeps script and grants in lockstep (it parses the boto3 calls made
below, so route new calls through a named client).

--invoke runs one real backup now (the same handler the daily schedule
calls; report failures, stay silent on success) so a fix can be verified
without waiting for the next 03:00Z run.
"""
import argparse
import base64
import json
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import boto3
import botocore.config
from botocore.exceptions import ClientError


@contextmanager
def inspect(label):
    try:
        yield
    except ClientError as exc:
        print('BACKUP_UNVERIFIED', label, exc.response['Error']['Code'])


cf = boto3.client('cloudformation')


def physical(logical):
    return cf.describe_stack_resource(StackName='dapier', LogicalResourceId=logical)['StackResourceDetail']['PhysicalResourceId']


def recent_log_events(logs, group, start_ms, pattern, keep=20):
    """Matching events oldest-first, all pages, only the newest `keep` kept."""
    events = []
    kwargs = {'logGroupName': group, 'startTime': start_ms, 'filterPattern': pattern, 'limit': 100}
    while True:
        page = logs.filter_log_events(**kwargs)
        events.extend(page.get('events', []))
        if not page.get('nextToken'):
            return events[-keep:]
        kwargs['nextToken'] = page['nextToken']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--invoke', action='store_true',
                        help='run one backup now and print its result')
    parser.add_argument('--log-days', type=int, default=3,
                        help='how many days of run logs to scan')
    args = parser.parse_args()

    fn = physical('BackupFunction')
    bucket = physical('BackupBucket')
    alarm = physical('BackupErrorAlarm')
    config = {}
    with inspect('config'):
        config = boto3.client('lambda').get_function_configuration(FunctionName=fn)
        print('BACKUP_CONFIG', json.dumps({k: config[k] for k in ('FunctionName', 'MemorySize', 'Timeout', 'LastModified', 'CodeSha256')}))
        assert config['MemorySize'] >= 1024
    cw = boto3.client('cloudwatch')
    with inspect('alarm'):
        states = cw.describe_alarms(AlarmNames=[alarm])['MetricAlarms']
        print('BACKUP_ALARM', json.dumps([{k: a[k] for k in ('AlarmName', 'StateValue', 'StateUpdatedTimestamp')} for a in states], default=str))
    with inspect('alarm-history'):
        # The verbatim transition reasons name the exact breaching datapoint
        # (e.g. "Threshold Crossed: 1 datapoint [1.0 (17/11/26 02:56:00)]")
        # that per-hour error buckets can only bracket. Newest first.
        items = []
        kwargs = {'AlarmName': alarm, 'HistoryItemType': 'Action', 'MaxResults': 10}
        while True:
            page = cw.describe_alarm_history(**kwargs)
            items.extend(page.get('AlarmHistoryItems', []))
            if not page.get('NextToken') or len(items) >= 10:
                break
            kwargs['NextToken'] = page['NextToken']
        entries = []
        for i in items:
            payload = i.get('HistoryPayload')
            if isinstance(payload, str):
                payload = json.loads(payload)
            entries.append({
                'timestamp': str(i['AlarmHistoryUpdatedTimestamp']),
                'summary': i['HistorySummary'],
                'reason': (payload or {}).get('stateChangeReason'),
            })
        print('BACKUP_ALARM_HISTORY', json.dumps({'items': entries}))
    with inspect('error-metrics'):
        # Every hour of the last four days in which the backup function
        # errored at all — absence after a scheduled run is the proof
        # that the run was green, not just that the alarm went quiet.
        points = sorted(
            (str(point['Timestamp']), point['Sum'])
            for point in cw.get_metric_statistics(
                Namespace='AWS/Lambda', MetricName='Errors',
                Dimensions=[{'Name': 'FunctionName', 'Value': fn}],
                StartTime=datetime.now(timezone.utc) - timedelta(days=4),
                EndTime=datetime.now(timezone.utc), Period=3600,
                Statistics=['Sum'])['Datapoints'])
        print('BACKUP_ERROR_METRICS', json.dumps({'hours_with_errors': points}))
        # Per-minute resolution for the hours that had errors: pins each
        # error to its invocation (the 2026-11-17 alarm datapoint cited
        # 02:56:00 while the timeouts' REPORT lines said 03:05–03:18 —
        # Lambda timestamps the invocation, not the timeout kill).
        minutes = sorted(
            (str(point['Timestamp']), point['Sum'])
            for point in cw.get_metric_statistics(
                Namespace='AWS/Lambda', MetricName='Errors',
                Dimensions=[{'Name': 'FunctionName', 'Value': fn}],
                StartTime=datetime.now(timezone.utc) - timedelta(days=4),
                EndTime=datetime.now(timezone.utc), Period=60,
                Statistics=['Sum'])['Datapoints']
            if point['Sum'])
        print('BACKUP_ERROR_MINUTES', json.dumps({'minutes_with_errors': minutes}))
    logs = boto3.client('logs')
    group = '/aws/lambda/' + fn
    start_ms = int((time.time() - args.log_days * 86400) * 1000)
    # Runtime reports carry the timeout/memory truth of each invocation
    # (a timeout-killed backup reports Status: timeout at the full limit),
    # the timeout lines name the kill itself, and the backup count lines
    # show which tables a run got through. Only aggregate lines are read
    # back — never table items.
    for pattern, label in (
            ('"REPORT"', 'reports'),
            ('"Task timed out"', 'timeouts'),
            ('"backup" "items" "across"', 'counts')):
        with inspect('logs-' + label):
            print('BACKUP_LOGS', json.dumps({'pattern': pattern, 'events': [
                {'timestamp': e['timestamp'], 'message': e['message']}
                for e in recent_log_events(logs, group, start_ms, pattern)]}))
    dynamo = boto3.client('dynamodb')
    names = sorted({v for k, v in config.get('Environment', {}).get('Variables', {}).items() if k.endswith('_TABLE') and k != 'TEAM_SECRETS_TABLE' and v})
    for name in names:
        with inspect('table-size ' + name):
            table = dynamo.describe_table(TableName=name)['Table']
            print('BACKUP_TABLE_SIZE', json.dumps({k: table[k] for k in ('TableName', 'ItemCount', 'TableSizeBytes')}))
    s3 = boto3.client('s3')
    now = datetime.now(timezone.utc)
    days = [(now - timedelta(days=offset)).strftime('%Y-%m-%d') for offset in range(5)]
    with inspect('recent-backups'):
        listed = s3.list_objects_v2(Bucket=bucket, Prefix='backups/').get('Contents', [])
        manifests = sorted(str(o['LastModified']) + ' ' + o['Key'] for o in listed
                           if o['Key'].endswith('/manifest.json'))
        print('BACKUP_RECENT_MANIFESTS', json.dumps({'recent': manifests[-7:], 'objects': len(listed)}))
    # Verify the newest day that has a snapshot: a deploy earlier in the
    # day runs before that day's 03:00Z backup, so "no manifest yet" for
    # today is normal and yesterday's snapshot is the verified one.
    verified = None
    with inspect('snapshot'):
        for day in days:
            key = f'backups/{day}/manifest.json'
            try:
                obj = s3.get_object(Bucket=bucket, Key=key)
            except ClientError as exc:
                if exc.response.get('Error', {}).get('Code') == 'NoSuchKey':
                    if day == days[0]:
                        print('BACKUP_NO_SNAPSHOT_YET', day)
                    continue
                raise
            manifest = json.loads(obj['Body'].read())
            print('BACKUP_MANIFEST', json.dumps({'path': f's3://{bucket}/{key}', 'lastModified': str(obj['LastModified']), 'date': manifest['date'], 'tables': manifest['tables']}))
            assert manifest['date'] == day
            if config:
                assert set(manifest['tables']) == set(names)
            for name, stats in manifest['tables'].items():
                assert 'error' not in stats, name
                head = s3.head_object(Bucket=bucket, Key=stats['key'])
                assert head['ContentLength'] == stats['bytes'], name
            print('BACKUP_SNAPSHOT_OBJECTS_VERIFIED', day, len(manifest['tables']))
            verified = day
            break
    if verified:
        print('BACKUP_SNAPSHOT_DAY_VERIFIED', verified)
        print('Scheduled success requires a manifest and successful runtime report for the latest backup day; alarm OK alone is insufficient.')
    if args.invoke:
        with inspect('invoke'):
            # A synchronous invoke holds the HTTP response open for the
            # whole run — botocore's default 60 s read timeout aborts the
            # client long before a 900 s backup finishes.
            invoke_client = boto3.client('lambda', config=botocore.config.Config(
                read_timeout=1000, retries={'max_attempts': 0}))
            result = invoke_client.invoke(FunctionName=fn, InvocationType='RequestResponse')
            payload = result.get('Payload').read()
            print('BACKUP_INVOKE', json.dumps({
                'status': result.get('StatusCode'),
                'functionError': result.get('FunctionError'),
                'manifest': json.loads(payload) if payload else None,
                'logTail': base64.b64decode(result.get('LogResult') or '').decode(errors='replace'),
            }, default=str))
            assert not result.get('FunctionError'), result.get('FunctionError')
            assert result.get('StatusCode') == 200


if __name__ == '__main__':
    main()
