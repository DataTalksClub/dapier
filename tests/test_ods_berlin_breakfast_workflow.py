"""Guard the shipped ods-berlin-breakfast workflow file."""
from pathlib import Path
from unittest.mock import patch
import yaml

from src.dapier.api.designer_store.validation import parse_workflow
from src.dapier.engine import execute
from src.dapier.engine.actions.code import run_code
from src.dapier.engine.code_tests import run_code_tests

WORKFLOW_FILE = Path(__file__).resolve().parents[1] / "workflows" / "ods-berlin-breakfast.yaml"


def shipped_workflow():
    return yaml.safe_load(WORKFLOW_FILE.read_text())


def find_action(workflow, action_id):
    for action in workflow.get("actions", []):
        if action.get("id") == action_id:
            return action
    return None


def test_the_shipped_ods_berlin_breakfast_workflow_validates():
    workflow = parse_workflow(WORKFLOW_FILE.read_text())
    assert workflow["id"] == "ods-berlin-breakfast"
    assert workflow["enabled"] is True
    assert workflow["trigger"]["connector"] == "schedule"
    assert workflow["trigger"]["event"] == "schedule.triggered"
    assert workflow["trigger"]["filters"]["schedule"]["equals"] == "ods-berlin-breakfast"

    action_ids = [action["id"] for action in workflow["actions"]]
    assert action_ids == [
        "prepare-announcement",
        "find-chat",
        "post-announcement",
        "pin-announcement",
        "maybe-unpin-previous",
    ]

    gate = find_action(workflow, "maybe-unpin-previous")
    assert gate["type"] == "condition"
    assert gate["when"]["steps.find-chat.output.chat.pinned_message_id"] == {"exists": True}
    assert gate["when"]["steps.find-chat.output.chat.pinned_is_ours"] == {"equals": True}
    then_ids = [action["id"] for action in gate["then"]]
    assert then_ids == ["unpin-previous"]


def test_prepare_announcement_embedded_code_tests():
    workflow = shipped_workflow()
    prepare_action = find_action(workflow, "prepare-announcement")
    assert prepare_action is not None
    report = run_code_tests(prepare_action)
    assert report["failed"] == 0, report


def test_prepare_announcement_text_formatting():
    workflow = shipped_workflow()
    prepare_action = find_action(workflow, "prepare-announcement")
    result = run_code(prepare_action, {"data": {}})["result"]
    assert "meeting_date" in result
    assert "timezone" in result
    assert "Дата-завтрак в среду на Alexanderplatz" in result["text"]
    assert "8:30 утра" in result["text"]
    assert "https://maps.app.goo.gl/fN63iVfjGdfFDZRBA" in result["text"]
    assert "Ставим 👍 кто придёт" in result["text"]


def test_workflow_execution_end_to_end():
    workflow = shipped_workflow()
    event = {
        "id": "ods-sched-1",
        "connector": "schedule",
        "event": "schedule.triggered",
        "data": {"schedule": "ods-berlin-breakfast"},
    }

    calls = []

    def fake_call(token, method, payload=None, **kwargs):
        calls.append({"method": method, "payload": payload})
        if method == "getMe":
            return {"id": 8606382261, "username": "ods_berlin_bot"}
        if method == "getChat":
            return {
                "id": -1001295648628,
                "title": "ODS Berlin",
                "type": "supergroup",
                "pinned_message": {
                    "message_id": 18766,
                    "from": {"id": 8606382261, "is_bot": True},
                },
            }
        if method == "sendMessage":
            return {
                "message_id": 19001,
                "chat": {"id": -1001295648628},
            }
        if method == "pinChatMessage":
            return True
        if method == "unpinChatMessage":
            return True
        return {}

    connection = {
        "connection_id": "telegram-ods-bot",
        "credential_id": "cred-tg",
        "provider": "telegram",
    }
    with patch("src.dapier.engine.all_workflows", return_value=[workflow]), \
         patch("src.dapier.engine.actions.base._connected_connection", return_value=connection), \
         patch("src.dapier.connections.credentials.get_credential", return_value={"token": "fake-bot-token"}), \
         patch("src.dapier.connections.providers.telegram_api.call", side_effect=fake_call):
        execute(event)

    called_methods = [c["method"] for c in calls]
    assert called_methods == ["getChat", "getMe", "sendMessage", "pinChatMessage", "unpinChatMessage"]

    # Check sendMessage payload
    send_call = next(c for c in calls if c["method"] == "sendMessage")
    assert send_call["payload"]["chat_id"] == "-1001295648628"
    assert send_call["payload"]["message_thread_id"] == 547
    assert send_call["payload"]["parse_mode"] == "HTML"
    assert "Дата-завтрак в среду на Alexanderplatz" in send_call["payload"]["text"]

    # Check pinChatMessage payload
    pin_call = next(c for c in calls if c["method"] == "pinChatMessage")
    assert pin_call["payload"]["chat_id"] == "-1001295648628"
    assert pin_call["payload"]["message_id"] == 19001
    assert pin_call["payload"]["disable_notification"] is True

    # Check unpinChatMessage payload
    unpin_call = next(c for c in calls if c["method"] == "unpinChatMessage")
    assert unpin_call["payload"]["chat_id"] == "-1001295648628"
    assert unpin_call["payload"]["message_id"] == 18766


def test_foreign_pin_is_left_alone():
    """A pin by someone else is never unpinned (the announce.py port)."""
    workflow = shipped_workflow()
    event = {
        "id": "ods-sched-2",
        "connector": "schedule",
        "event": "schedule.triggered",
        "data": {"schedule": "ods-berlin-breakfast"},
    }

    calls = []

    def fake_call(token, method, payload=None, **kwargs):
        calls.append({"method": method, "payload": payload})
        if method == "getMe":
            return {"id": 8606382261, "username": "ods_berlin_bot"}
        if method == "getChat":
            return {
                "id": -1001295648628,
                "title": "ODS Berlin",
                "type": "supergroup",
                "pinned_message": {
                    "message_id": 18766,
                    "from": {"id": 12345, "is_bot": False},
                },
            }
        if method == "sendMessage":
            return {
                "message_id": 19001,
                "chat": {"id": -1001295648628},
            }
        if method == "pinChatMessage":
            return True
        if method == "unpinChatMessage":
            return True
        return {}

    connection = {
        "connection_id": "telegram-ods-bot",
        "credential_id": "cred-tg",
        "provider": "telegram",
    }
    with patch("src.dapier.engine.all_workflows", return_value=[workflow]), \
         patch("src.dapier.engine.actions.base._connected_connection", return_value=connection), \
         patch("src.dapier.connections.credentials.get_credential", return_value={"token": "fake-bot-token"}), \
         patch("src.dapier.connections.providers.telegram_api.call", side_effect=fake_call):
        execute(event)

    called_methods = [c["method"] for c in calls]
    assert "unpinChatMessage" not in called_methods
    assert called_methods == ["getChat", "getMe", "sendMessage", "pinChatMessage"]
