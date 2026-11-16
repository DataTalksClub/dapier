"""Implementation of the `dapier slack` commands."""

from urllib.parse import urlencode

from .. import api
from .shared import print_json

__all__ = [
    "slack_find_user", "slack_open_dm", "slack_history", "slack_dm_export",
    "slack_replies", "slack_channels", "slack_search",
]

DEFAULT_CONNECTION_ID = "slack-alexey"


def slack_find_user(api_url, query, connection_id=DEFAULT_CONNECTION_ID, limit=50, debug=False):
    path = "/api/agent/slack/users?" + urlencode({
        "q": query,
        "connection_id": connection_id,
        "limit": int(limit),
    })
    data = api.call(api_url, "GET", path, debug=debug)
    items = data.get("items") or []
    if not items:
        print("No matching users.")
        return 0
    print(f"{'ID':<14} {'NAME':<28} {'HANDLE':<20} EMAIL")
    for item in items:
        print(
            f"{str(item.get('id') or ''):<14} "
            f"{str(item.get('name') or '')[:28]:<28} "
            f"{str(item.get('handle') or '')[:20]:<20} "
            f"{item.get('email') or ''}"
        )
    return 0


def slack_open_dm(api_url, user, connection_id=DEFAULT_CONNECTION_ID, debug=False):
    data = api.call(
        api_url, "POST", "/api/agent/slack/dm",
        body={"user": user, "connection_id": connection_id}, debug=debug)
    print_json(data)
    return 0


def slack_history(api_url, channel, connection_id=DEFAULT_CONNECTION_ID, limit=100,
                  cursor=None, oldest=None, latest=None, debug=False):
    params = {
        "channel": channel,
        "connection_id": connection_id,
        "limit": int(limit),
    }
    if cursor:
        params["cursor"] = cursor
    if oldest:
        params["oldest"] = oldest
    if latest:
        params["latest"] = latest
    data = api.call(api_url, "GET", "/api/agent/slack/history?" + urlencode(params), debug=debug)
    print_json(data)
    return 0


def slack_dm_export(api_url, user, connection_id=DEFAULT_CONNECTION_ID, limit=200,
                    cursor=None, debug=False):
    params = {
        "user": user,
        "connection_id": connection_id,
        "limit": int(limit),
    }
    if cursor:
        params["cursor"] = cursor
    data = api.call(api_url, "GET", "/api/agent/slack/dm-export?" + urlencode(params), debug=debug)
    print_json(data)
    return 0


def _get(api_url, path, params, debug):
    params = {k: v for k, v in params.items() if v not in (None, "")}
    print_json(api.call(api_url, "GET", path + "?" + urlencode(params), debug=debug))
    return 0


def slack_replies(api_url, channel, thread_ts, connection_id=DEFAULT_CONNECTION_ID,
                  limit=200, cursor=None, debug=False):
    return _get(api_url, "/api/agent/slack/replies", {
        "channel": channel, "thread_ts": thread_ts, "connection_id": connection_id,
        "limit": int(limit), "cursor": cursor}, debug)


def slack_channels(api_url, connection_id=DEFAULT_CONNECTION_ID, limit=200, cursor=None,
                   debug=False):
    return _get(api_url, "/api/agent/slack/channels", {
        "connection_id": connection_id, "limit": int(limit), "cursor": cursor}, debug)


def slack_search(api_url, query, connection_id=DEFAULT_CONNECTION_ID, count=20, page=1,
                 debug=False):
    return _get(api_url, "/api/agent/slack/search", {
        "q": query, "connection_id": connection_id, "count": int(count),
        "page": int(page)}, debug)
