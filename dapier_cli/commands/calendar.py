"""Implementation of the `dapier calendar` commands: a thin client over
``/api/agent/calendar/*``. Writes default Google's ``sendUpdates`` to
``none`` (guests are never emailed unless asked for) and support
``--preview``, which prints the exact request body without sending.
"""

from urllib.parse import urlencode

from .. import api
from .shared import preview_body, print_json

__all__ = [
    "calendar_accounts", "calendar_calendars", "calendar_events",
    "calendar_event", "calendar_freebusy", "calendar_write", "calendar_delete",
]

BASE = "/api/agent/calendar"


def _account_params(account, extra=None):
    params = dict(extra or {})
    if account:
        params["account"] = account
    return "?" + urlencode(params) if params else {}


def calendar_accounts(api_url, as_json=False, debug=False):
    data = api.call(api_url, "GET", BASE + "/accounts", debug=debug)
    accounts = data.get("accounts") or []
    if as_json:
        print_json(data)
        return 0
    if not accounts:
        print("No Google connections serve Calendar. Connect one first "
              "(dapier connections create).")
        return 0
    print(f"{'CONNECTION':<24} {'EMAIL':<36} TITLE / REFS")
    for item in accounts:
        refs = ", ".join(item.get("refs") or [])
        title = " / ".join(part for part in (item.get("title"), refs) if part)
        print(f"{str(item.get('connection_id') or ''):<24} "
              f"{str(item.get('email') or '(not signed in)'):<36} {title}")
    return 0


def calendar_calendars(api_url, account=None, max_results=25, page_token=None,
                       as_json=False, debug=False):
    params = {"max": int(max_results)}
    if account:
        params["account"] = account
    if page_token:
        params["page_token"] = page_token
    data = api.call(api_url, "GET", BASE + "/calendars?" + urlencode(params), debug=debug)
    calendars = data.get("calendars") or []
    if as_json:
        print_json(data)
        return 0
    print(f"{'ID':<48} {'SUMMARY':<32} TIMEZONE")
    for item in calendars:
        print(f"{str(item.get('id') or ''):<48} "
              f"{str(item.get('summary') or '')[:32]:<32} "
              f"{item.get('timezone') or ''}")
    print(f"({data.get('count') or 0} calendars)")
    if data.get("next_page_token"):
        print(f"next page: --page-token {data['next_page_token']}")
    return 0


def _event_line(item):
    start = item.get("start") or ""
    end = item.get("end") or ""
    flag = " (all-day)" if item.get("all_day") else ""
    recurring = " [recurring]" if item.get("recurrence") else ""
    return (f"{str(item.get('event_id') or ''):<28} {start:<25} {end:<25} "
            f"{(item.get('summary') or '(no summary)')[:40]}{flag}{recurring}")


def calendar_events(api_url, account=None, calendar=None, time_min=None, time_max=None,
                    query=None, max_results=25, page_token=None, as_json=False,
                    debug=False):
    params = {"time_min": time_min, "time_max": time_max, "max": int(max_results)}
    if account:
        params["account"] = account
    if calendar:
        params["calendar"] = calendar
    if query:
        params["q"] = query
    if page_token:
        params["page_token"] = page_token
    data = api.call(api_url, "GET", BASE + "/events?" + urlencode(params), debug=debug)
    events = data.get("events") or []
    if as_json:
        print_json(data)
        return 0
    print(f"{'ID':<28} {'START':<25} {'END':<25} SUMMARY")
    for item in events:
        print(_event_line(item))
    print(f"({data.get('count') or 0} events in {data.get('time_min')} … {data.get('time_max')})")
    if data.get("next_page_token"):
        print(f"next page: --page-token {data['next_page_token']}")
    return 0


def calendar_event(api_url, event_id, account=None, calendar=None, as_json=False,
                   debug=False):
    params = _account_params(account, {"calendar": calendar} if calendar else None)
    data = api.call(api_url, "GET", BASE + f"/events/{event_id}{params}", debug=debug)
    if as_json:
        print_json(data)
        return 0
    print(_event_line(data))
    if data.get("location"):
        print(f"Location: {data['location']}")
    if data.get("description"):
        print(f"Description: {data['description']}")
    attendees = ", ".join(str(a.get("email"))
                          for a in data.get("attendees") or [] if a.get("email"))
    if attendees:
        print(f"Attendees: {attendees}")
    if data.get("html_link"):
        print(f"Link: {data['html_link']}")
    return 0


def calendar_freebusy(api_url, account=None, calendar_ids=None, time_min=None,
                      time_max=None, as_json=False, debug=False):
    body = {"time_min": time_min, "time_max": time_max,
            "calendar_ids": calendar_ids or ["primary"]}
    if account:
        body["account"] = account
    data = api.call(api_url, "POST", BASE + "/freebusy", body=body, debug=debug)
    if as_json:
        print_json(data)
        return 0
    print(f"window {data.get('time_min')} … {data.get('time_max')}")
    for calendar_id, block in (data.get("calendars") or {}).items():
        print(f"{calendar_id}:")
        for span in block.get("busy") or []:
            print(f"  busy {span.get('start')} … {span.get('end')}")
        if not block.get("busy"):
            print("  free")
    return 0


def _write_body(account, calendar, send_updates, fields):
    body = {key: value for key, value in (fields or {}).items() if value}
    if account:
        body["account"] = account
    if calendar:
        body["calendar"] = calendar
    body["send_updates"] = send_updates or "none"
    return body


def calendar_write(api_url, method, event_id=None, account=None, calendar=None,
                   send_updates="none", fields=None, as_json=False, preview=False,
                   debug=False):
    """create (POST /events) and update (PATCH /events/{id}) share this path."""
    body = _write_body(account, calendar, send_updates, fields)
    if preview:
        return preview_body(body)
    path = BASE + "/events" if method == "POST" else BASE + f"/events/{event_id}"
    params = _account_params(account, {"calendar": calendar} if calendar else None)
    data = api.call(api_url, method, path + params, body=body, debug=debug)
    if as_json:
        print_json(data)
        return 0
    verb = "created" if method == "POST" else "updated"
    print(f"ok: event {data.get('event_id')} {verb} "
          f"({data.get('start')} … {data.get('end')}, sendUpdates "
          f"{body['send_updates']})")
    return 0


def calendar_delete(api_url, event_id, account=None, calendar=None,
                    send_updates="none", as_json=False, preview=False, debug=False):
    body = _write_body(account, calendar, send_updates, None)
    if preview:
        return preview_body({"event_id": event_id, **body})
    params = _account_params(account, {"calendar": calendar} if calendar else None)
    data = api.call(api_url, "DELETE", BASE + f"/events/{event_id}{params}",
                    body=body, debug=debug)
    if as_json:
        print_json(data)
        return 0
    print(f"ok: event {data.get('deleted')} deleted (sendUpdates {body['send_updates']})")
    return 0
