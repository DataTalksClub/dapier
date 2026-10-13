"""Push and backup polls must share atomic step leases for one video."""
import time

import boto3
import pytest
from moto import mock_aws

from plugins.google.connector import youtube  # noqa: F401 (registration)
from src.dapier.connectors.ingress import processing_id
from src.dapier.engine import worker
from src.dapier.triggers.poll_triggers import event_for


def push(video_id="video-1"):
    return {"id": f"youtube:{video_id}", "connector": "youtube",
            "event": "video.published", "data": {"video_id": video_id}}


def poll(name="youtube-uploads", video_id="video-1"):
    return event_for({"poll_id": name, "source": "youtube.videos", "id_path": "id"},
                     {"id": video_id, "video_id": video_id, "title": "New title"})


@pytest.fixture
def executions(monkeypatch):
    monkeypatch.setenv("EXECUTIONS_TABLE", "youtube-executions")
    monkeypatch.delenv("TASK_USAGE_TABLE", raising=False)
    with mock_aws():
        yield boto3.resource("dynamodb").create_table(
            TableName="youtube-executions",
            KeySchema=[{"AttributeName": "execution_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "execution_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST")


@pytest.mark.parametrize("first,second", [(push(), poll()), (poll(), push()),
                                         (poll(), poll("another-poll"))])
def test_completed_video_is_skipped_across_sources(executions, first, second):
    assert first["id"] != second["id"]
    assert worker._is_pending("announce", "slack", first)
    worker._mark_completed("announce", "slack", first, output={"ts": "123"})

    assert not worker._is_pending("announce", "slack", second)
    records = executions.scan()["Items"]
    assert len(records) == 1
    assert records[0]["output"] == {"ts": "123"}
    assert records[0]["expires_at"] > time.time() + 89 * 86400


def test_duplicate_in_flight_waits_and_failed_step_can_retry(executions):
    assert worker._is_pending("announce", "slack", push())
    with pytest.raises(worker.LeaseBusy):
        worker._is_pending("announce", "slack", poll())
    worker._release_action("announce", "slack", push(), RuntimeError("Slack failed"))
    assert worker._is_pending("announce", "slack", poll())


def test_video_identity_stays_scoped_to_workflow_and_step(executions):
    assert worker._is_pending("announce", "slack", push())
    worker._mark_completed("announce", "slack", push())
    assert worker._is_pending("another-workflow", "slack", poll())
    assert worker._is_pending("announce", "email", poll())
    assert worker._is_pending("announce", "slack", poll(video_id="video-2"))


@pytest.mark.parametrize("event_id", ["replay-123", "inbox-replay-123"])
def test_explicit_replay_can_run_again(executions, event_id):
    assert worker._is_pending("announce", "slack", push())
    worker._mark_completed("announce", "slack", push())
    assert worker._is_pending("announce", "slack", {**push(), "id": event_id})


def test_existing_websub_execution_record_blocks_poll(executions):
    executions.put_item(Item={"execution_id": "announce:slack:youtube:video-1",
                              "status": "completed"})
    assert not worker._is_pending("announce", "slack", poll())


def test_other_events_and_missing_video_ids_keep_delivery_identity():
    assert processing_id({**push(), "data": {}}) == "youtube:video-1"
    assert processing_id({**poll(), "event": "video.updated"}) == poll()["id"]
    assert processing_id({**poll(), "connector": "other"}) == poll()["id"]
