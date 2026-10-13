"""Two failed SQS deliveries stay quiet; the third reports a failed run."""
import json
from unittest.mock import Mock

import pytest

from src.dapier.engine import worker


@pytest.mark.parametrize('scheduled,received,notify', [(0,1,False), (0,2,False),
    (0,3,True), (2,1,True), (1,1,False), (1,2,True)])
def test_notification_counts_scheduled_and_queue_attempts(monkeypatch, scheduled, received, notify):
    error = ValueError('provider failed')
    error.dapier_workflow = 'wf'
    monkeypatch.setattr(worker, 'execute', Mock(side_effect=error))
    monkeypatch.setattr(worker, '_schedule_retry', lambda *a: False)
    monkeypatch.setattr(worker, '_auto_pause_on_failure', lambda *a: False)
    monkeypatch.setattr(worker, 'inbox', Mock())
    notice = Mock()
    monkeypatch.setattr(worker, '_emit_failure_notice', notice)
    record = {'messageId': 'record', 'body': json.dumps({'id': 'event', 'connector': 'email', 'data': {}}),
        'attributes': {'ApproximateReceiveCount': str(received)},
        'messageAttributes': {'retry_attempt': {'stringValue': str(scheduled)}}}
    assert worker.handler({'Records': [record]}, None) == {'batchItemFailures': [{'itemIdentifier': 'record'}]}
    assert notice.called is notify


def test_success_on_third_delivery_sends_no_failure_email(monkeypatch):
    monkeypatch.setattr(worker, 'execute', lambda *a, **k: ['wf'])
    monkeypatch.setattr(worker, 'inbox', Mock())
    notice = Mock()
    monkeypatch.setattr(worker, '_emit_failure_notice', notice)
    record = {'messageId': 'record', 'body': json.dumps({'id': 'event', 'connector': 'email'}),
              'attributes': {'ApproximateReceiveCount': '3'}}
    assert worker.handler({'Records': [record]}, None) == {'batchItemFailures': []}
    notice.assert_not_called()


def test_missing_or_malformed_delivery_counter_is_first_attempt():
    assert worker._notification_attempt({}) == 1
    assert worker._notification_attempt({'attributes': {'ApproximateReceiveCount': 'invalid'}}) == 1
