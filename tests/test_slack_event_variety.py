"""Slack event variety: one distinct workflow event per subscribed Slack type.

The Slack chip declares message.received / app.mention / reaction.added /
member.joined and the intake publishes all four (see
triggers.intake.slack_events). app_mention used to fold into
message.received; it now publishes its own name only, while every
``message.*`` delivery keeps the message.received contract byte-for-byte:
same name, same envelope, same dedup id.
"""
import hashlib

from src.dapier.triggers.intake import slack_events

from tests.test_slack_events import (Table, setup_connection, signed,
                                     slack_secrets)


def connected(monkeypatch):
    """A connected slack connection with a stored signing secret."""
    table, stored = Table(), {}
    setup_connection(table)
    slack_secrets(monkeypatch, stored)
    stored["oauth#slack-conn"] = {"token": "xoxb-" + "t" * 20,
                                  "signing_secret": "faslack-signing-secret-000"}
    published = []
    return table, published


def publish_into(published):
    return lambda *args, **kwargs: published.append((args, kwargs))


# Real delivery shapes: reaction_added nests the channel inside ``item``;
# member_joined_channel carries the inviter at the top level.
NEW_DELIVERIES = {
    "app.mention": {"type": "app_mention", "channel": "C1", "user": "U1",
                    "text": "<@U0> ship it", "ts": "1758900015.000400"},
    "reaction.added": {"type": "reaction_added", "user": "U1", "reaction": "tada",
                       "item_user": "U0",
                       "item": {"type": "message", "channel": "C1",
                                "ts": "1758900012.000300"},
                       "event_ts": "1758900100.000500"},
    "member.joined": {"type": "member_joined_channel", "user": "U1",
                      "channel": "C1", "channel_type": "C",
                      "inviter": "U0", "event_ts": "1758900200.000600"},
}


def test_event_name_maps_every_subscribed_family():
    assert slack_events.event_name("message.channels") == "message.received"
    assert slack_events.event_name("message") == "message.received"
    assert slack_events.event_name("message.mpim") == "message.received"
    assert slack_events.event_name("app_mention") == "app.mention"
    assert slack_events.event_name("reaction_added") == "reaction.added"
    assert slack_events.event_name("member_joined_channel") == "member.joined"
    assert slack_events.event_name("channel_created") is None
    assert slack_events.event_name("") is None
    assert slack_events.EVENTS == ("message.received", "app.mention",
                                   "reaction.added", "member.joined")


def test_every_new_event_publishes_its_own_name(monkeypatch):
    table, published = connected(monkeypatch)
    for number, (name, slack_event) in enumerate(NEW_DELIVERIES.items()):
        body, headers = signed({"type": "event_callback", "team_id": "TWORKSPACE",
                                "event_id": f"Ev{number}", "event_time": 1758900000,
                                "event": slack_event})
        status, answer = slack_events.handle("slack-conn", headers, body,
                                             connections_table=table,
                                             publish=publish_into(published))
        assert status == 200 and answer == {"accepted": True}, name
        assert published[-1][0][:2] == ("slack", name), name
        assert published[-1][1]["source"] == "slack-conn", name
        assert published[-1][0][2]["type"] == slack_event["type"], name
    assert [args[1] for args, _ in published] == ["app.mention", "reaction.added",
                                                  "member.joined"]


def test_app_mention_publishes_only_the_new_name(monkeypatch):
    # Backward-compat decision: app_mention never fires message.received
    # workflows any more; it publishes app.mention and nothing else.
    table, published = connected(monkeypatch)
    body, headers = signed({"type": "event_callback", "team_id": "TWORKSPACE",
                            "event_id": "EvM", "event_time": 1758900000,
                            "event": NEW_DELIVERIES["app.mention"]})
    status, _ = slack_events.handle("slack-conn", headers, body,
                                    connections_table=table,
                                    publish=publish_into(published))
    assert status == 200
    assert [args[1] for args, _ in published] == ["app.mention"]


def test_message_deliveries_keep_the_message_received_contract(monkeypatch):
    table, published = connected(monkeypatch)
    payload = {"type": "event_callback", "team_id": "TWORKSPACE",
               "event_id": "Ev1", "event_time": 1758900000,
               "event": {"type": "message.im", "channel": "D0", "user": "U1",
                         "text": "a dm", "ts": "1758900030.000100"}}
    body, headers = signed(payload)
    status, _ = slack_events.handle("slack-conn", headers, body,
                                    connections_table=table,
                                    publish=publish_into(published))
    assert status == 200
    assert published[0][0][:2] == ("slack", "message.received")
    data = published[0][0][2]
    assert data["channel_id"] == "D0" and data["user_id"] == "U1"
    assert data["text"] == "a dm" and data["ts"] == "1758900030.000100"
    expected = "slack:" + hashlib.sha256(b"slack-conn:Ev1").hexdigest()[:32]
    assert published[0][1]["event_id"] == expected


def test_reaction_payload_carries_channel_reaction_user_and_timestamps(monkeypatch):
    table, published = connected(monkeypatch)
    body, headers = signed({"type": "event_callback", "team_id": "TWORKSPACE",
                            "event_id": "EvR", "event_time": 1758900000,
                            "event": NEW_DELIVERIES["reaction.added"]})
    slack_events.handle("slack-conn", headers, body, connections_table=table,
                        publish=publish_into(published))
    data = published[0][0][2]
    # Slack nests the channel inside item for reaction deliveries.
    assert data["channel_id"] == "C1"
    assert data["user_id"] == "U1" and data["reaction"] == "tada"
    assert data["ts"] == "1758900100.000500"  # event_ts fallback: when added
    assert data["item_ts"] == "1758900012.000300"  # the reacted-to message
    assert data["event"]["item_user"] == "U0"  # whose message was reacted to


def test_member_joined_payload_carries_channel_user_and_inviter(monkeypatch):
    table, published = connected(monkeypatch)
    body, headers = signed({"type": "event_callback", "team_id": "TWORKSPACE",
                            "event_id": "EvJ", "event_time": 1758900000,
                            "event": NEW_DELIVERIES["member.joined"]})
    slack_events.handle("slack-conn", headers, body, connections_table=table,
                        publish=publish_into(published))
    data = published[0][0][2]
    assert data["channel_id"] == "C1" and data["user_id"] == "U1"
    assert data["inviter"] == "U0"
    assert data["event"]["channel_type"] == "C"


def test_new_deliveries_dedup_stably_and_never_collide(monkeypatch):
    table, published = connected(monkeypatch)
    body, headers = signed({"type": "event_callback", "team_id": "TWORKSPACE",
                            "event_id": "EvR", "event_time": 1758900000,
                            "event": NEW_DELIVERIES["reaction.added"]})
    slack_events.handle("slack-conn", headers, body, connections_table=table,
                        publish=publish_into(published))
    slack_events.handle("slack-conn", headers, body, connections_table=table,
                        publish=publish_into(published))
    assert published[0][1]["event_id"] == published[1][1]["event_id"]
    other, other_headers = signed({"type": "event_callback", "team_id": "TWORKSPACE",
                                   "event_id": "EvJ", "event_time": 1758900000,
                                   "event": NEW_DELIVERIES["member.joined"]})
    slack_events.handle("slack-conn", other_headers, other, connections_table=table,
                        publish=publish_into(published))
    assert published[1][1]["event_id"] != published[2][1]["event_id"]


def test_unsubscribed_events_and_bot_reactions_stay_silent(monkeypatch):
    table, published = connected(monkeypatch)
    publish = publish_into(published)
    other, other_headers = signed({"type": "event_callback", "team_id": "TWORKSPACE",
                                   "event_id": "EvU", "event": {
                                       "type": "channel_created",
                                       "channel": {"id": "C1"}}})
    assert slack_events.handle("slack-conn", other_headers, other,
                               connections_table=table, publish=publish)[1] == {
        "accepted": False}
    bot, bot_headers = signed({"type": "event_callback", "team_id": "TWORKSPACE",
                               "event_id": "EvB", "event": {
                                   "type": "reaction_added", "user": "U1",
                                   "reaction": "tada", "bot_id": "B0T",
                                   "item": {"type": "message", "channel": "C1",
                                            "ts": "1758900012.000300"}}})
    assert slack_events.handle("slack-conn", bot_headers, bot,
                               connections_table=table, publish=publish)[1] == {
        "accepted": False}
    assert published == []
