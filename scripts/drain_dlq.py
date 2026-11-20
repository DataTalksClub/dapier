"""List the event dead-letter queue and delete chosen events from it.

Run by the dispatchable dlq-drain workflow. Without --delete it only lists:
one DLQ_MESSAGE line per message with the event id (the suffix of the run
id, ``<workflow>:<event id>``), connector, event type, subject and receive
count, so each message can be matched to its run in history. --delete
removes only the messages whose event id is named; everything else stays.

The DLQ cannot be redriven to the event queue from CI: the queue policy's
DenyUntrustedPublishers boundary admits only Dapier's own roles and the
inbound SNS topic. Re-run a dead-lettered event through run replay
(`dapier runs replay`) and delete its DLQ copy here once that is green.

Grants: infra/dlq-drain-access.yaml (scoped to the dapier DLQ).
"""
import argparse
import json

import boto3

cf = boto3.client('cloudformation')
sqs = boto3.client('sqs')


def physical(logical):
    return cf.describe_stack_resource(StackName='dapier', LogicalResourceId=logical)['StackResourceDetail']['PhysicalResourceId']


def describe(body):
    """(event id, connector, event type, subject) of one queued message: a
    plain dapier event, or an SNS-delivered inbound email receipt."""
    try:
        payload = json.loads(body)
    except ValueError:
        return None, None, None, None
    if isinstance(payload, dict) and payload.get('Type') == 'Notification':
        try:
            payload = json.loads(payload.get('Message') or '{}')
        except ValueError:
            return None, 'sns', None, None
    if not isinstance(payload, dict):
        return None, None, None, None
    if payload.get('contract') == 'inbound-email':
        return payload.get('event_id'), 'email', 'message.received', payload.get('subject')
    data = payload.get('data') if isinstance(payload.get('data'), dict) else {}
    return payload.get('id'), payload.get('connector'), payload.get('event'), data.get('subject')


def drain(queue_url, delete, client=sqs):
    """Receive every visible message once; delete the named events. Returns
    the listed messages (dicts) and the event ids deleted."""
    seen, listed, deleted = set(), [], []
    while True:
        page = client.receive_message(
            QueueUrl=queue_url, MaxNumberOfMessages=10, WaitTimeSeconds=1,
            VisibilityTimeout=60, AttributeNames=['SentTimestamp', 'ApproximateReceiveCount'])
        fresh = [m for m in page.get('Messages', []) if m['MessageId'] not in seen]
        if not fresh:
            return listed, deleted
        for message in fresh:
            seen.add(message['MessageId'])
            event_id, connector, event_type, subject = describe(message.get('Body') or '')
            entry = {'message_id': message['MessageId'], 'event_id': event_id,
                     'connector': connector, 'event': event_type,
                     'subject': (subject or '')[:120],
                     'sent': (message.get('Attributes') or {}).get('SentTimestamp'),
                     'receives': (message.get('Attributes') or {}).get('ApproximateReceiveCount')}
            listed.append(entry)
            if event_id and event_id in delete:
                client.delete_message(QueueUrl=queue_url, ReceiptHandle=message['ReceiptHandle'])
                deleted.append(event_id)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--delete', nargs='*', default=[],
                        help='event ids whose DLQ messages to delete (default: list only)')
    args = parser.parse_args()
    queue_url = physical('DeadLetterQueue')
    listed, deleted = drain(queue_url, set(args.delete))
    for entry in listed:
        print('DLQ_MESSAGE', json.dumps(entry))
    print('DLQ_DELETED', json.dumps(deleted))
    missing = sorted(set(args.delete) - set(deleted))
    if missing:
        print('DLQ_NOT_FOUND', json.dumps(missing))
    remaining = sqs.get_queue_attributes(
        QueueUrl=queue_url, AttributeNames=['ApproximateNumberOfMessages'])['Attributes']
    print('DLQ_REMAINING', remaining['ApproximateNumberOfMessages'],
          '(eventually consistent; listed messages stay invisible for 60s)')


if __name__ == '__main__':
    main()
