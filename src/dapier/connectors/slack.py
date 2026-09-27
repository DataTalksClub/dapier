"""Slack connector: post messages through a stored credential or connection,
plus channel/user discovery and the auth.test health check.

The discovery and health-check runners delegate to the shared provider layer
(``connections.discovery``), which reads the connection's stored bot token.
"""
import json

from ..connections import discovery as provider
from ..engine.actions.slack import run_slack, run_slack_find, run_slack_find_user
from .registry import (
    Action,
    ConnectionTest,
    Discovery,
    register,
    register_connection_test,
    register_discovery,
)

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
        {"key": "channel", "label": "Channel", "placeholder": "#alerts", "required": True,
         "discover": {"resource": "slack.channels"}},
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


register(Action(
    type="slack_find",
    label="Slack: find user or channel",
    icon="slack",
    run=lambda action, event, workflow_id, steps=None: run_slack_find(action, event, steps=steps),
    required=frozenset({"query"}),
    optional=frozenset({"find", "connection_id", "credential_id", "timeout_seconds"}),
    description=("Looks up one workspace user (by email) or channel (by name). "
                 "Output: {found: true, user: {id, name, real_name, email, tz}} or "
                 "{found: true, channel: {id, name, is_private}}; a miss is "
                 "{found: false, user: null} / {found: false, channel: null}."),
    fields=(
        {"key": "find", "label": "Find", "type": "select",
         "options": ["user", "channel"], "default": "user"},
        {"key": "query", "label": "Query", "type": "text", "required": True,
         "help": ("user: the person's email address; "
                  "channel: the channel name (leading # optional)")},
        {"key": "connection_id", "label": "Connection ID", "placeholder": "resolves the credential"},
        {"key": "credential_id", "label": "Credential ID"},
    ),
))


register(Action(
    type="slack_find_user",
    label="Slack: find user by email",
    icon="slack",
    description=("Looks up one workspace user by email via users.lookupByEmail. "
                 "Output: {found: true, user: {id, name, real_name, email, tz}}; "
                 "a miss is {found: false, user: null}."),
    run=lambda action, event, workflow_id, steps=None: run_slack_find_user(action, event, steps=steps),
    required=frozenset({"connection_id", "email"}),
    optional=frozenset({"credential_id"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "resolves the credential",
         "required": True},
        {"key": "email", "label": "Email", "type": "email", "required": True,
         "placeholder": "person@example.com",
         "discover": {"resource": "slack.users"},
         "help": "Rendered from the event; the workspace user with this address"},
        {"key": "credential_id", "label": "Credential ID"},
    ),
))


def _listed(resource):
    """A discovery runner that lists ``resource`` through the shared layer."""
    def run(connection, params, *, transport=None):
        return provider.discover(connection, resource, params, transport=transport)
    return run


def _tested(connection, *, transport=None):
    return provider.test_connection(connection, transport=transport)


register_discovery(Discovery(
    name="channels",
    connector="slack",
    label="Channels",
    description="Active channels in the workspace",
    run=_listed("channels"),
))

register_discovery(Discovery(
    name="users",
    connector="slack",
    label="Users",
    description="People in the workspace",
    run=_listed("users"),
))

register_discovery(Discovery(
    name="messages",
    connector="slack",
    label="Messages",
    description="Recent messages in one channel",
    params=({"key": "channel", "label": "Channel ID", "type": "text", "required": True},),
    run=_listed("messages"),
))

register_connection_test(ConnectionTest(connector="slack", run=_tested))


# --- trigger discovery: channel options for the action's channel field ---------

from . import trigger_discovery
from .trigger_discovery import DEFAULT_LIMIT, TriggerDiscovery, register_trigger_discovery


def _fetch_channel_options(event=None, connection_id=None, limit=DEFAULT_LIMIT):
    """Channel options via the registry listing; falls back to the stored
    shared ``slack`` credential when no connection is connected yet."""
    return trigger_discovery.options_from_registry(
        "slack.channels", connection_id, limit, provider="slack",
        credential_fallback="slack",
        option_of=lambda item: {"value": item.get("id"),
                                "label": f"#{item.get('name') or item.get('id')}"})


register_trigger_discovery(TriggerDiscovery(
    connector="slack", label="Slack", kind="options", resource="slack.channels",
    fetch=_fetch_channel_options))
