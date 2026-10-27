"""Idle polling never waits in Lambda or reads full task history."""
import pytest

from src.dapier import headless_worker, host_jobs
from tests.test_workers import lease_fixture
from tests.test_live_action_permissions import _resources, _api_role_statements


def test_deployed_index_projects_metadata_and_api_can_query_it():
    resources = _resources()
    index, = resources["HostTasksTable"]["Properties"]["GlobalSecondaryIndexes"]
    assert index["IndexName"] == host_jobs.STATUS_INDEX
    assert index["KeySchema"] == [{"AttributeName": "status", "KeyType": "HASH"}]
    assert index["Projection"]["ProjectionType"] == "INCLUDE"
    assert not {"prompt", "logs", "summary", "attachments"}.intersection(
        index["Projection"]["NonKeyAttributes"])
    assert any("dynamodb:Query" in s.get("Action", []) and
               "${HostTasksTable.Arn}/index/*" in s.get("Resource", [])
               for s in _api_role_statements(resources))


def test_idle_claim_uses_only_index_queries_and_nonblocking_sqs(monkeypatch):
    with lease_fixture() as (table, queue, url):
        def no_scan(**kwargs):
            pytest.fail("claim must never scan task history")
        monkeypatch.setattr(table, "scan", no_scan)
        calls = []
        monkeypatch.setattr(queue, "receive_message", lambda **kw: calls.append(kw) or {})
        assert host_jobs.claim("host", table_ref=table, queue_ref=queue,
                               queue_url=url)[1] == {"job": None}
        assert calls[0]["WaitTimeSeconds"] == 0


def test_paginated_index_claims_oldest_matching_task_and_rejects_stale_results(monkeypatch):
    with lease_fixture() as (table, queue, url):
        for task_id, created in (("new", 2), ("old", 1)):
            table.put_item(Item={"task_id": task_id, "kind": "agent", "engine": "codex",
                                 "status": "queued", "created_at": created,
                                 "prompt": "private prompt", "logs": {"stdout": "x" * 10000}})
        query = table.query
        pages = []
        def paginated(**kwargs):
            result = query(**kwargs, Limit=1)
            pages.append(result)
            return result
        monkeypatch.setattr(table, "query", paginated)
        meta = {"worker": {"worker_id": "w", "engine": "codex"}}
        kwargs = dict(table_ref=table, queue_ref=queue, queue_url=url)
        job = host_jobs.claim("host", meta, now=1000, **kwargs)[1]["job"]
        assert job["task_id"] == "old" and job["prompt"] == "private prompt"
        assert any(page.get("LastEvaluatedKey") for page in pages)
        assert all("prompt" not in row and "logs" not in row
                   for page in pages for row in page.get("Items", []))
        # Simulate an eventually consistent queued index entry after pickup.
        stale = {"task_id": "old", "kind": "agent", "engine": "codex",
                 "status": "queued", "created_at": 1}
        monkeypatch.setattr(table, "query", lambda **kw: {"Items": [stale]} if
                            kw["ExpressionAttributeValues"][":status"] == "queued" else {})
        assert host_jobs.claim("other", meta, now=1001, **kwargs)[1]["job"] is None
        # A stale expired index lease must not interrupt a refreshed table lease.
        stale.update(status="running", lease_until=999)
        monkeypatch.setattr(table, "query", lambda **kw: {"Items": [stale]} if
                            kw["ExpressionAttributeValues"][":status"] == "running" else {})
        assert host_jobs.claim("other", meta, now=1001, **kwargs)[1]["job"] is None
        row = table.get_item(Key={"task_id": "old"})["Item"]
        assert row["status"] == "running" and row["lease_owner"] == "host"
        # No task is lost after the index catches up.
        monkeypatch.setattr(table, "query", query)
        assert host_jobs.claim("other", meta, now=1002, **kwargs)[1]["job"]["task_id"] == "new"


@pytest.mark.parametrize("engine", ["claude", "codex"])
@pytest.mark.parametrize("request_seconds,expected_sleep", [(0.2, 9.8), (10.1, 0)])
def test_worker_paces_idle_calls_on_host(tmp_path, monkeypatch, engine, request_seconds, expected_sleep):
    ticks = iter([100, 100 + request_seconds, 110])
    monkeypatch.setattr(headless_worker.time, "monotonic", lambda: next(ticks))
    sleeps = []
    monkeypatch.setattr(headless_worker.time, "sleep", sleeps.append)
    class Api:
        count = 0
        def call(self, operation, body):
            self.count += 1
            if self.count == 2:
                raise RuntimeError("stop after one idle interval")
            return {"job": None}
    with pytest.raises(RuntimeError, match="stop after one idle interval"):
        headless_worker.serve(workspace_root=tmp_path, api=Api(), engine=engine)
    assert sleeps == pytest.approx([expected_sleep])


def test_competing_claim_cannot_overwrite_winner_and_notification_retry_survives(monkeypatch):
    with lease_fixture() as (table, queue, url):
        table.put_item(Item={"task_id": "race", "kind": "agent", "engine": "codex",
                             "status": "queued", "created_at": 1, "prompt": "p"})
        original = table.update_item
        raced = []
        def compete(**kwargs):
            if kwargs.get("ConditionExpression") == "#status = :queued" and not raced:
                raced.append(True)
                original(Key={"task_id": "race"},
                         UpdateExpression="SET #s = :s, lease_owner = :o, lease_until = :u",
                         ExpressionAttributeNames={"#s": "status"},
                         ExpressionAttributeValues={":s": "running", ":o": "winner", ":u": 2000})
            return original(**kwargs)
        monkeypatch.setattr(table, "update_item", compete)
        meta = {"worker": {"worker_id": "w", "engine": "codex"}}
        assert host_jobs.claim("loser", meta, table_ref=table, queue_ref=queue,
                               queue_url=url, now=1000)[1]["job"] is None
        assert table.get_item(Key={"task_id": "race"})["Item"]["lease_owner"] == "winner"
        table.put_item(Item={"task_id": "notify", "kind": "agent", "engine": "codex",
                             "status": "succeeded", "created_at": 2, "notify_to": "op@example.com",
                             "summary": "finished"})
        notified = []
        monkeypatch.setattr(host_jobs, "_notify", lambda table, task_id, row:
                            notified.append((task_id, row.get("summary"))))
        host_jobs.claim("host", meta, table_ref=table, queue_ref=queue, queue_url=url, now=1001)
        assert notified == [("notify", "finished")]
