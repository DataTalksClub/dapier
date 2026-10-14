"""Guard the shipped ods-berlin-chat-id workflow (the /ids helper of the
ods-berlin-bot port)."""
from pathlib import Path
from unittest.mock import patch
import yaml

from src.dapier.api.designer_store.validation import parse_workflow
from src.dapier.engine import execute
from src.dapier.engine.matching import matches
from src.dapier.triggers import hook_triggers

WORKFLOW_FILE = Path(__file__).resolve().parents[1] / "workflows" / "ods-berlin-chat-id.yaml"

CONNECTION = {
    "connection_id": "telegram-ods-bot",
    "credential_id": "cred-tg",
    "provider": "telegram",
}


def shipped_workflow():
    return yaml.safe_load(WORKFLOW_FILE.read_text())


def ids_event(text="/ids", chat_id=-1001295648628, thread=547, hook="ods-berlin"):
    """A delivered-shaped group message: what the ods-berlin hook publishes
    when someone sends /ids inside the Meetings topic."""
    message = {
        "message_id": 19010,
        "text": text,
        "chat": {"id": chat_id, "title": "ODS Berlin",
                 "type": "supergroup", "is_forum": True},
        "from": {"id": 42, "is_bot": False, "first_name": "Alexey"},
    }
    if thread is not None:
        message["message_thread_id"] = thread
    return {
        "id": "ods-ids-1",
        "connector": "telegram",
        "event": "message.received",
        "data": hook_triggers.update_data({"update_id": 4242, "message": message}, hook),
    }


def run_reply(workflow, event, capture):
    """Execute the workflow with the Telegram Bot API faked; each Bot API
    call lands in ``capture`` as {method, payload}."""

    def fake_call(token, method, payload=None, **kwargs):
        capture.append({"method": method, "payload": payload})
        if method == "sendMessage":
            chat_id = payload["chat_id"]
            return {"message_id": 19011, "chat": {"id": chat_id}}
        return {}

    with patch("src.dapier.engine.all_workflows", return_value=[workflow]), \
         patch("src.dapier.engine.actions.base._connected_connection", return_value=CONNECTION), \
         patch("src.dapier.connections.credentials.get_credential", return_value={"token": "fake-bot-token"}), \
         patch("src.dapier.connections.providers.telegram_api.call", side_effect=fake_call):
        return execute(event)


def test_the_shipped_ods_berlin_chat_id_workflow_validates():
    workflow = parse_workflow(WORKFLOW_FILE.read_text())
    assert workflow["id"] == "ods-berlin-chat-id"
    assert workflow["enabled"] is True
    assert workflow["trigger"]["connector"] == "telegram"
    assert workflow["trigger"]["event"] == "message.received"
    assert workflow["trigger"]["filters"]["hook"]["equals"] == "ods-berlin"
    assert workflow["trigger"]["filters"]["text"]["equals"] == "/ids"
    reply = workflow["actions"][0]
    assert reply["type"] == "telegram_send"
    assert reply["connection_id"] == "telegram-ods-bot"


def test_only_an_ids_command_in_the_group_matches():
    workflow = shipped_workflow()
    assert matches(workflow, ids_event()) is True
    # Any other text on the same hook stays unmatched — the bot stays quiet.
    assert matches(workflow, ids_event(text="hello everyone")) is False
    # So does /ids arriving on a different bot's hook.
    assert matches(workflow, ids_event(hook="todo")) is False


def test_reply_carries_the_group_chat_id_and_topic():
    workflow = shipped_workflow()
    calls = []
    ran = run_reply(workflow, ids_event(), calls)

    assert ran == ["ods-berlin-chat-id"]
    assert [c["method"] for c in calls] == ["sendMessage"]
    payload = calls[0]["payload"]
    assert payload["chat_id"] == -1001295648628
    assert payload["message_thread_id"] == 547
    assert "chat_id: -1001295648628" in payload["text"]
    assert "message_thread_id: 547" in payload["text"]


def test_reply_outside_a_forum_omits_the_topic():
    """A plain chat has no topic id: the sendMessage omits the field
    instead of sending an empty thread."""
    workflow = shipped_workflow()
    calls = []
    run_reply(workflow, ids_event(chat_id=42, thread=None), calls)

    payload = calls[0]["payload"]
    assert payload["chat_id"] == 42
    assert "message_thread_id" not in payload
    assert "chat_id: 42" in payload["text"]
