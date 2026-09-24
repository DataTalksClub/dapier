"""Renew WebSub subscriptions for the channels workflow items watch.

There is no global channel list: each YouTube workflow item names its own
channel(s) in the trigger filters — ``channel_id: {equals: ID}`` for one
channel or ``channel_id: {in: [ID, ...]}`` for several. The renewal schedule
subscribes exactly the channels the configured items name, so watching a
channel means editing (or adding) a workflow item, and a channel nobody
watches is simply not subscribed.
"""

import json
import os
import urllib.parse
import urllib.request

import boto3

from ... import engine


HUB_URL = "https://pubsubhubbub.appspot.com/subscribe"


def channels_from_workflows(items):
    """Channel IDs named by youtube triggers, deduplicated in first-seen order."""
    channels = []
    for workflow in items:
        trigger = workflow.get("trigger") or {}
        if not workflow.get("enabled", True) or trigger.get("connector") != "youtube":
            continue
        rule = (trigger.get("filters") or {}).get("channel_id")
        if isinstance(rule, dict):
            values = rule.get("in") if isinstance(rule.get("in"), list) else [rule.get("equals")]
        else:
            values = [rule]
        for value in values:
            channel_id = str(value or "").strip()
            if channel_id and channel_id not in channels:
                channels.append(channel_id)
    return channels


def handler(_event, _context):
    channel_ids = channels_from_workflows(engine.all_workflows())
    if not channel_ids:
        return {"statusCode": 200, "body": json.dumps({"subscriptions": []})}
    secret = boto3.client("secretsmanager").get_secret_value(
        SecretId=os.environ["YOUTUBE_WEBHOOK_SECRET_ID"]
    )["SecretString"]
    results = []
    for channel_id in channel_ids:
        payload = urllib.parse.urlencode({
            "hub.callback": os.environ["YOUTUBE_CALLBACK_URL"],
            "hub.topic": f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}",
            "hub.mode": "subscribe",
            "hub.verify": "async",
            "hub.secret": secret,
        }).encode()
        request = urllib.request.Request(HUB_URL, data=payload, method="POST")
        with urllib.request.urlopen(request, timeout=15) as response:
            results.append({"channel_id": channel_id, "status": response.status})
    return {"statusCode": 200, "body": json.dumps({"subscriptions": results})}
