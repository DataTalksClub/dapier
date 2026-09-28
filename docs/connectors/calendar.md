# Google Calendar connector

The Google Calendar connector covers Zapier's Calendar staples: create an
event (detailed or quick-add), find events (with a find-or-create option),
update one, delete one, and a "New Event" poll trigger. Everything runs
through a Google OAuth connection — the same connection type Drive and
Sheets use — talking to the Calendar API v3.

The connector is registered as `google-calendar` (palette chip, discovery
sources, poll source). Connection ids are separate from the connector name:
the docs' scheduling account is connected as `google-calendar` (see
[google.md](google.md#the-two-workspace-account-connections)).

## Connect it

The Google connector uses the shared Google OAuth client
([google.md](google.md) documents the client setup). Create or update the
connection with the CLI:

```sh
uv run dapier connections create google-calendar --provider google --scopes \
  https://www.googleapis.com/auth/calendar.freebusy \
  https://www.googleapis.com/auth/calendar.events.owned \
  https://www.googleapis.com/auth/calendar.readonly \
  https://www.googleapis.com/auth/userinfo.email
uv run dapier connections connect google-calendar --agent <agent-name>
```

The console's Google connection card requests exactly these scopes.

| Scope | Purpose | Google classification |
|-------|---------|-----------------------|
| `https://www.googleapis.com/auth/calendar.events.owned` | Create, update, and delete events on calendars the user owns | Sensitive |
| `https://www.googleapis.com/auth/calendar.readonly` | The calendars/events listings and the New Event poll | Sensitive |
| `https://www.googleapis.com/auth/calendar.freebusy` | Free/busy lookups for the scheduling flows | Sensitive |
| `https://www.googleapis.com/auth/userinfo.email` | Verify the consenting account at connect time | Basic identity |

### Consent-time scope step (ops)

The live Google OAuth client must request the calendar scopes at consent
time. Add each scope from the table above to the Google Auth Platform
project's **Data Access** scope list (the procedure and the project's
current scope table are in [google.md](google.md)), keep the Dapier
connection's scope list in sync, then reconnect the account — changing a
scope list never updates an already issued refresh token. Dapier itself adds
no code-level gate on top: a Google connection verifies through the
`userinfo.email` identity check, so a connection granted the calendar scopes
verifies and serves the connector as-is.

## Actions

| Action | Zapier counterpart | Notes |
|--------|--------------------|-------|
| `calendar_create_event` | Create Detailed Event | Full event: title, start/end, timezone, description, location, attendees. |
| `calendar_quick_add` | Quick Add Event | One line of text ("Reviewer call tomorrow 10am"), Calendar parses it. |
| `calendar_find_events` | Find or Create Event | Text `q` over a time window (defaults: yesterday through the next quarter); with **Create if missing** a miss posts the event instead of returning `found: false`. |
| `calendar_update_event` | Update Event | Patches only the fields that are set; attendees replaces the whole list. |
| `calendar_delete_event` | Delete Event | Removes one event; pair with a find step when the event may already be gone. |

Times are ISO: a bare `YYYY-MM-DD` is an all-day event (Google's `date`
field), anything else a `dateTime` with the optional IANA `timezone` pinned
on. Start and end must agree on the style — Google rejects mixed event
times. Find outputs chain into update/delete through
`{steps.<id>.output.event.event_id}`.

## Trigger: New Event (poll)

`event.new` is a poll, not a webhook: Calendar has no event-creation push,
so a stored poll trigger lists the calendar on the poll schedule
(`google-calendar.events` source, `rate(5 minutes)` is a good cadence) and
fires one event per **newly created** event.

The cursor is the event's `created` timestamp, seeded on the first fire so
enabling a trigger does not fire the calendar's whole history; an edit to an
old event never poses as a new event. A recurring series' occurrences carry
the series' creation time, so a new week of a weekly series does not fire
one event per occurrence.

Save a poll trigger from a JSON file:

```sh
uv run dapier triggers polls save calendar-news.json
```

```json
{
  "name": "calendar-news",
  "expression": "rate(5 minutes)",
  "source": "google-calendar.events",
  "calendar_id": "ops@example.test",
  "connection_id": "google-calendar",
  "actions": [{"type": "email_send", "to": "ops@example.test"}]
}
```

`connection_id` is required (the poll refreshes the connection's OAuth
token); `calendar_id` takes any id from the calendars listing, `primary`
included.

## Discovery

The connection's listings feed the field pickers (console and CLI):

| Resource | Lists |
|----------|-------|
| `google-calendar.calendars` | Calendars the connection can see, with timezones — the actions' Calendar ID field browses it. |
| `google-calendar.events` | Upcoming events in one calendar (`calendar_id` required, optional text `query`). |

```sh
uv run dapier connections discover google-calendar calendars
uv run dapier connections discover google-calendar events --param calendar_id=ops@example.test
```

The trigger config offers the same listings as options (a calendar picker,
and per-calendar event options).
