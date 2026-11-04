"""The `dapier calendar` noun: calendars, bounded event windows, free/busy,
and event writes with explicit guest-notification behavior over
/api/agent/calendar/*."""

from .. import commands

GROUPS = ("calendar",)

EPILOG = """examples:
  dapier calendar accounts                        # list Google accounts serving Calendar
  dapier calendar calendars --account me@gmail.com
  dapier calendar events --time-min 2026-11-04T00:00:00Z --time-max 2026-11-11T00:00:00Z --account me@gmail.com
  dapier calendar freebusy --calendar primary --calendar work@group.calendar.google.com \\
      --time-min 2026-11-04T00:00:00Z --time-max 2026-11-05T00:00:00Z
  dapier calendar create --summary "Sync" --start 2026-11-05T09:00:00Z \\
      --end 2026-11-05T10:00:00Z --attendee peer@example.com
  dapier calendar create ... --send-updates all --account me@gmail.com  # default none: no guest email
  dapier calendar create ... --preview --account me@gmail.com  # review the JSON, then run without --preview
  dapier calendar delete EVENT_ID --account me@gmail.com
--account is required on every operation except accounts; use the verified
email or connection id. Event windows are bounded
(<= 366 days). Writes never invite guests unless --send-updates says so;
--preview prints the exact request body without sending anything."""

COMMANDS = ("accounts", "calendars", "events", "event", "freebusy",
            "create", "update", "delete")


def _write_args(p):
    p.add_argument("--calendar", default=None, help="Calendar id (default primary)")
    p.add_argument("--account", required=True)
    p.add_argument("--summary", default=None)
    p.add_argument("--start", default=None, help="ISO 8601 start (or date for all-day)")
    p.add_argument("--end", default=None, help="ISO 8601 end (or date for all-day)")
    p.add_argument("--timezone", default=None, help="IANA timezone for start/end")
    p.add_argument("--description", default=None)
    p.add_argument("--location", default=None)
    p.add_argument("--attendee", action="append", default=[], metavar="EMAIL")
    p.add_argument("--recurse", action="append", default=[], metavar="RRULE",
                   help="RFC5545 rule, e.g. RRULE:FREQ=WEEKLY;BYDAY=MO,WE")
    p.add_argument("--reminder", action="append", default=[], metavar="METHOD;MINUTES",
                   help="e.g. popup;30 or email;1440 (disables the default reminders)")
    p.add_argument("--attach-link", action="append", default=[], metavar="URL[;TITLE]",
                   help="Attachment link (Drive file URL); repeatable")
    p.add_argument("--all-day", action="store_true")
    p.add_argument("--event-file", default=None,
                   help="Send this reviewed JSON event resource instead of the fields")
    p.add_argument("--send-updates", default="none",
                   choices=["all", "externalOnly", "none"],
                   help="Guest notification behavior (default none: no guest email)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--preview", action="store_true",
                   help="Print the exact request body and exit — nothing is sent")


def register(sub):
    import argparse

    p = sub.add_parser(
        "calendar", help="Google Calendar over a stored connection: events, "
        "free/busy, create/update/delete (explicit guest-notification rules)",
        epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = p.add_subparsers(dest="command", required=True)

    acc = sp.add_parser("accounts", help="List Google accounts serving Calendar")
    acc.add_argument("--json", action="store_true")

    def base_args(x):
        x.add_argument("--account", required=True)

    cals = sp.add_parser("calendars", help="List the account's calendars")
    base_args(cals)
    cals.add_argument("--max", type=int, default=25)
    cals.add_argument("--page-token", default=None)
    cals.add_argument("--json", action="store_true")

    events = sp.add_parser("events", help="List/search events in a bounded window")
    base_args(events)
    events.add_argument("--calendar", default=None)
    events.add_argument("--time-min", required=True, help="ISO 8601 window start")
    events.add_argument("--time-max", required=True, help="ISO 8601 window end (<= 366 days)")
    events.add_argument("--query", default=None, help="Text match")
    events.add_argument("--max", type=int, default=25)
    events.add_argument("--page-token", default=None)
    events.add_argument("--json", action="store_true")

    event = sp.add_parser("event", help="Show one event")
    event.add_argument("event_id")
    base_args(event)
    event.add_argument("--calendar", default=None)
    event.add_argument("--json", action="store_true")

    fb = sp.add_parser("freebusy", help="Free/busy for calendars over a window")
    base_args(fb)
    fb.add_argument("--calendar", action="append", default=[],
                    help="Calendar id (repeatable; default primary)")
    fb.add_argument("--time-min", required=True)
    fb.add_argument("--time-max", required=True)
    fb.add_argument("--json", action="store_true")

    create = sp.add_parser("create", help="Create an event (guests only notified via --send-updates)")
    _write_args(create)

    update = sp.add_parser("update", help="Update one event's fields")
    update.add_argument("event_id")
    _write_args(update)

    delete = sp.add_parser("delete", help="Delete an event (guests notified per --send-updates)")
    delete.add_argument("event_id")
    delete.add_argument("--calendar", default=None)
    delete.add_argument("--account", required=True)
    delete.add_argument("--send-updates", default="none",
                        choices=["all", "externalOnly", "none"])
    delete.add_argument("--json", action="store_true")
    delete.add_argument("--preview", action="store_true",
                        help="Print the request the delete would make and exit")


def _reminders(specs):
    if not specs:
        return None
    overrides = []
    for spec in specs:
        method, _, minutes = str(spec).partition(";")
        overrides.append({"method": method.strip() or "popup",
                          "minutes": int(minutes or 30)})
    return {"useDefault": False, "overrides": overrides}


def _attach_links(specs):
    links = []
    for spec in specs or []:
        url, _, title = str(spec).partition(";")
        entry = {"file_url": url.strip()}
        if title.strip():
            entry["title"] = title.strip()
        links.append(entry)
    return links


def _fields(args):
    return {
        "summary": args.summary, "start": args.start, "end": args.end,
        "timezone_name": args.timezone, "description": args.description,
        "location": args.location, "attendees": args.attendee or None,
        "recurrence": args.recurse or None, "reminders": _reminders(args.reminder),
        "attachments": _attach_links(args.attach_link) or None,
        "all_day": args.all_day,
        "payload": None,
    }


def _read_event_file(path):
    import json
    import sys

    with (sys.stdin if path == "-" else open(path, encoding="utf-8")) as handle:
        return json.loads(handle.read())


def run(args, api_url, debug, child=None):
    c = commands
    account = getattr(args, "account", None)
    as_json = getattr(args, "json", False)
    if args.command == "accounts":
        return c.calendar_accounts(api_url, as_json=args.json, debug=debug)
    if args.command == "calendars":
        return c.calendar_calendars(api_url, account=account, max_results=args.max,
                                    page_token=args.page_token, as_json=as_json, debug=debug)
    if args.command == "events":
        return c.calendar_events(api_url, account=account, calendar=args.calendar,
                                 time_min=args.time_min, time_max=args.time_max,
                                 query=args.query, max_results=args.max,
                                 page_token=args.page_token, as_json=as_json, debug=debug)
    if args.command == "event":
        return c.calendar_event(api_url, args.event_id, account=account,
                                calendar=args.calendar, as_json=as_json, debug=debug)
    if args.command == "freebusy":
        return c.calendar_freebusy(api_url, account=account,
                                   calendar_ids=args.calendar or None,
                                   time_min=args.time_min, time_max=args.time_max,
                                   as_json=as_json, debug=debug)
    if args.command in ("create", "update"):
        fields = _fields(args)
        payload = None
        if args.event_file:
            payload = _read_event_file(args.event_file)
            fields = {"payload": payload}
        return c.calendar_write(api_url, "POST" if args.command == "create" else "PATCH",
                                getattr(args, "event_id", None), account=account,
                                calendar=args.calendar, send_updates=args.send_updates,
                                fields=fields, as_json=as_json, preview=args.preview,
                                debug=debug)
    if args.command == "delete":
        return c.calendar_delete(api_url, args.event_id, account=account,
                                 calendar=args.calendar, send_updates=args.send_updates,
                                 as_json=as_json, preview=args.preview, debug=debug)
    return 2
