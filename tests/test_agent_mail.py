"""Agent mail: shared senders, mailbox rules, ingress routing, and the consumer."""
import json
import os
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

from botocore.exceptions import ClientError

from src.dapier.agent_mail import work
from src.dapier.api import agent
from src.dapier.api.admin import routes
from src.dapier.triggers import agent_mailboxes, email_triggers
from src.dapier.triggers.intake import email_ingress


SEED = ["alexey.s.grigoriev@gmail.com", "alexey@datatalks.club"]


class Table:
    def __init__(self):
        self.items = {}

    def _key(self, mapping):
        for name in ("task_id", "name", "id"):
            if name in mapping:
                return mapping[name]
        raise KeyError(mapping)

    def put_item(self, Item, ConditionExpression=None):
        key = self._key(Item)
        if ConditionExpression and key in self.items:
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": "exists"}},
                "PutItem",
            )
        self.items[key] = dict(Item)

    def get_item(self, Key):
        item = self.items.get(self._key(Key))
        return {"Item": dict(item)} if item is not None else {}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeNames, ExpressionAttributeValues, **_):
        item = self.items[self._key(Key)]
        for placeholder, attr in ExpressionAttributeNames.items():
            index = placeholder.replace("#k", "")
            item[attr] = ExpressionAttributeValues[f":v{index}"]

    def scan(self, **_):
        return {"Items": [dict(item) for item in self.items.values()]}

    def delete_item(self, Key):
        self.items.pop(self._key(Key), None)


def _env(monkeypatch):
    monkeypatch.setenv("AGENT_MAILBOXES_TABLE", "mailboxes")
    monkeypatch.setenv("AGENT_FROM_TABLE", "senders")
    monkeypatch.setenv("AGENT_TASKS_TABLE", "tasks")
    monkeypatch.setenv("TRIGGER_EMAIL_DOMAIN", "dtcdev.click")


def _tables(monkeypatch):
    _env(monkeypatch)
    mailboxes = Table()
    senders = Table()
    tasks = Table()
    monkeypatch.setattr(agent_mailboxes, "mailbox_table", lambda table_ref=None: table_ref or mailboxes)
    monkeypatch.setattr(agent_mailboxes, "from_table", lambda table_ref=None: table_ref or senders)
    monkeypatch.setattr(agent_mailboxes, "tasks_table", lambda table_ref=None: table_ref or tasks)
    return mailboxes, senders, tasks


def _request(method, body=None, query=None):
    return {
        "requestContext": {"http": {"method": method}},
        "queryStringParameters": query,
        "body": json.dumps(body) if body is not None else None,
    }


def test_sender_list_seeds_adds_removes_and_rejects_a_duplicate(monkeypatch):
    _tables(monkeypatch)
    listed = json.loads(routes.agent_from_list(_request("GET"))["body"])
    assert listed["addresses"] == SEED

    added = json.loads(routes.agent_from_add(_request("POST", {"address": "Ada <ada@example.com>"}), "op")["body"])
    assert added["added"] is True
    assert added["addresses"][-1] == "ada@example.com"

    again = json.loads(routes.agent_from_add(_request("POST", {"address": "ada@example.com"}), "op")["body"])
    assert again["added"] is False
    assert again["addresses"].count("ada@example.com") == 1

    wildcard = routes.agent_from_add(_request("POST", {"address": "*@datatalks.club"}), "op")
    assert wildcard["statusCode"] == 400

    for address in list(again["addresses"]):
        routes.agent_from_remove(_request("DELETE", query={"address": address}), "op")
    emptied = json.loads(routes.agent_from_list(_request("GET"))["body"])
    assert emptied["addresses"] == []


def test_display_name_matches_the_bare_seed_address():
    header = "Alexey Grigorev <Alexey.S.Grigoriev@gmail.com>"
    assert work.sender_allowed(header, SEED)
    assert not work.sender_allowed("other@gmail.com", SEED)
    assert not work.sender_allowed("alexey@datatalks.club", [])


def test_unknown_rule_operator_is_rejected_and_a_subject_rule_is_stored(monkeypatch):
    _tables(monkeypatch)
    routes.agent_mailbox_save(_request("PUT", {"workspace": "/work", "engine": "claude"}), "op")
    bad = routes.agent_rule_add(_request("POST", {"field": "subject", "operator": "bogus", "value": "x"}), "op")
    assert bad["statusCode"] == 400
    assert "unknown operator" in json.loads(bad["body"])["error"]
    field = routes.agent_rule_add(_request("POST", {"field": "from", "operator": "equals", "value": "a@b.co"}), "op")
    assert field["statusCode"] == 400
    saved = json.loads(routes.agent_rule_add(
        _request("POST", {"field": "subject", "operator": "contains", "value": "invoice"}), "op")["body"])
    assert saved["rule"]["field"] == "subject"
    updated = json.loads(routes.agent_mailbox_save(
        _request("PUT", {"workspace": "/elsewhere", "rules": []}), "op")["body"])
    assert updated["mailbox"]["workspace"] == "/elsewhere"
    assert updated["mailbox"]["rules"] == [saved["rule"]]


def test_agent_local_part_is_exclusive_with_email_triggers_and_yaml(monkeypatch):
    mailboxes, _senders, _tasks = _tables(monkeypatch)
    monkeypatch.setenv("EMAIL_TRIGGERS_TABLE", "triggers")
    triggers = Table()
    triggers.put_item(Item={"name": "agent", "address": "agent@dtcdev.click", "actions": [], "flow": ""})
    with patch.object(email_triggers, "get_table", return_value=triggers):
        response = routes.agent_mailbox_save(_request("PUT", {"workspace": "/work"}), "op")
    assert response["statusCode"] == 400
    assert "email trigger" in json.loads(response["body"])["error"]

    mailboxes.put_item(Item={"name": "agent", "enabled": True, "rules": [], "workspace": "/work"})
    with patch.object(email_triggers, "get_table", return_value=Table()), \
            patch.dict(os.environ, {"EMAIL_TRIGGERS_TABLE": "triggers"}):
        try:
            email_triggers.build_item(
                {"name": "agent", "actions": [{"type": "webhook", "url": "https://example.test"}]}, "op")
        except email_triggers.TriggerError as exc:
            assert "agent mailbox" in str(exc)
        else:
            raise AssertionError("email trigger claimed the agent mailbox local-part")


def test_email_trigger_cannot_take_the_agent_mailbox(monkeypatch):
    _tables(monkeypatch)
    monkeypatch.setattr(agent_mailboxes, "peek_mailbox", lambda name, table_ref=None: {"name": "agent"} if name == "agent" else None)
    try:
        email_triggers.build_item(
            {"name": "agent", "actions": [{"type": "webhook", "url": "https://example.test"}]}, "op")
    except email_triggers.TriggerError as exc:
        assert "agent mailbox" in str(exc)
    else:
        raise AssertionError("email trigger claimed the agent local-part")


def test_yaml_route_blocks_the_mailbox(monkeypatch):
    _tables(monkeypatch)
    claimed = {"trigger": {"connector": "email", "event": "message.received",
                           "filters": {"route": {"equals": "agent"}}}}
    with patch("src.dapier.engine.workflows", return_value=[claimed]):
        response = routes.agent_mailbox_save(_request("PUT", {"workspace": "/work"}), "op")
    assert response["statusCode"] == 400
    assert "YAML" in json.loads(response["body"])["error"]


def _mime(to, sender="Alexey <alexey@datatalks.club>", subject="Please invoice this",
          text="do the thing", filename=None, payload=b"file-bytes"):
    message = EmailMessage()
    message["From"] = sender
    message["To"] = to
    message["Subject"] = subject
    message["Message-ID"] = "<m-1@example.test>"
    if text is None:
        message.add_alternative("<p>html only</p>", subtype="html")
    else:
        message.set_content(text)
    if filename:
        message.add_attachment(payload, maintype="application", subtype="octet-stream", filename=filename)
    return message.as_bytes()


def _deliver(monkeypatch, raw, peek):
    s3 = type("S3", (), {})()
    s3.get_object = lambda **_kwargs: {"Body": type("B", (), {"read": lambda self: raw})()}
    sent = []

    class Queue:
        def send_message(self, **kwargs):
            sent.append(kwargs)

    monkeypatch.setattr(email_ingress, "s3", s3)
    monkeypatch.setattr(email_ingress, "queue", Queue())
    monkeypatch.setattr(agent_mailboxes, "peek_mailbox", peek)
    monkeypatch.setenv("EVENT_QUEUE_URL", "https://queue.test/events")
    monkeypatch.setenv("AGENT_TASK_QUEUE_URL", "https://queue.test/agent")
    monkeypatch.setenv("AGENT_MAILBOXES_TABLE", "mailboxes")
    email_ingress.handler({"Records": [
        {"s3": {"bucket": {"name": "mail"}, "object": {"key": "raw/m-1"}}}]}, None)
    return sent


def test_enabled_agent_mail_goes_only_to_the_agent_queue(monkeypatch):
    sent = _deliver(monkeypatch, _mime("Agent <agent@dtcdev.click>"),
                    lambda name, table_ref=None: {"name": name, "enabled": True})
    assert len(sent) == 1
    assert sent[0]["QueueUrl"] == "https://queue.test/agent"
    body = json.loads(sent[0]["MessageBody"])
    assert body["mailbox"] == "agent"
    assert "connector" not in body
    assert "aplexer" not in Path("src/dapier/engine/worker.py").read_text()


def test_other_recipients_and_a_disabled_mailbox_stay_on_the_event_queue(monkeypatch):
    sent = _deliver(monkeypatch, _mime("todo@dtcdev.click"),
                    lambda name, table_ref=None: None)
    assert sent[0]["QueueUrl"] == "https://queue.test/events"
    assert json.loads(sent[0]["MessageBody"])["connector"] == "email"

    sent = _deliver(monkeypatch, _mime("agent@dtcdev.click"),
                    lambda name, table_ref=None: {"name": "agent", "enabled": False})
    assert sent[0]["QueueUrl"] == "https://queue.test/events"


class Sessions:
    def __init__(self, phase="waiting"):
        self.started = []
        self.sent = []
        self.phase = phase

    def start(self, **kwargs):
        self.started.append(kwargs)
        return type("Session", (), {"id": "sess-1", "tag": kwargs["tag"]})()

    def status(self, selector):
        return {"phase": self.phase}

    def send(self, selector, data):
        self.sent.append((selector, data))
        return len(data)


def _consumer(monkeypatch, tmp_path, raw, senders=None, rules=None, sessions=None):
    mailboxes, sender_table, task_table = _tables(monkeypatch)
    workspace = tmp_path / "repo"
    workspace.mkdir(exist_ok=True)
    mailbox = {
        "name": "agent", "enabled": True, "engine": "claude", "profile": "",
        "workspace": str(workspace), "tag_prefix": "mail",
        "instructions": "Be brief.", "rules": rules or [],
    }
    mailboxes.put_item(Item=mailbox)
    sender_table.put_item(Item={"id": "from-allow", "addresses": list(SEED if senders is None else senders)})
    (tmp_path / "raw").write_bytes(raw)

    def open_object(bucket, key):
        return raw

    sessions = sessions or Sessions()
    tasks = work.TaskBook(task_table)
    body = {
        "mailbox": "agent",
        "message_id": "<m-1@example.test>",
        "from": "Alexey <alexey@datatalks.club>",
        "to": "agent@dtcdev.click",
        "subject": "Please invoice this",
        "s3": {"bucket": "mail", "key": "raw/m-1"},
    }
    return tasks, sessions, body, open_object, workspace


def _deliver_one(tasks, sessions, body, open_object):
    return work.handle_delivery(
        body,
        load_mailbox=lambda name: tasks.table.items.get("agent") if False else agent_mailboxes.peek_mailbox(name),
        load_senders=lambda: agent_mailboxes.api_from_list()[1]["addresses"],
        open_object=open_object,
        tasks=tasks,
        sessions=sessions,
        sleep=lambda _seconds: None,
        ready_timeout=5,
    )


def test_a_match_starts_one_session_and_a_duplicate_does_not(monkeypatch, tmp_path):
    raw = _mime("agent@dtcdev.click", filename="notes.txt", payload=b"abc")
    tasks, sessions, body, open_object, workspace = _consumer(monkeypatch, tmp_path, raw)
    # peek reads the mailbox table, which the consumer stub returns.
    assert _deliver_one(tasks, sessions, body, open_object) == "ack"
    assert _deliver_one(tasks, sessions, body, open_object) == "ack"
    assert len(sessions.started) == 1
    assert sessions.started[0]["fresh"] is True
    assert sessions.started[0]["engine"] == "claude"
    prompt = sessions.sent[0][1].decode()
    assert "Be brief." in prompt
    assert "From: Alexey <alexey@datatalks.club>" in prompt
    assert "Subject: Please invoice this" in prompt
    assert "notes.txt" in prompt
    assert "do the thing" in prompt
    assert sessions.sent[0][0] == "sess-1"
    row = tasks.table.items["email:<m-1@example.test>"]
    assert row["status"] == "started"
    assert row["session_id"] == "sess-1"
    assert (workspace / ".dapier-mail").exists()


def test_ignored_sender_and_subject_miss_do_not_start(monkeypatch, tmp_path):
    raw = _mime("agent@dtcdev.click", sender="stranger@example.com")
    tasks, sessions, body, open_object, _workspace = _consumer(monkeypatch, tmp_path, raw)
    body["from"] = "stranger@example.com"
    assert _deliver_one(tasks, sessions, body, open_object) == "ack"
    assert sessions.started == []
    assert tasks.table.items["email:<m-1@example.test>"]["status"] == "ignored"

    raw = _mime("agent@dtcdev.click", subject="hello", text="nope")
    tasks, sessions, body, open_object, _workspace = _consumer(
        monkeypatch, tmp_path, raw,
        rules=[{"id": "r1", "field": "subject", "operator": "contains", "value": "invoice"}],
    )
    body["subject"] = "hello"
    body["message_id"] = "<m-2@example.test>"
    assert _deliver_one(tasks, sessions, body, open_object) == "ack"
    assert sessions.started == []
    assert tasks.table.items["email:<m-2@example.test>"]["status"] == "ignored"


def test_body_rule_reads_the_stored_mime(monkeypatch, tmp_path):
    raw = _mime("agent@dtcdev.click", text="nothing to see")
    tasks, sessions, body, open_object, _workspace = _consumer(
        monkeypatch, tmp_path, raw,
        rules=[{"id": "r1", "field": "body", "operator": "contains", "value": "secret"}],
    )
    assert _deliver_one(tasks, sessions, body, open_object) == "ack"
    assert sessions.started == []

    raw = _mime("agent@dtcdev.click", text="the secret is here", filename=None)
    tasks, sessions, body, open_object, _workspace = _consumer(
        monkeypatch, tmp_path, raw,
        rules=[{"id": "r1", "field": "body", "operator": "contains", "value": "secret"}],
    )
    body["message_id"] = "<m-body@example.test>"
    assert _deliver_one(tasks, sessions, body, open_object) == "ack"
    assert len(sessions.started) == 1


def test_html_only_prompt_names_the_raw_object(monkeypatch, tmp_path):
    raw = _mime("agent@dtcdev.click", text=None)
    tasks, sessions, body, open_object, _workspace = _consumer(monkeypatch, tmp_path, raw)
    assert _deliver_one(tasks, sessions, body, open_object) == "ack"
    prompt = sessions.sent[0][1].decode()
    assert "no plain-text body" in prompt
    assert "s3://mail/raw/m-1" in prompt
    assert "Attachments: none" in prompt


def test_resume_sends_without_a_second_start(monkeypatch, tmp_path):
    raw = _mime("agent@dtcdev.click")
    tasks, sessions, body, open_object, _workspace = _consumer(monkeypatch, tmp_path, raw)
    tasks.table.items["email:<m-1@example.test>"] = {
        "task_id": "email:<m-1@example.test>",
        "status": "starting",
        "session_id": "sess-existing",
        "mailbox": "agent",
    }
    assert _deliver_one(tasks, sessions, body, open_object) == "ack"
    assert sessions.started == []
    assert sessions.sent[0][0] == "sess-existing"
    assert tasks.table.items["email:<m-1@example.test>"]["status"] == "started"


def test_a_start_failure_leaves_the_message_and_records_the_error(monkeypatch, tmp_path):
    raw = _mime("agent@dtcdev.click")
    tasks, sessions, body, open_object, _workspace = _consumer(monkeypatch, tmp_path, raw)

    def boom(**_kwargs):
        raise RuntimeError("aplexer down")

    sessions.start = boom
    deleted = []

    class Queue:
        def receive_message(self, **kwargs):
            self.kwargs = kwargs
            return {"Messages": [{"MessageId": "1", "ReceiptHandle": "rh", "Body": json.dumps(body)}]}

        def delete_message(self, **kwargs):
            deleted.append(kwargs)

    queue = Queue()
    work.poll_once(
        queue, "https://queue.test/agent",
        load_mailbox=lambda name: agent_mailboxes.peek_mailbox(name),
        load_senders=lambda: agent_mailboxes.api_from_list()[1]["addresses"],
        open_object=open_object, tasks=tasks, sessions=sessions, sleep=lambda _s: None,
    )
    assert queue.kwargs["WaitTimeSeconds"] == 20
    assert queue.kwargs["VisibilityTimeout"] == 120
    assert deleted == []
    assert "aplexer down" in tasks.table.items["email:<m-1@example.test>"]["error"]


def test_agent_api_surface_matches_the_console_handlers(monkeypatch):
    _tables(monkeypatch)
    monkeypatch.setattr(agent, "require_operator", lambda event, action: ("op", None))
    monkeypatch.setattr(agent.audit, "emit", lambda *args, **kwargs: None)
    event = _request("PUT", {"workspace": "/from-agent", "engine": "codex"})
    response = agent.route(event, "PUT", "/api/agent/agent-mailboxes")
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["mailbox"]["engine"] == "codex"
    listed = agent.route(_request("GET"), "GET", "/api/agent/agent-mail/from")
    assert json.loads(listed["body"])["addresses"] == SEED
    tasks = agent.route(_request("GET"), "GET", "/api/agent/agent-tasks")
    assert json.loads(tasks["body"])["tasks"] == []
