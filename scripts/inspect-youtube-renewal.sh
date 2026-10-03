#!/usr/bin/env bash
# Read-only, bounded post-deploy evidence; never print raw provider/log payloads.
set -euo pipefail
uv run --with boto3 python - <<'PY'
import boto3,json,time
from botocore.exceptions import ClientError
proof={}
def read(name,fn):
 try:proof[name]=fn()
 except ClientError as e:proof[name]={'unverified':True,'awsErrorCode':e.response['Error']['Code']}
resources=boto3.client('cloudformation').describe_stack_resources(StackName='dapier')['StackResources']
physical={r['LogicalResourceId']:r['PhysicalResourceId'] for r in resources}
fn=physical['YouTubeSubscriptionFunction'];rule=physical['YouTubeSubscriptionFunctionRenewal']
proof['stackResources']={'function':fn,'rule':rule}
events=boto3.client('events')
read('rule',lambda:{k:v for k,v in events.describe_rule(Name=rule).items() if k in ('State','ScheduleExpression')})
read('target',lambda:{'matchesFunction':any(t['Arn'].endswith(':function:'+fn) for t in events.list_targets_by_rule(Rule=rule)['Targets'])})
logs=boto3.client('logs');start=int((time.time()-10*86400)*1000)
def recent():
 result=logs.filter_log_events(logGroupName='/aws/lambda/'+fn,startTime=start,limit=50,filterPattern='"AccessDeniedException"')
 return {'accessDeniedEventsInBoundedPage':len(result.get('events',[])),'morePages':bool(result.get('nextToken'))}
read('recentFailureLogs',recent)
print('YOUTUBE_RENEWAL_READONLY_PROOF='+json.dumps(proof,sort_keys=True))
PY
