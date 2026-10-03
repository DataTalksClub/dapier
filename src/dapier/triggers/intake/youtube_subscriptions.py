"""WebSub subscriptions for the channels workflow items watch.

YouTube pushes new uploads over WebSub: the hub POSTs an Atom feed to
/hooks/youtube, signed with the shared hub secret. A channel gets watched
two ways: a designer save/toggle/delete syncs the hub right away
(``reconcile``, called best-effort from the designer store), and this
schedule re-subscribes everything the stored workflows watch, healing
hub-side drift or a subscription lost while the callback was unreachable.

There is no global channel list: each YouTube workflow item names its own
channel(s) in the trigger filters — ``channel_id: {equals: ID}`` for one
channel or ``channel_id: {in: [ID, ...]}`` for several. A channel nobody
watches is simply not subscribed.
"""

import json
import logging
import os
import urllib.parse
import urllib.error
import urllib.request

import boto3

from ... import engine

logger = logging.getLogger(__name__)


HUB_URL = "https://pubsubhubbub.appspot.com/subscribe"
TIMEOUT = 15


def topic_url(channel_id):
    """The hub topic for a channel: its videos-only Atom feed."""
    return f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"


def channels_of(workflow):
    """Channel IDs one workflow watches (enabled youtube triggers only).

    Reads every trigger spec — the single ``trigger`` (designer workflows)
    and the ``triggers`` list (stored hook triggers merge into the engine in
    the list form) — so the renewal schedule re-subscribes channels watched
    by either kind.
    """
    if not workflow or not workflow.get("enabled", True):
        return []
    triggers = workflow.get("triggers")
    if isinstance(triggers, list) and triggers:
        specs = [trigger for trigger in triggers if isinstance(trigger, dict)]
    else:
        trigger = workflow.get("trigger")
        specs = [trigger] if isinstance(trigger, dict) else []
    channels = []
    for trigger in specs:
        if trigger.get("connector") != "youtube":
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


def channels_from_workflows(items):
    """Channel IDs named by youtube triggers, deduplicated in first-seen order."""
    channels = []
    for workflow in items:
        for channel_id in channels_of(workflow):
            if channel_id not in channels:
                channels.append(channel_id)
    return channels


def _settings():
    """(callback_url, hub secret); RuntimeError when the deployment runs
    without the push path (the schedule no-ops for the same reason)."""
    callback_url = os.environ.get("YOUTUBE_CALLBACK_URL", "").strip()
    secret_id = os.environ.get("YOUTUBE_WEBHOOK_SECRET_ID", "").strip()
    if not callback_url or not secret_id:
        raise RuntimeError("YouTube webhook push is not configured")
    secret = boto3.client("secretsmanager").get_secret_value(SecretId=secret_id)["SecretString"]
    return callback_url, secret


def hub_request(mode, channel_id, *, callback_url, secret, timeout=TIMEOUT,
                transport=None):
    """POST one subscribe/unsubscribe request to the hub; returns the status.

    ``transport`` is the injectable network seam
    (``(method, url, *, headers, body, timeout) -> (status, raw)``) the
    save-time path passes through; without it the request goes over urllib,
    like the schedule's.
    """
    payload = urllib.parse.urlencode({
        "hub.callback": callback_url,
        "hub.topic": topic_url(channel_id),
        "hub.mode": mode,
        "hub.verify": "async",
        "hub.secret": secret,
    }).encode()
    if transport is not None:
        status, _raw = transport("POST", HUB_URL,
                                 headers={"content-type": "application/x-www-form-urlencoded"},
                                 body=payload, timeout=timeout)
        if status >= 300:
            # urllib raises HTTPError for these; the seam must fail the same
            # way so callers see one failure shape.
            raise RuntimeError(f"YouTube hub answered HTTP {status}")
        return status
    request = urllib.request.Request(HUB_URL, data=payload, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status


def subscribe_channel(channel_id, *, transport=None):
    """Subscribe one channel right now — the save-time path behind the
    schedule's cycle (a freshly saved youtube trigger must not wait days
    for its first renewal). Returns the hub status; raises when the push
    path is not configured or the hub call fails, so callers decide between
    a save warning and a logged no-op."""
    callback_url, secret = _settings()
    return hub_request("subscribe", channel_id, callback_url=callback_url,
                       secret=secret, transport=transport)


def handler(_event, _context):
    channel_ids = channels_from_workflows(engine.all_workflows())
    if not channel_ids:
        return {"statusCode": 200, "body": json.dumps({"subscriptions": []})}
    callback_url, secret = _settings()
    results = []
    for channel_id in channel_ids:
        status = hub_request("subscribe", channel_id, callback_url=callback_url, secret=secret)
        results.append({"channel_id": channel_id, "status": status})
    logger.info("YouTube renewal completed: channels=%d accepted=%d",
                len(results), sum(item["status"] == 202 for item in results))
    return {"statusCode": 200, "body": json.dumps({"subscriptions": results})}


def reconcile(previous_workflow, workflow):
    """Sync the hub after a designer save/toggle/delete; best-effort.

    Subscribes the channels the saved workflow newly watches and
    unsubscribes the ones it stopped watching (or that vanish with it on
    delete) — but only channels no other enabled workflow still names, and
    only the delta: an unchanged channel list never rings the hub. A
    deployment without the push path (no callback URL or secret id) is a
    silent no-op, like the schedule. Returns warning strings for the save
    response; never raises.
    """
    workflow_id = str((workflow or previous_workflow or {}).get("id") or "")
    before = channels_from_workflows([previous_workflow] if previous_workflow else [])
    after = channels_from_workflows([workflow] if workflow else [])
    if before == after:
        return []
    try:
        callback_url, secret = _settings()
    except RuntimeError:
        return []
    except Exception as exc:
        return [f"YouTube webhook sync failed: {exc}"]
    try:
        others = channels_from_workflows(
            item for item in engine.all_workflows()
            if str(item.get("id") or "") != workflow_id)
    except Exception as exc:
        # The live set only guards unsubscription from channels other
        # workflows still watch. Unreadable, the unsubscribe half cannot be
        # proven safe, so only the always-safe subscribe half runs — an
        # orphaned subscription is inert (its notifications match no
        # workflow) and the renewal schedule re-subscribes what a partial
        # sync missed. Never the save's problem.
        logger.warning("youtube reconcile: could not list live workflows (%s); "
                       "skipping unsubscribe", exc)
        others = None
    warnings = []
    for channel_id in after:
        if channel_id in before or (others is not None and channel_id in others):
            continue
        try:
            hub_request("subscribe", channel_id, callback_url=callback_url, secret=secret)
        except Exception as exc:
            warnings.append(f"YouTube subscribe for channel {channel_id} failed: {exc}")
    if others is None:
        return warnings
    for channel_id in before:
        if channel_id in after or channel_id in others:
            continue
        try:
            hub_request("unsubscribe", channel_id, callback_url=callback_url, secret=secret)
        except Exception as exc:
            warnings.append(f"YouTube unsubscribe for channel {channel_id} failed: {exc}")
    return warnings


def subscription_status(channel_id, *, transport=None):
    """Read the hub's authenticated diagnostic without exposing its secret URL."""
    import re
    from datetime import datetime, timezone
    from html.parser import HTMLParser
    from email.utils import parsedate_to_datetime

    if not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", channel_id):
        raise ValueError("a valid YouTube channel ID is required")
    callback, secret = _settings()
    topic = topic_url(channel_id)
    url = "https://pubsubhubbub.appspot.com/subscription-details?" + urllib.parse.urlencode({
        "hub.callback": callback, "hub.topic": topic, "hub.secret": secret,
    })
    try:
        if transport:
            status, raw = transport("GET", url, headers={}, body=None, timeout=TIMEOUT)
        else:
            with urllib.request.urlopen(url, timeout=TIMEOUT) as response:
                status, raw = response.status, response.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"YouTube hub diagnostic returned HTTP {exc.code}") from None
    except Exception:
        raise RuntimeError("YouTube hub diagnostic unreachable") from None
    if status >= 300:
        raise RuntimeError(f"YouTube hub diagnostic returned HTTP {status}")

    class VisibleText(HTMLParser):
        def __init__(self):
            super().__init__()
            self.tokens = []
            self.hidden = 0

        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style"):
                self.hidden += 1

        def handle_endtag(self, tag):
            if tag in ("script", "style"):
                self.hidden = max(0, self.hidden - 1)

        def handle_data(self, data):
            if not self.hidden and data.strip():
                self.tokens.append(data.strip())

    parser = VisibleText()
    parser.feed(raw.decode("utf-8", errors="replace"))
    state, expiry = "unknown", None
    for index, token in enumerate(parser.tokens[:-1]):
        label = token.lower().strip(" :")
        value = " ".join(parser.tokens[index + 1:index + 4])
        if label in ("state", "subscription state", "subscription status"):
            match = re.match(r"(verified|unverified|active|expired|pending|deleted)\b", value, re.I)
            if match:
                state = match[1].lower()
        if "expir" in label and len(label) < 60:
            match = re.search(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?", value)
            if match:
                stamp = datetime.fromisoformat(match[0].replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                expiry = stamp.astimezone(timezone.utc).isoformat()
            else:
                # The live hub diagnostic formats dates as RFC 2822, not ISO.
                match = re.search(r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun), \d{1,2} (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) \d{4} \d{2}:\d{2}:\d{2} (?:[+-]\d{4}|GMT|UTC)", value)
                if match:
                    try:
                        stamp = parsedate_to_datetime(match[0])
                        expiry = stamp.astimezone(timezone.utc).isoformat()
                    except (ValueError, OverflowError):
                        pass
    active = False if state in ("expired", "deleted", "unverified") else None if state == "unknown" or expiry is None else (
        state in ("verified", "active") and datetime.fromisoformat(expiry) > datetime.now(timezone.utc))
    return {"active": active, "state": state, "expires_at": expiry,
            "topic": topic, "callback": callback}


def renew_subscription(channel_id):
    """Renew only a channel already watched by an enabled workflow."""
    if channel_id not in channels_from_workflows(engine.all_workflows()):
        raise ValueError("channel is not watched by an enabled YouTube workflow")
    status = subscribe_channel(channel_id)
    return {"accepted": status == 202, "status": status, "topic": topic_url(channel_id)}
