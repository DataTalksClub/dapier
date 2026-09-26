"""Telegram connector: post messages through a bot connection."""
from ..engine.actions.telegram import run_telegram_send
from .registry import Action, register

register(Action(
    type="telegram_send",
    label="Telegram",
    icon="send",
    run=lambda action, event, workflow_id, steps=None: run_telegram_send(action, event, steps=steps),
    required=frozenset({"connection_id"}),
    optional=frozenset({"chat_id", "text", "timeout_seconds"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "required": True},
        {"key": "chat_id", "label": "Chat ID", "placeholder": "defaults to the triggering chat"},
        {"key": "text", "label": "Text template", "type": "textarea", "placeholder": "{text}"},
        {"key": "timeout_seconds", "label": "Timeout (s)", "type": "number"},
    ),
))
