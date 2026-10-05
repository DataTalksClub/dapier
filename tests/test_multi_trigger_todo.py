"""Cutover contract: each TODO input writes once, with its original formatting."""
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from src.dapier import engine
from src.dapier.engine.actions.templating import render

ROOT = Path(__file__).resolve().parents[1] / "migrations/multi-trigger-workflows"


@pytest.mark.parametrize("event_type", [None, "message.received", "channel_post.received", "callback_query.received"])
def test_todo_inputs_write_once_and_confirm_only_telegram(event_type):
    workflow = yaml.safe_load((ROOT / "todo-intake.yaml").read_text())
    telegram = event_type is not None
    event = {"id": "merge-test", "occurred_at": "2026-09-30T12:00:00Z",
             "connector": "telegram" if telegram else "email",
             "event": event_type or "message.received",
             "data": {"hook": "todo", "text": "/todo Check invoice", "chat_id": "123"}
             if telegram else {"route": "todo", "subject": "Invoice", "from": "operator@example.test"}}
    calls = []
    def capture(action, envelope, workflow_id, steps=None):
        calls.append(action)
        return {}
    with patch.object(engine, "_run_connector", capture):
        engine.execute(event, workflows=[workflow])
    assert [a["type"] for a in calls] == ["sheets_append_row", "dataops"] + (["telegram_send"] if telegram else [])
    values = calls[0]["values"][0]
    assert render(values[0], event) == "2026-09-30"
    assert render(values[1], event) == ("Check invoice" if telegram else 'Process email "Invoice" from operator@example.test')


def test_other_email_routes_and_telegram_hooks_do_not_run_todo():
    workflow = yaml.safe_load((ROOT / "todo-intake.yaml").read_text())
    for connector, data in [("email", {"route": "invoice"}), ("telegram", {"hook": "automator-telegram"})]:
        with patch.object(engine, "_run_connector", side_effect=AssertionError("unexpected action")):
            assert engine.execute({"connector": connector, "event": "message.received", "data": data}, workflows=[workflow]) == []
