"""Implementations of the `dapier runs events` (inbox) commands."""

import json
from urllib.parse import quote
from urllib.parse import urlencode

from .. import api

__all__ = ["email_line", "inbox_list", "inbox_replay", "inbox_show", "outcome_text",
           "print_inbox"]


def outcome_text(item):
    """What happened to an event, in the same words the console's Emails
    page uses: who handled it, or why nothing did."""
    outcome = item.get("outcome") or item.get("status") or ""
    workflows = [run.get("workflow") for run in item.get("runs") or [] if run.get("workflow")]
    workflows = workflows or list(item.get("matched") or [])
    if outcome == "handled":
        return f"handled by {', '.join(workflows)}" if workflows else "handled"
    if outcome == "failed":
        error = " ".join(str(item.get("error") or "").split())
        return f"failed: {error}" if error else "failed"
    if outcome == "refused":
        return "refused: sender not allowed"
    if outcome == "unmatched":
        return "no workflow"
    if outcome == "pending":
        return "processing"
    return outcome or "-"


def email_line(email):
    """One line of envelope metadata for an email row: sender, recipient,
    subject — or the feedback kind for a bounce/complaint."""
    if not email:
        return ""
    if email.get("feedback"):
        recipients = ", ".join(email.get("recipients") or []) or "-"
        return f"{email['feedback']} for {recipients}"
    sender = email.get("from_address") or email.get("from") or "-"
    to = ", ".join(email.get("to") or []) or "-"
    subject = email.get("subject") or "(no subject)"
    return f"from {sender} to {to}: {subject}"


def print_inbox(items):
    print(f"{'RECEIVED':19}  {'CONNECTOR':10} {'OUTCOME':40} INBOX ID")
    for item in items:
        received = (item.get("received_at") or "-")[:19].replace("T", " ")
        print(f"{received:19}  {item.get('connector') or '-':10} "
              f"{outcome_text(item)[:40]:40} {item.get('inbox_id', '')}")
        line = email_line(item.get("email"))
        if line:
            print(f"{'':21}{line[:110]}")


def inbox_list(api_url, connector=None, limit=25, next_token=None, outcome=None,
               debug=False):
    params = {"limit": int(limit)}
    if connector:
        params["connector"] = connector
    if outcome:
        params["outcome"] = outcome
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
    print(f"  outcome: {outcome_text(event)}")
    for run in event.get("runs") or []:
        print(f"  run: {run.get('run_id')} ({run.get('status') or 'status unknown'})")
    email = event.get("email") or {}
    for key in ("from", "to", "cc", "subject", "date", "message_id", "route",
                "feedback", "bounce_type", "recipients"):
        value = email.get(key)
        if value not in (None, "", []):
            print(f"  email.{key}: {', '.join(value) if isinstance(value, list) else value}")
    for attachment in email.get("attachments") or []:
        size = attachment.get("size")
        size_text = f" ({size} bytes)" if size is not None else ""
        print(f"  attachment: {attachment.get('name') or '(unnamed)'}{size_text}")
    if event.get("data") not in (None, {}, []):
        print(f"  data: {json.dumps(event['data'], sort_keys=True, default=str)}")
    return 0


def inbox_replay(api_url, inbox_id, debug=False):
    data = api.call(api_url, "POST", f"/api/agent/triggers/inbox/{quote(inbox_id, safe='')}/replay", body={}, debug=debug)
    print(f"Replay accepted for {data.get('replayed_from') or inbox_id}.")
    print(f"The re-injected event ({data.get('run_id') or 'pending'}) appears in `dapier runs list` "
          "once the worker picks it up.")
    return 0


