import json
import os
import urllib.parse
import urllib.request

import boto3


HUB_URL = "https://pubsubhubbub.appspot.com/subscribe"


def handler(_event, _context):
    channel_ids = [value.strip() for value in os.environ.get("YOUTUBE_CHANNEL_IDS", "").split(",") if value.strip()]
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
