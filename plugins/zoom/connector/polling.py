"""The ``zoom.recordings`` poll source and the poll-aware sample pull.

``recording.completed`` fires from Zoom's webhooks (triggers.intake.zoom_
webhooks) — but those need a Zoom app with the Event Subscription and the
signed endpoint validated. The ``zoom.recordings`` source is the no-app-
config path: a stored poll trigger lists the connected account's cloud
recordings on the schedule machinery (triggers.poll_sources) and publishes
the same ``zoom``/``recording.completed`` events, so a workflow matches the
same chip either way.
"""
import urllib.parse
from datetime import datetime, timedelta, timezone

from src.dapier.connections import discovery as provider
from src.dapier.triggers.poll_sources import PollSource, register_source
from src.dapier.connectors import trigger_discovery  # noqa: F401  (as_sample in _fetch_zoom_sample)
from src.dapier.connectors.trigger_discovery import (
    DEFAULT_LIMIT,
    TriggerDiscovery,
    register_trigger_discovery,
)
from .samples import _PER_EVENT_ZOOM_SAMPLE

# --- poll source: "New Recording" on the poll-trigger schedule ---------------
#
# recording.completed fires from Zoom's webhooks (triggers.intake.zoom_
# webhooks) — but those need a Zoom app with the Event Subscription and the
# signed endpoint validated. The ``zoom.recordings`` source is the no-app-
# config path: a stored poll trigger lists the connected account's cloud
# recordings on the schedule machinery (triggers.poll_sources) and publishes
# the same ``zoom``/``recording.completed`` events, so a workflow matches the
# same chip either way. Like the webhook path, only recordings carrying a
# video file (MP4/M4V) fire, and the first fire seeds the cursor without
# emitting — enabling a trigger must not fire the last month of history.

ZOOM_POLL_PAGE_SIZE = 100
ZOOM_POLL_PAGES = 3  # ~300 recordings per fire, the discovery listing's order
ZOOM_POLL_LOOKBACK_DAYS = 30
# The video-file filter recording.completed publishes (zoom_webhooks).
ZOOM_POLL_FILE_TYPES = frozenset({"MP4", "M4V"})


def _zoom_poll_validate(body):
    """Save-time fetch spec: the ``connection_id`` of the Zoom connection to
    poll as (required — the fetch refreshes its OAuth token), the optional
    ``for_email`` mailbox (default ``me`` — the connection's own user),
    plus the fetch defaults every stored zoom poll carries."""
    from src.dapier.triggers.email_triggers import TriggerError

    body = body if isinstance(body, dict) else {}
    if not str(body.get("connection_id") or "").strip():
        raise TriggerError(
            "connection_id is required: pick the Zoom connection to poll as")
    return {
        "for_email": str(body.get("for_email") or "").strip() or "me",
        "cursor_mode": "next_cursor",
        "id_path": "id",
        "url": "",
    }


def _recording_video_files(meeting):
    """The recording's MP4/M4V files' metadata — the video filter
    ``recording.completed`` publishes (zoom_webhooks)."""
    return [
        {
            "id": file.get("id"),
            "file_type": file.get("file_type"),
            "recording_type": file.get("recording_type"),
            "file_size": file.get("file_size"),
            "play_url": file.get("play_url"),
            "download_url": file.get("download_url"),
        }
        for file in (meeting.get("recording_files") or [])
        if isinstance(file, dict)
        and str(file.get("file_type", "")).upper() in ZOOM_POLL_FILE_TYPES
    ]


def _zoom_poll_item(meeting):
    """One recording flattened to the shape the ``recording.completed``
    webhook publishes (``zoom_webhooks.recording_data``): meeting identity,
    topic, start time, share URL, and the video files' metadata with their
    download URLs — so downstream templates read the same keys no matter
    which path fired. Non-video-only recordings return None, mirroring the
    webhook's "at least one MP4 or M4V" rule.

    ``id`` is the watermark key: start time and recording uuid composed, so
    ISO text comparison orders recordings chronologically while the uuid
    keeps same-minute recordings distinct (the s3 source composes
    ``last_modified|key`` for the same reason).
    """
    videos = _recording_video_files(meeting)
    if not videos:
        return None
    uuid = str(meeting.get("uuid") or "")
    start_time = str(meeting.get("start_time") or "")
    return {
        "id": f"{start_time}|{uuid}",
        "meeting_id": str(meeting.get("id")),
        "meeting_uuid": uuid,
        "topic": meeting.get("topic"),
        "host_id": meeting.get("host_id"),
        "start_time": start_time,
        "share_url": meeting.get("share_url"),
        "download_url": videos[0].get("download_url") or "",
        "video_files": videos,
    }


def _zoom_poll_recordings(token, for_email, *, transport=None):
    """The mailbox's cloud recordings from the last
    ZOOM_POLL_LOOKBACK_DAYS days, following continuation pages up to
    ZOOM_POLL_PAGES through the provider's shared request path."""
    today = datetime.now(timezone.utc).date()
    params = {
        "from": (today - timedelta(days=ZOOM_POLL_LOOKBACK_DAYS)).isoformat(),
        "to": today.isoformat(),
        "per_page": ZOOM_POLL_PAGE_SIZE,
    }
    meetings = []
    for _page in range(ZOOM_POLL_PAGES):
        url = (f"{provider.ZOOM_API_URL}/users/{urllib.parse.quote(for_email, safe='')}"
               "/recordings?" + urllib.parse.urlencode(params))
        data = provider._request("GET", url, token, None, transport=transport)
        page = [entry for entry in data.get("meetings") or []
                if isinstance(entry, dict) and entry.get("uuid")]
        meetings.extend(page)
        next_page = data.get("next_page_token")
        if not page or not next_page:
            return meetings
        params = {**params, "next_page_token": next_page}
    return meetings


def _list_recordings(item, *, transport=None):
    """The connection's cloud recordings as poll items, oldest first.

    Lists through Zoom's ``/users/{for_email}/recordings`` with the bearer
    token from ``poll_triggers._bearer_token``, so the connection's OAuth
    token is refreshed exactly like the classic fetch. Raises
    ``RuntimeError`` on a failed fetch, like every poll source.
    """
    from src.dapier.triggers import poll_triggers

    for_email = str(item.get("for_email") or "").strip() or "me"
    token = poll_triggers._bearer_token(item["connection_id"])
    try:
        meetings = _zoom_poll_recordings(token, for_email, transport=transport)
    except provider.DiscoveryError as exc:
        raise RuntimeError(f"zoom poll failed: {exc}") from None
    return sorted(
        (entry for entry in (_zoom_poll_item(meeting) for meeting in meetings)
         if entry is not None),
        key=lambda entry: entry["id"])


def _zoom_poll_fetch(item, cursor=None, *, transport=None):
    """One poll page as ``(items, next_cursor)`` for ``triggers.poll_sources``.

    The cursor is the composite ``start_time|uuid`` watermark, which text
    comparison orders chronologically. With no stored cursor — the first
    fire after enabling — the fetch only seeds the watermark at the newest
    recording already in the lookback window and emits nothing. With a
    cursor, only recordings strictly past it fire, and the parked cursor is
    the last fired recording (the incoming one when nothing qualifies;
    ``fire`` parks it only once the page drains). The 30-day window
    deliberately overlaps between fires: the cursor filters the re-listed
    history and the seen store dedupes what slips past it.
    """
    if not str(item.get("connection_id") or "").strip():
        raise RuntimeError("poll source 'zoom.recordings' needs connection_id: "
                           "the Zoom connection to poll as")
    recordings = _list_recordings(item, transport=transport)
    if cursor is None:
        # First fire: seed the watermark at the newest recording already in
        # the window (None while the account has none) without emitting.
        return [], (recordings[-1]["id"] if recordings else None)
    watermark = str(cursor)
    fresh = [entry for entry in recordings if entry["id"] > watermark]
    return fresh, (fresh[-1]["id"] if fresh else watermark)


def _zoom_poll_view(item):
    """The provider params ``public_view`` shows beside ``source``."""
    return {"for_email": item.get("for_email")}


register_source(PollSource(
    name="zoom.recordings", connector="zoom", event="recording.completed",
    label="Zoom", validate=_zoom_poll_validate,
    fetch=_zoom_poll_fetch, view=_zoom_poll_view))


# --- trigger samples, poll-aware ---------------------------------------------------

def _stored_zoom_poll(name):
    """The stored poll trigger named by ``event`` when it watches Zoom
    recordings, or None. Poll ids cannot contain dots and event names do, so
    a per-event ask never matches a poll; a missing selector, unconfigured
    poll triggers, an unknown name and a non-zoom source (the generic poll
    connector owns those) fold together: the caller only distinguishes
    live-vs-fallback, so any storage hiccup folds too — sampling never
    raises for want of infrastructure (see docs/connector-coverage-audit.md)."""
    from src.dapier.triggers import poll_triggers

    name = str(name or "").strip().lower()
    if not name or "." in name:
        return None
    try:
        item = poll_triggers.get_item(name)
    except Exception:
        return None
    if not item or str(item.get("source") or "") != "zoom.recordings":
        return None
    return item


# Older than any composite recording id: the chip's live sample pulls
# against this watermark, so the newest recording in the window answers
# before the trigger is even past its first (seeding) fire.
_ZOOM_EPOCH_CURSOR = "0000-01-01T00:00:00|"


def _fetch_zoom_sample(event=None, connection_id=None, limit=DEFAULT_LIMIT):
    """The Zoom chip's sample pull. An ``event`` naming a stored
    zoom.recordings poll pulls live through the poll's own fetch against the
    epoch watermark — no stored cursor is read or advanced — and wraps the
    newest recording in the envelope a real fire would publish. Anything
    else — a per-event ask, no stored poll yet, or a live fetch that cannot
    run (no connection, an unreachable Zoom) — falls through to the
    per-event chain: the newest recorded zoom run carrying the asked event,
    else the documented webhook example. A sample pull shows the payload
    shape, it never raises."""
    from src.dapier.triggers import poll_triggers

    item = _stored_zoom_poll(event)
    if item is not None:
        try:
            recordings, _next_cursor = _zoom_poll_fetch(item, _ZOOM_EPOCH_CURSOR)
            envelope = (poll_triggers.event_for(item, recordings[-1])
                        if recordings else None)
        except Exception:
            envelope = None
        if envelope is not None:
            return {
                "sample": trigger_discovery.as_sample(envelope),
                "source": "live",
                "connection_id": item.get("connection_id") or None,
            }
    return _PER_EVENT_ZOOM_SAMPLE(event=event, connection_id=connection_id,
                                  limit=limit)


register_trigger_discovery(TriggerDiscovery(
    connector="zoom", label="Zoom", kind="sample", resource="",
    fetch=_fetch_zoom_sample))
