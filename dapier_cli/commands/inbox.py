"""Implementations of the `dapier runs events` (inbox) commands."""

import json
from urllib.parse import quote
from urllib.parse import urlencode

from .. import api

__all__ = ["inbox_list", "inbox_replay", "inbox_show", "print_inbox"]


def print_inbox(items):
    print(f"{'INBOX ID':44} {'CONNECTOR':10} {'STATUS':10} RECEIVED")
    for item in items:
        received = (item.get("received_at") or "-")[:19]
        print(f"{item.get('inbox_id', ''):44} {item.get('connector') or '-':10} "
              f"{item.get('status', ''):10} {received}")


def inbox_list(api_url, connector=None, limit=25, next_token=None, debug=False):
    params = {"limit": int(limit)}
    if connector:
        params["connector"] = connector
    if next_token:
        params["next"] = next_token
    data = api.call(api_url, "GET",
                    f"/api/agent/triggers/inbox?{urlencode(params)}", debug=debug)
    items = data.get("events", [])
    if not items:
        print("Inbox is empty. Every trigger event lands here once the worker picks it up — "
              "matched or not.")
        return 0
    print_inbox(items)
    next_page = (data.get("paging") or {}).get("next")
    if next_page:
        print(f"\nnext page: {next_page}  (pass it to --next)")
    return 0


def inbox_show(api_url, inbox_id, debug=False):
    data = api.call(api_url, "GET", f"/api/agent/triggers/inbox/{quote(inbox_id, safe='')}", debug=debug)
    event = data.get("event") or {}
    print(f"inbox event: {event.get('inbox_id') or inbox_id}")
    for key in ("connector", "event", "source", "status", "occurred_at", "received_at",
                "processed_at", "matched", "error"):
        if event.get(key) not in (None, "", []):
            print(f"  {key}: {event[key]}")
    if event.get("data") not in (None, {}, []):
        print(f"  data: {json.dumps(event['data'], sort_keys=True, default=str)}")
    return 0


def inbox_replay(api_url, inbox_id, debug=False):
    data = api.call(api_url, "POST", f"/api/agent/triggers/inbox/{quote(inbox_id, safe='')}/replay", body={}, debug=debug)
    print(f"Replay accepted for {data.get('replayed_from') or inbox_id}.")
    print(f"The re-injected event ({data.get('run_id') or 'pending'}) appears in `dapier runs list` "
          "once the worker picks it up.")
    return 0


