"""slack action: post a templated message through a stored credential."""
import os

from ...connections import credentials
from . import base
from . import telegram_format
from .templating import render


def _token_for(action):
    credential_id = action.get("credential_id")
    if action.get("connection_id"):
        import boto3

        item = boto3.resource("dynamodb").Table(os.environ["CONNECTIONS_TABLE"]).get_item(
            Key={"connection_id": action["connection_id"]},
        ).get("Item") or {}
        credential_id = item.get("credential_id") or credential_id
    if not credential_id:
        raise ValueError("Slack action needs a connection_id or credential_id")
    secret = credentials.get_credential(credential_id)
    token = (secret.get("token") or secret.get("bot_token")
             or secret.get("user_token") or secret.get("SLACK_BOT_TOKEN"))
    if not token:
        raise ValueError("Slack secret does not contain a bot token")
    return token


def run_slack(action, event, *, steps=None):
    token = _token_for(action)
    if action.get("telegram_format"):
        return _post_telegram_format(action, event, token)
    data = event.get("data", {})
    text = render(action.get("text", "{title}\n{url}"), event, steps)
    result = base._json_request(
        "https://slack.com/api/chat.postMessage",
        {
            "channel": action["channel"],
            "text": text,
            "unfurl_links": action.get("unfurl_links", True),
            "unfurl_media": action.get("unfurl_media", True),
        },
        headers={"authorization": f"Bearer {token}"},
        timeout=action.get("timeout_seconds", 10),
    )
    if not result.get("ok"):
        raise RuntimeError(f"Slack rejected message: {result.get('error', 'unknown_error')}")
    return {"ok": True, "channel": result.get("channel"), "ts": result.get("ts")}


def _post_telegram_format(action, event, token):
    """Post a Telegram update as rendered Slack blocks (au-tomator behavior).

    The first message goes to the channel; anything that does not fit — plus
    the rendered ``source_link`` — continues in its thread. The body comes
    from the event's ``text``/``entities`` fields, not the ``text`` template:
    rendering the raw text through a template would drop the entity markup
    the blocks are built from.
    """
    data = event.get("data", {})
    text = data.get("text") or "no text in this post, see Telegram for the attachment"
    messages = telegram_format.build_messages(text, data.get("entities"))
    link = render(action.get("source_link", ""), event, steps=None).strip()

    channel = action["channel"]
    first = _post_blocks(token, action, channel, messages[0])
    thread_ts = first.get("ts")
    posted = 1
    for blocks in messages[1:]:
        _post_blocks(token, action, channel, blocks, thread_ts=thread_ts)
        posted += 1
    if link and thread_ts:
        _post_blocks(token, action, channel, telegram_format.link_blocks(link),
                     thread_ts=thread_ts)
        posted += 1
    return {"ok": True, "channel": channel, "ts": thread_ts, "messages": posted}


def _post_blocks(token, action, channel, blocks, thread_ts=None):
    message = {
        "channel": channel,
        "unfurl_links": False,
        "unfurl_media": False,
        "blocks": blocks,
    }
    if thread_ts is not None:
        message["thread_ts"] = thread_ts
    result = base._json_request(
        "https://slack.com/api/chat.postMessage",
        message,
        headers={"authorization": f"Bearer {token}"},
        timeout=action.get("timeout_seconds", 10),
    )
    if not result.get("ok"):
        raise RuntimeError(f"Slack rejected message: {result.get('error', 'unknown_error')}")
    return result
