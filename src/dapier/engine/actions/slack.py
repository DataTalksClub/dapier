"""slack action: post a templated message through a stored credential."""
import os

from ...connections import credentials
from . import base


def run_slack(action, event):
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
    data = event.get("data", {})
    template = action.get("text", "{title}\n{url}")
    text = template.format_map(base._SafeFormat(data))
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
