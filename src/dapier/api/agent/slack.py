"""Operator Slack proxy: DM open + history export via a named connection."""

import json
import re

from . import slack_ops
from .common import _LateBinding, _json_response

require_operator = _LateBinding("require_operator")
audit = _LateBinding("audit")

DEFAULT_CONNECTION_ID = slack_ops.DEFAULT_CONNECTION_ID


def _connection_id(event, body=None):
    query = event.get("queryStringParameters") or {}
    raw = None
    if isinstance(body, dict):
        raw = body.get("connection_id")
    raw = raw or query.get("connection_id") or DEFAULT_CONNECTION_ID
    return str(raw).strip().lower() or DEFAULT_CONNECTION_ID


def _error(exc):
    status = getattr(exc, "status", 400) or 400
    message = str(exc) or "Slack request failed"
    # Never echo credential material.
    if "xox" in message.lower():
        message = "Slack request failed"
    return _json_response(status, {"error": message})


def find_users_api(event):
    subject, error = require_operator(event, "slack.find_users")
    if error:
        return error
    query = (event.get("queryStringParameters") or {})
    try:
        payload = slack_ops.find_users(
            query.get("q") or query.get("query") or "",
            connection_id=_connection_id(event),
            limit=int(query.get("limit") or 50),
        )
    except (slack_ops.SlackOpsError, ValueError) as exc:
        return _error(exc)
    audit.emit(payload["connection"], "slack.find_users", subject, outcome="ok")
    return _json_response(200, payload)


def open_dm_api(event):
    subject, error = require_operator(event, "slack.open_dm")
    if error:
        return error
    try:
        body = json.loads(event.get("body") or "{}")
    except (ValueError, TypeError):
        return _json_response(400, {"error": "Invalid JSON body"})
    if not isinstance(body, dict):
        return _json_response(400, {"error": "Invalid JSON body"})
    try:
        payload = slack_ops.open_dm(
            body.get("user") or body.get("user_id") or "",
            connection_id=_connection_id(event, body),
        )
    except slack_ops.SlackOpsError as exc:
        return _error(exc)
    audit.emit(payload["connection"], "slack.open_dm", subject, outcome="ok")
    return _json_response(200, payload)


def history_api(event):
    subject, error = require_operator(event, "slack.history")
    if error:
        return error
    query = event.get("queryStringParameters") or {}
    try:
        payload = slack_ops.history(
            query.get("channel") or "",
            connection_id=_connection_id(event),
            limit=int(query.get("limit") or 100),
            cursor=query.get("cursor"),
            oldest=query.get("oldest"),
            latest=query.get("latest"),
        )
    except (slack_ops.SlackOpsError, ValueError) as exc:
        return _error(exc)
    audit.emit(payload["connection"], "slack.history", subject, outcome="ok")
    return _json_response(200, payload)


def dm_export_api(event):
    subject, error = require_operator(event, "slack.dm_export")
    if error:
        return error
    query = event.get("queryStringParameters") or {}
    try:
        payload = slack_ops.dm_export(
            query.get("user") or query.get("user_id") or query.get("q") or "",
            connection_id=_connection_id(event),
            limit=int(query.get("limit") or 200),
            cursor=query.get("cursor"),
        )
    except (slack_ops.SlackOpsError, ValueError) as exc:
        return _error(exc)
    audit.emit(payload["connection"], "slack.dm_export", subject, outcome="ok")
    return _json_response(200, payload)


def _simple(event, action, fn, build):
    subject, error = require_operator(event, action)
    if error:
        return error
    query = event.get("queryStringParameters") or {}
    try:
        payload = fn(**build(query, _connection_id(event)))
    except (slack_ops.SlackOpsError, ValueError) as exc:
        return _error(exc)
    audit.emit(payload["connection"], action, subject, outcome="ok")
    return _json_response(200, payload)


def post_api(event):
    subject, error = require_operator(event, "slack.post")
    if error:
        return error
    try:
        body = json.loads(event.get("body") or "{}")
    except (ValueError, TypeError):
        return _json_response(400, {"error": "Invalid JSON body"})
    if not isinstance(body, dict):
        return _json_response(400, {"error": "Invalid JSON body"})
    connection_id = str(
        body.get("connection_id") or slack_ops.DEFAULT_POST_CONNECTION_ID
    ).strip().lower() or slack_ops.DEFAULT_POST_CONNECTION_ID
    try:
        payload = slack_ops.post_message(
            body.get("channel") or "",
            body.get("text") or "",
            connection_id=connection_id,
            thread_ts=body.get("thread_ts"),
            unfurl_links=body.get("unfurl_links", True),
            unfurl_media=body.get("unfurl_media", True),
            dry_run=bool(body.get("dry_run", False)),
        )
    except (slack_ops.SlackOpsError, ValueError) as exc:
        return _error(exc)
    audit.emit(payload["connection"], "slack.post", subject, outcome="ok")
    return _json_response(200, payload)


def route_slack(event, method, path):
    """Return a response for Slack agent routes, or None if unmatched."""
    if path == "/api/agent/slack/users" and method == "GET":
        return find_users_api(event)
    if path == "/api/agent/slack/dm" and method == "POST":
        return open_dm_api(event)
    if path == "/api/agent/slack/history" and method == "GET":
        return history_api(event)
    if path == "/api/agent/slack/dm-export" and method == "GET":
        return dm_export_api(event)
    if path == "/api/agent/slack/post" and method == "POST":
        return post_api(event)
    if path == "/api/agent/slack/replies" and method == "GET":
        return _simple(event, "slack.replies", slack_ops.replies, lambda q, c: {
            "channel": q.get("channel") or "", "thread_ts": q.get("thread_ts") or "",
            "connection_id": c, "limit": int(q.get("limit") or 200),
            "cursor": q.get("cursor")})
    if path == "/api/agent/slack/channels" and method == "GET":
        return _simple(event, "slack.channels", slack_ops.channels, lambda q, c: {
            "connection_id": c, "limit": int(q.get("limit") or 200),
            "cursor": q.get("cursor")})
    if path == "/api/agent/slack/search" and method == "GET":
        return _simple(event, "slack.search", slack_ops.search, lambda q, c: {
            "query": q.get("q") or q.get("query") or "", "connection_id": c,
            "count": int(q.get("count") or 20), "page": int(q.get("page") or 1)})
    return None
