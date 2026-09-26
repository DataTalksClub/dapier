"""Slack connector: post messages through a stored credential or connection."""
from ..engine.actions.slack import run_slack
from .registry import Action, register

register(Action(
    type="slack",
    label="Slack",
    icon="slack",
    run=lambda action, event, workflow_id, steps=None: run_slack(action, event, steps=steps),
    required=frozenset({"channel"}),
    optional=frozenset({"credential_id", "connection_id", "text", "unfurl_links",
                        "unfurl_media", "timeout_seconds", "telegram_format",
                        "source_link"}),
    fields=(
        {"key": "credential_id", "label": "Credential ID"},
        {"key": "connection_id", "label": "Connection ID", "placeholder": "resolves the credential"},
        {"key": "channel", "label": "Channel", "placeholder": "#alerts", "required": True},
        {"key": "text", "label": "Text template", "type": "textarea", "placeholder": "{title}\n{url}"},
        {"key": "timeout_seconds", "label": "Timeout (s)", "type": "number"},
        {"key": "unfurl_links", "label": "Unfurl links", "type": "boolean", "default": "true"},
        {"key": "unfurl_media", "label": "Unfurl media", "type": "boolean", "default": "true"},
        {"key": "telegram_format", "label": "Telegram formatting", "type": "boolean",
         "placeholder": "renders {text}+entities as Slack blocks, splits long posts into a thread"},
        {"key": "source_link", "label": "Source link template",
         "placeholder": "https://t.me/channel/{message_id}"},
    ),
))
