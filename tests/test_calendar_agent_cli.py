"""Calendar agent ops, routes, and the `dapier calendar` CLI thin client.

Pins the CALENDAR-DRIVE amendment's guarantees: explicit account selection,
bounded event windows (inverted/over-long rejected), guest email never
accidental (sendUpdates defaults to none and is validated), free/busy,
recurrence/timezone/attendee/reminders/attachment-link fields, the
reviewed-JSON-payload path, provider error relays, wrong-account isolation,
and the CLI's --preview (prints the request, sends nothing).
"""

import json

import pytest

from src.dapier.api.agent import calendar as calendar_api
from src.dapier.api.agent import calendar_ops
from src.dapier.api.agent import google_accounts

USERINFO = "https://www.googleapis.com/auth/userinfo.email"
CAL_SCOPES = ("https://www.googleapis.com/auth/calendar.freebusy",
              "https://www.googleapis.com/auth/calendar.events.owned",
              "https://www.googleapis.com/auth/calendar.readonly",
              USERINFO)
ACCOUNT_A = "cal@gmail.com"


def _connection(cid, email, scopes):
    return {"connection_id": cid, "provider": "google", "status": "connected",
            "verified_account_id": email, "account_title": email.split("@")[0].title(),
            "scopes": list(scopes), "granted_scopes": list(scopes)}


@pytest.fixture
def calendar_world(monkeypatch):
    conn = _connection("google-cal", ACCOUNT_A, CAL_SCOPES)
    monkeypatch.setattr(google_accounts, "_tables", lambda: (object(), object()))
    monkeypatch.setattr(google_accounts.connections, "_scan_all", lambda table: [conn])
    from src.dapier.api import discovery as api_discovery

    monkeypatch.setattr(api_discovery, "_load", lambda cid, table: (conn, None))

    def fake_token(connection, **kwargs):
        return f"token-{connection['connection_id']}", {"expires_at": None}

    monkeypatch.setattr(calendar_ops.tokens, "get_access_token", fake_token)
    return conn


@pytest.fixture
def cal_provider(monkeypatch):
    calls = []
    routes = []

    def on(method, fragment, canned=None, status=200):
        routes.append((method, fragment, canned, status))
        return on

    on.routes = routes
    on.calls = calls

    def request(method, url, token, payload, *, transport=None):
        calls.append({"method": method, "url": url, "token": token, "payload": payload})
        for want_method, fragment, canned, status in reversed(routes):
            if want_method == method and fragment in url:
                if status >= 300:
                    from src.dapier.connections.discovery import DiscoveryError

                    raise DiscoveryError(
                        f"provider returned HTTP {status}: {canned or 'backend error'}",
                        status=502)
                return canned or {}
        raise AssertionError(f"unexpected provider call {method} {url}")

    monkeypatch.setattr(calendar_ops.provider, "_request", request)
    on.on = on
    return on


# --- reads ----------------------------------------------------------------------


def test_accounts_and_calendars_use_the_resolved_connection(calendar_world, cal_provider):
    cal_provider.on("GET", "/users/me/calendarList", {"items": [
        {"id": "primary", "summary": "Main", "timeZone": "Europe/Berlin",
         "primary": True, "accessRole": "owner"}]})
    payload = calendar_ops.list_calendars(ACCOUNT_A)
    assert payload["count"] == 1
    assert payload["calendars"][0]["timezone"] == "Europe/Berlin"
    assert cal_provider.calls[0]["token"] == "token-google-cal"
    with pytest.raises(calendar_ops.CalendarOpsError) as required:
        calendar_ops.list_calendars("other@gmail.com")
    assert required.value.status == 400


def test_list_events_requires_a_bounded_window(calendar_world, cal_provider):
    cal_provider.on("GET", "/events?", {"items": [
        {"id": "e1", "summary": "Sync", "start": {"dateTime": "2026-11-05T09:00:00Z"},
         "end": {"dateTime": "2026-11-05T10:00:00Z"}}]})
    with pytest.raises(calendar_ops.CalendarOpsError):
        calendar_ops.list_events(ACCOUNT_A, time_min=None, time_max=None)
    with pytest.raises(calendar_ops.CalendarOpsError) as inverted:
        calendar_ops.list_events(ACCOUNT_A, time_min="2026-11-10T00:00:00Z",
                                 time_max="2026-11-01T00:00:00Z")
    assert "before" in str(inverted.value)
    with pytest.raises(calendar_ops.CalendarOpsError) as huge:
        calendar_ops.list_events(ACCOUNT_A, time_min="2026-01-01T00:00:00Z",
                                 time_max="2027-06-01T00:00:00Z")
    assert "366" in str(huge.value)
    payload = calendar_ops.list_events(ACCOUNT_A, time_min="2026-11-04T00:00:00Z",
                                       time_max="2026-11-11T00:00:00Z")
    assert payload["count"] == 1 and payload["events"][0]["summary"] == "Sync"
    url = cal_provider.calls[-1]["url"]
    assert "singleEvents=true" in url and "orderBy=startTime" in url
    assert "timeMin=2026-11-04T00%3A00%3A00Z" in url


def test_freebusy_posts_the_window_and_calendar_ids(calendar_world, cal_provider):
    cal_provider.on("POST", "/freeBusy", {"calendars": {
        "primary": {"busy": [{"start": "2026-11-05T09:00:00Z", "end": "2026-11-05T10:00:00Z"}]},
        "work@group": {"busy": []}}})
    payload = calendar_ops.freebusy(ACCOUNT_A, calendar_ids=["primary", "work@group"],
                                    time_min="2026-11-04T00:00:00Z",
                                    time_max="2026-11-06T00:00:00Z")
    body = cal_provider.calls[-1]["payload"]
    assert body["items"] == [{"id": "primary"}, {"id": "work@group"}]
    assert payload["calendars"]["primary"]["busy"][0]["start"].startswith("2026-11-05")
    assert payload["calendars"]["work@group"]["busy"] == []


# --- writes: sendUpdates discipline, structured fields, payload path --------------


def test_create_defaults_to_no_guest_email(calendar_world, cal_provider):
    cal_provider.on("POST", "/events?", {"id": "e9", "summary": "Sync",
                                         "start": {"dateTime": "2026-11-05T09:00:00Z"},
                                         "end": {"dateTime": "2026-11-05T10:00:00Z"}})
    calendar_ops.create_event(ACCOUNT_A, summary="Sync",
                              start="2026-11-05T09:00:00Z", end="2026-11-05T10:00:00Z",
                              attendees=["peer@corp.com"])
    url = cal_provider.calls[-1]["url"]
    assert "sendUpdates=none" in url  # attendees exist, but no invitation went out


def test_create_send_updates_is_explicit_and_validated(calendar_world, cal_provider):
    cal_provider.on("POST", "/events?", {"id": "e9"})
    calendar_ops.create_event(ACCOUNT_A, summary="Sync", start="2026-11-05T09:00:00Z",
                              end="2026-11-05T10:00:00Z", send_updates="all")
    assert "sendUpdates=all" in cal_provider.calls[-1]["url"]
    with pytest.raises(calendar_ops.CalendarOpsError):
        calendar_ops.create_event(ACCOUNT_A, summary="S", start="x", end="y",
                                  send_updates="bomb")


def test_create_structured_fields_full_shape(calendar_world, cal_provider):
    cal_provider.on("POST", "/events?", {"id": "e10"})
    calendar_ops.create_event(
        ACCOUNT_A, summary="Weekly sync", start="2026-11-05T09:00:00Z",
        end="2026-11-05T10:00:00Z", timezone_name="Europe/Berlin",
        description="agenda", location="Room 2", attendees=["a@b.c", "d@e.f"],
        recurrence=["RRULE:FREQ=WEEKLY;BYDAY=MO,WE"],
        reminders={"useDefault": False, "overrides": [{"method": "popup", "minutes": 30}]},
        attachments=[{"file_url": "https://drive.google.com/file/d/F1/view",
                      "title": "deck"}])
    body = cal_provider.calls[-1]["payload"]
    assert body["start"]["timeZone"] == "Europe/Berlin"
    assert body["recurrence"] == ["RRULE:FREQ=WEEKLY;BYDAY=MO,WE"]
    assert body["attendees"] == [{"email": "a@b.c"}, {"email": "d@e.f"}]
    assert body["reminders"]["overrides"][0]["minutes"] == 30
    assert body["attachments"][0]["fileUrl"].endswith("/F1/view")

    with pytest.raises(calendar_ops.CalendarOpsError):
        calendar_ops.create_event(ACCOUNT_A, summary="S", start=None, end=None)
    with pytest.raises(calendar_ops.CalendarOpsError):
        calendar_ops.create_event(ACCOUNT_A, summary=None, start="x", end="y")


def test_create_with_reviewed_payload_rejects_mixed_fields(calendar_world, cal_provider):
    cal_provider.on("POST", "/events?", {"id": "e11"})
    reviewed = {"summary": "Board", "start": {"date": "2026-12-01"},
                "end": {"date": "2026-12-02"}}
    calendar_ops.create_event(ACCOUNT_A, payload=dict(reviewed))
    sent = cal_provider.calls[-1]["payload"]
    assert sent["summary"] == "Board"
    assert sent["start"] == {"date": "2026-12-01"}
    with pytest.raises(calendar_ops.CalendarOpsError) as mixed:
        calendar_ops.create_event(ACCOUNT_A, payload=dict(reviewed), summary="Also")
    assert "either" in str(mixed.value)


def test_update_and_delete_carry_send_updates(calendar_world, cal_provider):
    cal_provider.on("PATCH", "/events/e1?", {"id": "e1", "summary": "Moved"})
    calendar_ops.update_event(ACCOUNT_A, "e1", summary="Moved",
                              start="2026-11-06T09:00:00Z", end="2026-11-06T10:00:00Z",
                              send_updates="all")
    assert cal_provider.calls[-1]["method"] == "PATCH"
    assert "sendUpdates=all" in cal_provider.calls[-1]["url"]

    cal_provider.on("DELETE", "/events/e1?", {})
    result = calendar_ops.delete_event(ACCOUNT_A, "e1", send_updates="externalOnly")
    assert result["deleted"] == "e1"
    assert "sendUpdates=externalOnly" in cal_provider.calls[-1]["url"]
    assert cal_provider.calls[-1]["method"] == "DELETE"


def test_provider_error_relays_status_and_scope_hint(calendar_world, cal_provider):
    cal_provider.on("GET", "/events?", status=403, canned="Insufficient Permission")
    with pytest.raises(calendar_ops.CalendarOpsError) as denied:
        calendar_ops.list_events(ACCOUNT_A, time_min="2026-11-04T00:00:00Z",
                                 time_max="2026-11-05T00:00:00Z")
    assert denied.value.status == 403
    assert "edit --scopes" in str(denied.value)


def test_get_event_view_flattens(calendar_world, cal_provider):
    cal_provider.on("GET", "/events/e2", {
        "id": "e2", "summary": "1:1", "creator": {"email": "me@x.y"},
        "organizer": {"email": "me@x.y"}, "status": "confirmed",
        "start": {"date": "2026-12-01"}, "end": {"date": "2026-12-02"},
        "recurrence": ["RRULE:FREQ=DAILY"], "attendees": [
            {"email": "peer@x.y", "responseStatus": "accepted"}],
        "reminders": {"useDefault": True},
        "attachments": [{"fileUrl": "https://drive.google.com/file/d/F2/view",
                         "title": "notes"}]})
    view = calendar_ops.get_event(ACCOUNT_A, "e2")
    assert view["all_day"] is True
    assert view["recurrence"] == ["RRULE:FREQ=DAILY"]
    assert view["attendees"][0]["response_status"] == "accepted"
    assert view["attachments"][0]["file_url"].endswith("/F2/view")


# --- routes ---------------------------------------------------------------------


def _gate(monkeypatch):
    seen = {}

    def require_operator(event, action):
        seen["action"] = action
        return "op", None

    monkeypatch.setattr(calendar_api, "require_operator", require_operator)
    monkeypatch.setattr(calendar_api, "audit", type("A", (), {
        "emit": staticmethod(lambda *a, **k: seen.setdefault("audit", True))})())
    return seen


def test_route_calendar_dispatch_and_gating(calendar_world, cal_provider, monkeypatch):
    seen = _gate(monkeypatch)
    cal_provider.on("GET", "/users/me/calendarList", {"items": []})
    resp = calendar_api.route_calendar(
        {"queryStringParameters": {"account": ACCOUNT_A}}, "GET",
        "/api/agent/calendar/calendars")
    assert resp["statusCode"] == 200
    assert seen["action"] == "calendar.list_calendars"
    assert seen["audit"]

    from src.dapier.api.agent.common import _json_response

    def deny(event, action):
        return None, _json_response(403, {"error": "denied"})

    monkeypatch.setattr(calendar_api, "require_operator", deny)
    assert calendar_api.route_calendar({}, "GET", "/api/agent/calendar/events")["statusCode"] == 403
    assert calendar_api.route_calendar({}, "GET", "/api/agent/calendar/zzz") is None


def test_route_calendar_create_defaults_send_updates(calendar_world, cal_provider, monkeypatch):
    _gate(monkeypatch)
    cal_provider.on("POST", "/events?", {"id": "e12"})
    resp = calendar_api.route_calendar(
        {"body": json.dumps({"account": ACCOUNT_A, "summary": "S",
                             "start": "2026-11-05T09:00:00Z",
                             "end": "2026-11-05T10:00:00Z",
                             "attendees": ["x@y.z"]})},
        "POST", "/api/agent/calendar/events")
    assert resp["statusCode"] == 200
    assert "sendUpdates=none" in cal_provider.calls[-1]["url"]


# --- CLI thin client + preview ---------------------------------------------------


def test_calendar_cli_and_preview_send_nothing(monkeypatch, tmp_path):
    from dapier_cli.commands import calendar as cmd

    calls = []

    def fake_call(api_url, method, path, body=None, timeout=20, debug=False):
        calls.append({"method": method, "path": path, "body": body})
        return {"ok": True}

    monkeypatch.setattr(cmd.api, "call", fake_call)

    event_file = tmp_path / "event.json"
    event_file.write_text(json.dumps({"summary": "Board", "start": {"date": "2026-12-01"},
                                      "end": {"date": "2026-12-02"}}))

    # preview: the exact body is printed, no API call happens
    rc = cmd.calendar_write("http://api", "POST", account=ACCOUNT_A, calendar="primary",
                            send_updates="all",
                            fields={"summary": "Sync", "start": "2026-11-05T09:00:00Z",
                                    "end": "2026-11-05T10:00:00Z",
                                    "attendees": ["peer@corp.com"]},
                            preview=True)
    assert rc == 0 and calls == []
    # the previewed send_updates rides the body exactly as the write would
    body = cmd._write_body(ACCOUNT_A, "primary", "all",
                           {"summary": "Sync", "start": "2026-11-05T09:00:00Z",
                            "end": "2026-11-05T10:00:00Z", "attendees": ["peer@corp.com"]})
    assert body["send_updates"] == "all" and body["account"] == ACCOUNT_A

    cmd.calendar_write("http://api", "POST", account=ACCOUNT_A, calendar="primary",
                       send_updates="all",
                       fields={"summary": "Sync", "start": "2026-11-05T09:00:00Z",
                               "end": "2026-11-05T10:00:00Z"})
    assert calls[0]["method"] == "POST" and "/api/agent/calendar/events" in calls[0]["path"]
    assert calls[0]["body"]["send_updates"] == "all"

    before = len(calls)
    cmd.calendar_delete("http://api", "E1", account=ACCOUNT_A, preview=True)
    assert len(calls) == before  # preview did not add a call

    cmd.calendar_delete("http://api", "E1", account=ACCOUNT_A, send_updates="none")
    assert calls[-1]["method"] == "DELETE"
    assert calls[-1]["body"]["send_updates"] == "none"


def test_calendar_cli_parser():
    from dapier_cli.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["calendar", "events", "--time-min", "2026-11-04T00:00:00Z",
                              "--time-max", "2026-11-05T00:00:00Z",
                              "--account", ACCOUNT_A])
    assert args.command == "events" and args.account == ACCOUNT_A
    create = parser.parse_args(["calendar", "create", "--summary", "S",
                                "--start", "2026-11-05T09:00:00Z",
                                "--end", "2026-11-05T10:00:00Z",
                                "--attendee", "a@b.c", "--recurse", "RRULE:FREQ=DAILY",
                                "--send-updates", "all", "--preview"])
    assert create.send_updates == "all" and create.preview
    assert create.attendee == ["a@b.c"]


def test_preview_masks_attachment_bytes(monkeypatch, tmp_path, capsys):
    from dapier_cli.commands import gmail as cmd_gmail
    from dapier_cli.commands.shared import preview_body

    attachment = tmp_path / "a.bin"
    attachment.write_bytes(b"\x00\x01binary!")
    spec = cmd_gmail.parse_attach(str(attachment))
    rc = preview_body({"to": "x@y.z", "attachments": [spec]})
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert rc == 0 and parsed["preview"] is True
    assert "<9 bytes>" in parsed["would_send"]["attachments"][0]["content_b64"]
    assert "AAEB" not in out  # the bytes themselves never print
