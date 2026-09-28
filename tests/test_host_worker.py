"""Host worker agent kind: one session, resume, and a failed start left on the queue."""
import json
from pathlib import Path

from botocore.exceptions import ClientError

from src.dapier.host_worker import TaskBook, poll_once


class Table:
    def __init__(self):
        self.items = {}

    def put_item(self, Item, ConditionExpression=None):
        key = Item["task_id"]
        if ConditionExpression and key in self.items:
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": "exists"}},
                "PutItem",
            )
        self.items[key] = dict(Item)

    def get_item(self, Key):
        item = self.items.get(Key["task_id"])
        return {"Item": dict(item)} if item is not None else {}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeNames, ExpressionAttributeValues, **_):
        item = self.items[Key["task_id"]]
        for placeholder, attr in ExpressionAttributeNames.items():
            index = placeholder.replace("#k", "")
            item[attr] = ExpressionAttributeValues[f":v{index}"]


class Sessions:
    def __init__(self):
        self.started = []
        self.sent = []

    def start(self, **kwargs):
        self.started.append(kwargs)
        return type("S", (), {"id": "sess-1", "tag": kwargs["tag"]})()

    def status(self, selector):
        return {"phase": "waiting"}

    def send(self, selector, data):
        self.sent.append((selector, data))
        return len(data)


class Queue:
    def __init__(self, bodies):
        self.bodies = list(bodies)
        self.deleted = []
        self.kwargs = None

    def receive_message(self, **kwargs):
        self.kwargs = kwargs
        if not self.bodies:
            return {"Messages": []}
        body = self.bodies.pop(0)
        return {"Messages": [{"MessageId": "1", "ReceiptHandle": "rh", "Body": json.dumps(body)}]}

    def delete_message(self, **kwargs):
        self.deleted.append(kwargs["ReceiptHandle"])


def _message(tmp_path):
    return {
        "kind": "agent",
        "task_id": "agent:flow:event:wake",
        "engine": "claude",
        "workspace": str(tmp_path),
        "tag_prefix": "agent",
        "prompt": "do the thing",
    }


def test_one_start_then_send_and_a_duplicate_does_not_start_again(tmp_path):
    tasks = TaskBook(Table())
    sessions = Sessions()
    body = _message(tmp_path)
    queue = Queue([body, body])
    poll_once(queue, "https://queue.test/host", tasks=tasks, sessions=sessions, sleep=lambda _s: None)
    poll_once(queue, "https://queue.test/host", tasks=tasks, sessions=sessions, sleep=lambda _s: None)
    assert queue.kwargs["WaitTimeSeconds"] == 20
    assert len(sessions.started) == 1
    assert sessions.started[0]["fresh"] is True
    assert sessions.sent[0][1] == b"do the thing\n"
    assert queue.deleted == ["rh", "rh"]
    assert tasks.table.items[body["task_id"]]["status"] == "started"


def test_resume_sends_without_a_second_start(tmp_path):
    table = Table()
    body = _message(tmp_path)
    table.items[body["task_id"]] = {
        "task_id": body["task_id"], "status": "starting", "session_id": "sess-existing",
        "prompt": "do the thing",
    }
    sessions = Sessions()
    queue = Queue([body])
    poll_once(queue, "https://queue.test/host", tasks=TaskBook(table), sessions=sessions, sleep=lambda _s: None)
    assert sessions.started == []
    assert sessions.sent[0][0] == "sess-existing"
    assert queue.deleted == ["rh"]


def test_start_failure_leaves_the_message_and_stores_the_error(tmp_path):
    tasks = TaskBook(Table())
    sessions = Sessions()

    def boom(**_kwargs):
        raise RuntimeError("aplexer down")

    sessions.start = boom
    queue = Queue([_message(tmp_path)])
    poll_once(queue, "https://queue.test/host", tasks=tasks, sessions=sessions, sleep=lambda _s: None)
    assert queue.deleted == []
    assert "aplexer down" in tasks.table.items["agent:flow:event:wake"]["error"]
