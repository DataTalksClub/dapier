"""Slack connector: post and manage messages through a stored credential or
connection (send — top-level or threaded, scheduled, DM, update, react,
create-channel, topic, purpose, file upload), plus channel/user discovery,
the auth.test health check, the Events-API trigger samples (one per declared
event — message.received, app.mention, reaction.added, member.joined —
delivered by src.dapier.triggers.intake.slack_events), the find actions, and the
``slack.messages`` poll source (channel history feeding message.received on
a schedule, no Slack app required).

The discovery and health-check runners delegate to the shared provider layer
(``connections.discovery``), which reads the connection's stored bot token.
"""
import json

from src.dapier.connections import discovery as provider
from plugins.slack.runners.slack import (
    run_slack,
    run_slack_add_reaction,
    run_slack_add_reminder,
    run_slack_create_channel,
    run_slack_dm,
    run_slack_find,
    run_slack_find_message,
    run_slack_find_user,
    run_slack_invite_to_channel,
    run_slack_pin_message,
    run_slack_schedule_message,
    run_slack_set_purpose,
    run_slack_set_topic,
    run_slack_update_message,
    run_slack_upload_file,
)
from src.dapier.triggers.poll_sources import PollSource, register_source
from src.dapier.connectors import trigger_discovery
from src.dapier.connectors.registry import (
    Action,
    ConnectionTest,
    Connector,
    Discovery,
    connector,
    register,
    register_connection_test,
    register_discovery,
)
from src.dapier.connectors.trigger_discovery import (
    DEFAULT_LIMIT,
    DiscoveryNotFound,
    DiscoveryUpstream,
    TriggerDiscovery,
    register_trigger_discovery,
)

connector(Connector(
    name="slack", label="Slack",
    events=("message.received", "app.mention",
            "reaction.added", "member.joined"),
    icon="slack"))

register(Action(
    type="slack",
    label="Slack",
    icon="slack",
    run=lambda action, event, workflow_id, steps=None: run_slack(action, event, steps=steps),
    required=frozenset({"channel"}),
    optional=frozenset({"credential_id", "connection_id", "text", "unfurl_links",
                        "unfurl_media", "timeout_seconds", "telegram_format",
                        "source_link", "thread_ts", "username", "link_names", "reply_broadcast"}),
    fields=(
        {"key": "credential_id", "label": "Credential ID"},
        {"key": "connection_id", "label": "Connection ID", "placeholder": "resolves the credential"},
        {"key": "channel", "label": "Channel", "placeholder": "#alerts", "required": True,
         "discover": {"resource": "slack.channels"}},
        {"key": "username", "label": "Bot display name", "help": "Requires chat:write.customize on modern Slack apps"},
        {"key": "link_names", "label": "Link names", "type": "boolean"},
        {"key": "reply_broadcast", "label": "Broadcast thread reply", "type": "boolean"},
        {"key": "text", "label": "Text template", "type": "textarea", "placeholder": "{title}\n{url}"},
        {"key": "thread_ts", "label": "Thread timestamp", "placeholder": "{ts}",
         "discover": {"resource": "slack.messages",
                      "params": {"channel": "channel"}},
         "help": ("The ts of the message to reply to, e.g. from a "
                  "message.received trigger or slack_find output; leave empty "
                  "for a top-level post")},
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
    optional=frozenset({"find", "create_if_missing", "is_private",
                        "connection_id", "credential_id", "timeout_seconds"}),
    description=("Looks up one workspace user (by email) or channel (by name). "
                 "Output: {found: true, user: {id, name, real_name, email, tz}} or "
                 "{found: true, channel: {id, name, is_private}}; a miss is "
                 "{found: false, user: null} / {found: false, channel: null}. "
                 "With Create if missing on, a missed channel is created "
                 "(created: true) — Zapier's Find or Create Channel; a user "
                 "miss stays a miss."),
    fields=(
        {"key": "find", "label": "Find", "type": "select",
         "options": ["user", "channel"], "default": "user"},
        {"key": "query", "label": "Query", "type": "text", "required": True,
         "help": ("user: the person's email address; "
                  "channel: the channel name (leading # optional)")},
        {"key": "create_if_missing", "label": "Create if missing", "type": "boolean",
         "default": "false",
         "help": "Channels only: create the channel when no channel matches"},
        {"key": "is_private", "label": "Private channel", "type": "boolean",
         "default": "false",
         "help": "Only used when Create if missing is on"},
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


register(Action(
    type="slack_update_message",
    label="Slack: update message",
    icon="slack",
    description=("Edits one already-posted message via chat.update. channel/ts "
                 "render from the event — a slack trigger envelope carries "
                 "{channel_id} and {ts}. Output: {ok: true, channel, ts}."),
    run=lambda action, event, workflow_id, steps=None: run_slack_update_message(
        action, event, steps=steps),
    required=frozenset({"channel", "ts", "text"}),
    optional=frozenset({"connection_id", "credential_id"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "resolves the credential",
         "provider": "slack"},
        {"key": "channel", "label": "Channel", "required": True,
         "placeholder": "{channel_id} or #channel",
         "discover": {"resource": "slack.channels"}},
        {"key": "ts", "label": "Message ts", "required": True,
         "placeholder": "{ts}", "help": "The timestamp id of the message to edit",
         "discover": {"resource": "slack.messages",
                      "params": {"channel": "channel"}}},
        {"key": "text", "label": "New text", "type": "textarea", "required": True,
         "placeholder": "{text} (edited: updated by the workflow)"},
        {"key": "credential_id", "label": "Credential ID"},
    ),
))


register(Action(
    type="slack_add_reaction",
    label="Slack: add reaction",
    icon="slack",
    description=("Reacts to one message via reactions.add. reaction is the emoji "
                 "name without colons (tada); already_reacted counts as success so "
                 "a retried run stays green. Output: {ok: true, reaction, channel, ts}."),
    run=lambda action, event, workflow_id, steps=None: run_slack_add_reaction(
        action, event, steps=steps),
    required=frozenset({"channel", "timestamp", "reaction"}),
    optional=frozenset({"connection_id", "credential_id"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "resolves the credential",
         "provider": "slack"},
        {"key": "channel", "label": "Channel", "required": True,
         "placeholder": "{channel_id}",
         "discover": {"resource": "slack.channels"}},
        {"key": "timestamp", "label": "Message ts", "required": True,
         "placeholder": "{ts}", "help": "The timestamp id of the message to react to",
         "discover": {"resource": "slack.messages",
                      "params": {"channel": "channel"}}},
        {"key": "reaction", "label": "Reaction", "required": True,
         "placeholder": "tada", "help": "Emoji name without colons"},
        {"key": "credential_id", "label": "Credential ID"},
    ),
))


register(Action(
    type="slack_dm",
    label="Slack: send direct message",
    icon="slack",
    description=("Opens (or reuses) the DM channel with one user and posts the "
                 "templated text into it (conversations.open + chat.postMessage). "
                 "Chain slack_find_user and pass {steps.<id>.output.user.id} to "
                 "target the person an event names. Output: {ok: true, user, "
                 "channel, ts}."),
    run=lambda action, event, workflow_id, steps=None: run_slack_dm(action, event, steps=steps),
    required=frozenset({"user_id"}),
    optional=frozenset({"connection_id", "credential_id", "text", "unfurl_links",
                        "unfurl_media", "timeout_seconds"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "resolves the credential",
         "provider": "slack"},
        {"key": "user_id", "label": "User ID", "required": True,
         "placeholder": "{steps.find.output.user.id}",
         "discover": {"resource": "slack.users"},
         "help": "Rendered from the event; the person to message"},
        {"key": "text", "label": "Text template", "type": "textarea", "placeholder": "{title}\n{url}"},
        {"key": "unfurl_links", "label": "Unfurl links", "type": "boolean", "default": "true"},
        {"key": "unfurl_media", "label": "Unfurl media", "type": "boolean", "default": "true"},
        {"key": "credential_id", "label": "Credential ID"},
    ),
))


register(Action(
    type="slack_create_channel",
    label="Slack: create channel",
    icon="slack",
    description=("Creates one channel via conversations.create. The name is "
                 "normalized to what Slack accepts (lowercase, spaces to "
                 "hyphens, illegal characters dropped, 80 characters); an "
                 "existing name is an error — chain slack_find first when "
                 "create-if-missing matters. Output: {ok: true, channel: "
                 "{id, name, is_private}}."),
    run=lambda action, event, workflow_id, steps=None: run_slack_create_channel(
        action, event, steps=steps),
    required=frozenset({"name"}),
    optional=frozenset({"connection_id", "credential_id", "is_private"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "resolves the credential",
         "provider": "slack"},
        {"key": "name", "label": "Channel name", "required": True,
         "placeholder": "alerts-{customer}",
         "help": "Rendered from the event, then normalized for Slack"},
        {"key": "is_private", "label": "Private channel", "type": "boolean"},
        {"key": "credential_id", "label": "Credential ID"},
    ),
))


register(Action(
    type="slack_set_topic",
    label="Slack: set channel topic",
    icon="slack",
    description=("Sets one channel's topic via conversations.setTopic. "
                 "channel renders from the event — a slack trigger envelope "
                 "carries {channel_id}. Output: {ok: true, channel, topic}."),
    run=lambda action, event, workflow_id, steps=None: run_slack_set_topic(action, event, steps=steps),
    required=frozenset({"channel", "topic"}),
    optional=frozenset({"connection_id", "credential_id"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "resolves the credential",
         "provider": "slack"},
        {"key": "channel", "label": "Channel", "required": True,
         "placeholder": "{channel_id} or #channel",
         "discover": {"resource": "slack.channels"}},
        {"key": "topic", "label": "Topic", "type": "textarea", "required": True},
        {"key": "credential_id", "label": "Credential ID"},
    ),
))


register(Action(
    type="slack_set_purpose",
    label="Slack: set channel purpose",
    icon="slack",
    description=("Sets one channel's purpose via conversations.setPurpose. "
                 "channel renders from the event — a slack trigger envelope "
                 "carries {channel_id}. Output: {ok: true, channel, purpose}."),
    run=lambda action, event, workflow_id, steps=None: run_slack_set_purpose(
        action, event, steps=steps),
    required=frozenset({"channel", "purpose"}),
    optional=frozenset({"connection_id", "credential_id"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "resolves the credential",
         "provider": "slack"},
        {"key": "channel", "label": "Channel", "required": True,
         "placeholder": "{channel_id} or #channel",
         "discover": {"resource": "slack.channels"}},
        {"key": "purpose", "label": "Purpose", "type": "textarea", "required": True},
        {"key": "credential_id", "label": "Credential ID"},
    ),
))


register(Action(
    type="slack_invite_to_channel",
    label="Slack: invite users to channel",
    icon="slack",
    description=("Invites one or more workspace users into a channel via "
                 "conversations.invite. users takes comma-separated member "
                 "ids — pick one from the user picker or chain slack_find_user. "
                 "Slack's already_in_channel is absorbed as invited: false "
                 "with the reason, so a retried run stays green. Output: "
                 "{invited, channel, users} (plus reason on the absorbed miss)."),
    run=lambda action, event, workflow_id, steps=None: run_slack_invite_to_channel(
        action, event, steps=steps),
    required=frozenset({"channel", "users"}),
    optional=frozenset({"connection_id", "credential_id"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "resolves the credential",
         "provider": "slack"},
        {"key": "channel", "label": "Channel", "required": True,
         "placeholder": "{channel_id} or #channel",
         "discover": {"resource": "slack.channels"}},
        {"key": "users", "label": "Users", "required": True,
         "placeholder": "{steps.find.output.user.id}",
         "discover": {"resource": "slack.users"},
         "help": "Comma-separated member ids — the picker serves one, more "
                 "can be appended; chain slack_find_user to target the "
                 "person an event names"},
        {"key": "credential_id", "label": "Credential ID"},
    ),
))


register(Action(
    type="slack_pin_message",
    label="Slack: pin message",
    icon="slack",
    description=("Pins one message to its channel via pins.add. channel and "
                 "timestamp render from the event — a slack trigger envelope "
                 "carries {channel_id} and {ts}. Output: {pinned: true, "
                 "channel, timestamp}."),
    run=lambda action, event, workflow_id, steps=None: run_slack_pin_message(
        action, event, steps=steps),
    required=frozenset({"channel", "timestamp"}),
    optional=frozenset({"connection_id", "credential_id"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "resolves the credential",
         "provider": "slack"},
        {"key": "channel", "label": "Channel", "required": True,
         "placeholder": "{channel_id}",
         "discover": {"resource": "slack.channels"}},
        {"key": "timestamp", "label": "Message ts", "required": True,
         "placeholder": "{ts}", "help": "The timestamp id of the message to pin",
         "discover": {"resource": "slack.messages",
                      "params": {"channel": "channel"}}},
        {"key": "credential_id", "label": "Credential ID"},
    ),
))


register(Action(
    type="slack_find_message",
    label="Slack: find message",
    icon="slack",
    description=("Searches workspace messages via search.messages (the token "
                 "needs the search:read scope). query searches like Slack's "
                 "own search box; count caps the results (1-100, default 20). "
                 "Output: {found, messages: [{ts, channel_id, channel_name, "
                 "user, text, permalink}], count} — a miss is {found: false, "
                 "messages: [], count: 0}, not an error."),
    run=lambda action, event, workflow_id, steps=None: run_slack_find_message(
        action, event, steps=steps),
    required=frozenset({"query"}),
    optional=frozenset({"count", "connection_id", "credential_id"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "resolves the credential",
         "provider": "slack"},
        {"key": "query", "label": "Query", "type": "text", "required": True,
         "placeholder": "deploy postmortem",
         "help": "Rendered from the event; searches like Slack's search box"},
        {"key": "count", "label": "Max results", "type": "number",
         "help": "1-100, default 20"},
        {"key": "credential_id", "label": "Credential ID"},
    ),
))


register(Action(
    type="slack_upload_file",
    label="Slack: upload file",
    icon="slack",
    description=("Sends one file into a channel via files.uploadV2 "
                 "(getUploadURLExternal → upload → completeUploadExternal). "
                 "Content comes from exactly one of source_url (a download "
                 "URL), source_s3 {bucket, key} (a staged object — "
                 "dropbox_read_file and drive_read_file chain here), or "
                 "inline content. Output: {ok: true, channel, file: {id, "
                 "name, title, permalink}}."),
    run=lambda action, event, workflow_id, steps=None: run_slack_upload_file(
        action, event, steps=steps),
    required=frozenset({"channel", "filename"}),
    optional=frozenset({"connection_id", "credential_id", "source_url",
                        "source_s3", "content", "title", "initial_comment",
                        "thread_ts", "content_type"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "resolves the credential",
         "provider": "slack"},
        {"key": "channel", "label": "Channel", "required": True,
         "placeholder": "{channel_id} or C01ABC2DEF",
         "help": "The upload API takes a channel id (the picker serves ids)",
         "discover": {"resource": "slack.channels"}},
        {"key": "filename", "label": "File name", "required": True,
         "placeholder": "report.pdf"},
        {"key": "source_url", "label": "Source URL", "type": "url",
         "placeholder": "https://www.googleapis.com/drive/v3/files/{id}?alt=media",
         "help": ("One content source: a download URL — or source_s3 "
                  "{bucket, key} for a staged object, or inline content")},
        {"key": "content", "label": "Content",
         "help": "Inline text content — takes templates, e.g. {trigger.text}"},
        {"key": "title", "label": "Title",
         "help": "Shown in Slack; defaults to the file name"},
        {"key": "initial_comment", "label": "Comment", "type": "textarea",
         "help": "Posted with the file — takes templates"},
        {"key": "thread_ts", "label": "Thread ts",
         "placeholder": "{ts} — replies to that message instead of posting top-level",
         "discover": {"resource": "slack.messages",
                      "params": {"channel": "channel"}}},
        {"key": "content_type", "label": "Content type",
         "placeholder": "guessed from the file name"},
        {"key": "credential_id", "label": "Credential ID"},
    ),
))


register(Action(
    type="slack_schedule_message",
    label="Slack: send scheduled message",
    icon="slack",
    description=("Sends one templated message for later via "
                 "chat.scheduleMessage. post_at takes an ISO 8601 datetime "
                 "(a trailing Z or missing offset reads as UTC) or epoch "
                 "seconds, templated from the event. Output: {ok: true, "
                 "channel, scheduled_message_id, ts, post_at}."),
    run=lambda action, event, workflow_id, steps=None: run_slack_schedule_message(
        action, event, steps=steps),
    required=frozenset({"channel", "text", "post_at"}),
    optional=frozenset({"connection_id", "credential_id", "thread_ts",
                        "unfurl_links", "unfurl_media"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "resolves the credential",
         "provider": "slack"},
        {"key": "channel", "label": "Channel", "required": True,
         "placeholder": "{channel_id} or #channel",
         "discover": {"resource": "slack.channels"}},
        {"key": "text", "label": "Text template", "type": "textarea", "required": True,
         "placeholder": "{title}\n{url}"},
        {"key": "post_at", "label": "Post at", "required": True,
         "placeholder": "2026-10-02T09:00:00Z",
         "help": ("ISO 8601 datetime or epoch seconds — a missing offset "
                  "reads as UTC; renders from the event like every field")},
        {"key": "thread_ts", "label": "Thread ts",
         "placeholder": "{ts}",
         "discover": {"resource": "slack.messages",
                      "params": {"channel": "channel"}},
         "help": "Schedules the message as a reply in that thread"},
        {"key": "unfurl_links", "label": "Unfurl links", "type": "boolean", "default": "true"},
        {"key": "unfurl_media", "label": "Unfurl media", "type": "boolean", "default": "true"},
        {"key": "credential_id", "label": "Credential ID"},
    ),
))


register(Action(
    type="slack_add_reminder",
    label="Slack: add reminder",
    icon="slack",
    description=("Sets one reminder via reminders.add (Zapier's Add "
                 "Reminder). time takes Slack's natural-language times — "
                 "in 20 minutes, tomorrow 9am — or epoch seconds; without "
                 "it Slack reminds in 20 minutes. The reminder belongs to "
                 "the token's own user. Output: {ok: true, reminder: "
                 "{id, time, text}}."),
    run=lambda action, event, workflow_id, steps=None: run_slack_add_reminder(
        action, event, steps=steps),
    required=frozenset({"text"}),
    optional=frozenset({"time", "connection_id", "credential_id"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "resolves the credential",
         "provider": "slack"},
        {"key": "text", "label": "Reminder text", "type": "textarea", "required": True,
         "placeholder": "Rotate the {customer} API key"},
        {"key": "time", "label": "When",
         "placeholder": "in 20 minutes / tomorrow 9am / 1759400000",
         "help": ("Natural language or epoch seconds; renders from the "
                  "event. Empty = Slack's default (20 minutes)")},
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


# --- trigger discovery: one sample per declared event ---------------------------
#
# The events here are exactly the ones triggers.intake.slack_events publishes
# and the Slack chip declares; the event field of a discovery request picks
# the payload. message.received pulls live from channel history; a reaction
# or a join never shows up in history, so the other events fall back to
# recorded runs, then the documented delivery below.

_SYNTHETIC_DELIVERIES = {
    "message.received": {
        "team_id": "T0SLACKTEAM",
        "event_id": "Ev0SAMPLE0001",
        "event_time": 1758900000,
        "event": {"type": "message", "channel": "C01BQC114P2", "user": "U02PFU1LS",
                  "text": "Heads up: the deploy finished cleanly.",
                  "ts": "1758900012.000300"},
    },
    "app.mention": {
        "team_id": "T0SLACKTEAM",
        "event_id": "Ev0SAMPLE0002",
        "event_time": 1758900000,
        "event": {"type": "app_mention", "channel": "C01BQC114P2", "user": "U02PFU1LS",
                  "text": "<@U0SLACKBOT> what did the deploy do?",
                  "ts": "1758900015.000400"},
    },
    "reaction.added": {
        "team_id": "T0SLACKTEAM",
        "event_id": "Ev0SAMPLE0003",
        "event_time": 1758900000,
        "event": {"type": "reaction_added", "user": "U02PFU1LS", "reaction": "tada",
                  "item_user": "U0SLACKBOT",
                  "item": {"type": "message", "channel": "C01BQC114P2",
                           "ts": "1758900012.000300"},
                  "event_ts": "1758900100.000500"},
    },
    "member.joined": {
        "team_id": "T0SLACKTEAM",
        "event_id": "Ev0SAMPLE0004",
        "event_time": 1758900000,
        "event": {"type": "member_joined_channel", "user": "U02PFU1LS",
                  "channel": "C01BQC114P2", "channel_type": "C",
                  "inviter": "U0SLACKBOT", "event_ts": "1758900200.000600"},
    },
}

_SYNTHETIC_DELIVERY = _SYNTHETIC_DELIVERIES["message.received"]


def _envelope(connection_id, payload, event=None):
    """A sample envelope with the exact data shape a real delivery publishes."""
    from src.dapier.triggers.intake import slack_events

    return {
        "connector": "slack",
        "event": event or slack_events.SLACK_EVENT,
        "source": connection_id,
        "data": slack_events.event_data(payload, connection_id),
    }


def _fetch_sample(event=None, connection_id=None, limit=DEFAULT_LIMIT, transport=None):
    """The sample for one declared Slack event, delivery-shaped.

    ``message.received`` walks the connected workspace's channels and returns
    the first message found (Slack serves history per channel — no
    channel-less read like Telegram's getUpdates). The mention, reaction and
    membership events have no history read, so they go straight to the
    fallback: recorded runs carrying the asked event, then the documented
    delivery — the designer preview must always render.
    """
    wanted = event or "message.received"
    if wanted == "message.received":
        try:
            connection = trigger_discovery.connected_connection("slack", connection_id)
        except DiscoveryNotFound:
            connection = None
        if connection is not None:
            try:
                for channel in provider.discover(connection, "channels", {}, transport=transport):
                    messages = provider.discover(
                        connection, "messages",
                        {"channel": channel["id"], "limit": max(limit, 1)}, transport=transport)
                    for item in messages:
                        payload = {"team_id": None, "event_id": None, "event_time": None,
                                   "event": {"type": "message", "channel": channel["id"],
                                             "user": item.get("user"), "text": item.get("name"),
                                             "ts": item.get("ts")}}
                        return {"sample": trigger_discovery.as_sample(_envelope(
                            connection["connection_id"], payload, wanted)),
                            "source": "live", "connection_id": connection["connection_id"]}
            except (DiscoveryNotFound, DiscoveryUpstream):
                pass  # not connected yet, or the API came back empty — fall through
    found = trigger_discovery.history_sample("slack", event=wanted)
    if found is not None:
        return {"sample": found, "source": "history", "connection_id": connection_id}
    delivery = _SYNTHETIC_DELIVERIES.get(wanted)
    if delivery is None:
        wanted = "message.received"
        delivery = _SYNTHETIC_DELIVERY
    envelope = _envelope(connection_id or "slack", delivery, wanted)
    return {"sample": trigger_discovery.synthetic_sample(
        envelope["connector"], envelope["event"], envelope["data"]),
        "source": "synthetic", "connection_id": connection_id}


register_trigger_discovery(TriggerDiscovery(
    connector="slack", label="Slack", kind="sample", resource="",
    fetch=_fetch_sample))


# --- trigger discovery: channel options for the action's channel field ---------


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


def _person_option(item):
    """``{value: member id, label: "Name (@handle)"}`` for one member."""
    name = item.get("name") or item.get("id")
    handle = item.get("handle")
    return {"value": item.get("id"),
            "label": f"{name} (@{handle})" if handle else name}


def _fetch_user_options(event=None, connection_id=None, limit=DEFAULT_LIMIT):
    """User options via the registry listing; falls back to the stored shared
    ``slack`` credential like the channel options. The value is the member
    id the find-user action stores, labeled with name and handle."""
    return trigger_discovery.options_from_registry(
        "slack.users", connection_id, limit, provider="slack",
        credential_fallback="slack",
        option_of=_person_option)


register_trigger_discovery(TriggerDiscovery(
    connector="slack", label="Slack", kind="options", resource="slack.users",
    fetch=_fetch_user_options))


def _fetch_message_options(event=None, connection_id=None, limit=DEFAULT_LIMIT):
    """Recent-message options for one channel via the registry listing; the
    channel id rides in ``event`` (trigger_discovery.listing_params). The
    value is the message ts, labeled with its text."""
    return trigger_discovery.options_from_registry(
        "slack.messages", connection_id, limit, provider="slack",
        credential_fallback="slack",
        params=trigger_discovery.listing_params(event, ("channel",)),
        option_of=lambda item: {"value": item.get("ts") or item.get("id"),
                                "label": item.get("name") or item.get("id")})


register_trigger_discovery(TriggerDiscovery(
    connector="slack", label="Slack", kind="options", resource="slack.messages",
    fetch=_fetch_message_options))


# --- poll source: "New Message" on the poll-trigger schedule --------------------
#
# message.received fires from Slack's Events API (triggers.intake.slack_
# events) — but that needs a Slack app with Event Subscriptions and the
# signed Request URL validated. The ``slack.messages`` source is the no-app
# path: a stored poll trigger reads one channel's history through
# conversations.history on the schedule machinery (triggers.poll_sources)
# and publishes the same ``slack``/``message.received`` event — scoped per
# trigger through the poll filter — so a workflow matches the same chip
# either way. Only human-posted messages flatten (the intake never publishes
# bot posts; here Slack's own bot-message subtypes are skipped the same way
# the intake skips ``bot_id`` posts). The first fire seeds the cursor at the
# channel's newest message without emitting: enabling a trigger must not
# replay the channel's history.

SLACK_POLL_PAGE_SIZE = 200  # conversations.history's own page cap
SLACK_POLL_PAGES = 2        # ~400 messages per fire, newest first

# Edit subtypes the message family intentionally publishes as messages; a
# bot app's own posts announce themselves with a ``bot_id`` field instead.
_SLACK_POLL_BOT_SUBTYPES = frozenset({
    "bot_message", "message_changed", "message_deleted", "channel_join",
    "channel_leave", "channel_topic", "channel_purpose", "channel_name",
    "channel_archive", "channel_unarchive", "channel_convert_to_private",
})


def _slack_poll_validate(body):
    """Save-time fetch spec: the ``connection_id`` of the Slack connection to
    read as (required — the fetch resolves its stored bot token) and the
    ``channel_id`` of the channel to watch (required), plus the fetch
    defaults every stored slack poll carries."""
    from src.dapier.triggers.email_triggers import TriggerError

    body = body if isinstance(body, dict) else {}
    if not str(body.get("connection_id") or "").strip():
        raise TriggerError(
            "connection_id is required: pick the Slack connection to read as")
    if not str(body.get("channel_id") or "").strip():
        raise TriggerError(
            "channel_id is required: pick the Slack channel to watch")
    return {
        "channel_id": str(body.get("channel_id") or "").strip(),
        "cursor_mode": "next_cursor",
        "id_path": "id",
        "url": "",
    }


def _slack_poll_token(item):
    """The connection's stored bot token, resolved like the actions'."""
    from src.dapier.connections import credentials
    from src.dapier.engine.actions import base

    connection = base._connected_connection(item["connection_id"])
    secret = credentials.get_credential(connection.get("credential_id"))
    token = (secret.get("token") or secret.get("bot_token")
             or secret.get("user_token") or secret.get("SLACK_BOT_TOKEN"))
    if not token:
        raise RuntimeError(
            f"connection {item['connection_id']} has no stored bot token")
    return token


def _slack_poll_messages(token, channel_id, *, transport=None):
    """The channel's recent history, following continuation pages up to
    SLACK_POLL_PAGES through the provider's shared Slack call."""
    messages = []
    cursor = None
    for _page in range(SLACK_POLL_PAGES):
        payload = {"channel": channel_id, "limit": SLACK_POLL_PAGE_SIZE}
        if cursor:
            payload["cursor"] = cursor
        data = provider._slack_call("conversations.history", token, payload,
                                    transport=transport)
        found = [message for message in data.get("messages") or []
                 if isinstance(message, dict) and message.get("ts")]
        messages.extend(found)
        cursor = (data.get("response_metadata") or {}).get("next_cursor")
        if not found or not cursor:
            break
    return messages


def _slack_poll_item(message, channel_id):
    """One history message flattened to the keys a real Events delivery
    publishes (``slack_events.event_data``'s flattened fields) — so
    downstream templates read the same keys no matter which path fired.
    App posts never flatten, mirroring the intake's ``bot_id`` rule: a poll
    reading the channel a workflow posts into must not loop it either.
    ``id`` is the watermark key: the message ts, which text comparison
    orders chronologically."""
    if message.get("bot_id"):
        return None
    subtype = str(message.get("subtype") or "")
    if subtype in _SLACK_POLL_BOT_SUBTYPES:
        return None
    ts = str(message["ts"])
    return {
        "id": ts,
        "type": "message",
        "subtype": message.get("subtype"),
        "channel_id": channel_id,
        "user_id": message.get("user"),
        "text": message.get("text"),
        "ts": ts,
        "thread_ts": message.get("thread_ts"),
    }


def _slack_poll_fetch(item, cursor=None, *, transport=None):
    """One poll page as ``(items, next_cursor)`` for ``triggers.poll_sources``.

    Reads the stored ``channel_id``'s history with the bot token behind the
    stored ``connection_id``. The cursor is the newest fired message ts,
    which text comparison orders chronologically. With no stored cursor —
    the first fire after enabling — the fetch only seeds the watermark at
    the channel's newest message and emits nothing. With a cursor, only
    messages strictly past it fire, oldest first, and the parked cursor is
    the last fired ts (the incoming newest when nothing qualifies; ``fire``
    parks it only once the page drains). The lookback deliberately overlaps
    between fires: the cursor filters the re-read history and the seen store
    dedupes what slips past it. Raises ``RuntimeError`` on a failed fetch,
    like every poll source.
    """
    if not str(item.get("connection_id") or "").strip():
        raise RuntimeError("poll source 'slack.messages' needs connection_id: "
                           "the Slack connection to read as")
    channel_id = str(item.get("channel_id") or "").strip()
    if not channel_id:
        raise RuntimeError("poll source 'slack.messages' needs channel_id: "
                           "the Slack channel to watch")
    try:
        token = _slack_poll_token(item)
        messages = _slack_poll_messages(token, channel_id, transport=transport)
    except (provider.DiscoveryError, ValueError) as exc:
        raise RuntimeError(f"slack poll failed: {exc}") from None
    items = sorted(
        (entry for entry in (_slack_poll_item(message, channel_id)
                             for message in messages)
         if entry is not None),
        key=lambda entry: entry["id"])
    if cursor is None:
        # First fire: seed the watermark at the channel's newest message
        # (None while the channel is empty) without emitting anything.
        return [], (items[-1]["id"] if items else None)
    watermark = str(cursor)
    fresh = [entry for entry in items if entry["id"] > watermark]
    return fresh, (fresh[-1]["id"] if fresh else watermark)


def _slack_poll_view(item):
    """The provider params ``public_view`` shows beside ``source``."""
    return {"channel_id": item.get("channel_id")}


register_source(PollSource(
    name="slack.messages", connector="slack", event="message.received",
    label="Slack", validate=_slack_poll_validate,
    fetch=_slack_poll_fetch, view=_slack_poll_view))
