#!/usr/bin/env python3
"""Read-only Gmail / Calendar / Drive puller for a Dapier-issued access token.

Run it as the child of `dapier token exec`, which puts a fresh Google access
token in DAPIER_ACCESS_TOKEN for this process only:

    uv run dapier token exec gmail datatalks --agent admin-os -- \
        python3 .agents/skills/google-data-pull/gpull.py gmail list --q "newer_than:7d" --max 20

Standard library only. Every Google request is a GET (plus the POST to the
tokeninfo endpoint, which reads token metadata); the request helper refuses
anything else, so this script cannot send, modify, or delete. The token is
never printed or written anywhere.
"""

import argparse
import base64
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
CALENDAR = "https://www.googleapis.com/calendar/v3"
DRIVE = "https://www.googleapis.com/drive/v3"
TOKENINFO = "https://oauth2.googleapis.com/tokeninfo"

# Google-native files cannot be downloaded, only exported. Defaults per type.
EXPORT_DEFAULTS = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",  # first sheet only
    "application/vnd.google-apps.presentation": "text/plain",
    "application/vnd.google-apps.drawing": "image/png",
}


class ApiError(Exception):
    pass


def _token():
    token = os.environ.get("DAPIER_ACCESS_TOKEN")
    if not token:
        sys.exit("DAPIER_ACCESS_TOKEN is not set: run this under "
                 "`uv run dapier token exec <service> <account> --agent <agent> -- ...`")
    return token


def request(url, params=None, *, raw=False, form=None):
    """GET ``url`` (or POST ``form`` to tokeninfo only). Returns JSON or bytes."""
    if form is not None and url != TOKENINFO:
        raise RuntimeError("read-only helper: only GET requests are allowed")
    if params:
        url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if form is not None else "GET")
    if form is None:
        req.add_header("Authorization", f"Bearer {_token()}")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        try:
            detail = json.loads(detail).get("error", {})
            detail = detail.get("message", detail) if isinstance(detail, dict) else detail
        except ValueError:
            pass
        raise ApiError(f"HTTP {exc.code} for {url.split('?')[0]}: {detail}") from None
    return body if raw else json.loads(body or b"{}")


def paged(url, params, items_key, limit):
    """Yield up to ``limit`` items across nextPageToken pages (limit 0 = all)."""
    params = dict(params)
    count = 0
    while True:
        page = request(url, params)
        for item in page.get(items_key, []):
            yield item
            count += 1
            if limit and count >= limit:
                return
        token = page.get("nextPageToken")
        if not token:
            return
        params["pageToken"] = token


def emit(obj):
    print(json.dumps(obj, ensure_ascii=False))


def write_out(path, data):
    if path in (None, "-"):
        sys.stdout.buffer.write(data)
        return
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data)
    print(path, file=sys.stderr)


# --- whoami -----------------------------------------------------------------

def cmd_whoami(args):
    info = request(TOKENINFO, form={"access_token": _token()})
    emit({"email": info.get("email"), "expires_in": int(info.get("expires_in", 0)),
          "scopes": sorted(info.get("scope", "").split())})


# --- Gmail ------------------------------------------------------------------

def _headers(payload, wanted=("Subject", "From", "To", "Date")):
    found = {h["name"]: h["value"] for h in payload.get("headers", []) if h["name"] in wanted}
    return {name.lower(): found.get(name) for name in wanted}


def _b64(data):
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _walk(part):
    yield part
    for child in part.get("parts", []) or []:
        yield from _walk(child)


def _body_text(payload):
    """text/plain if present, else tag-stripped text/html."""
    plain, rich = [], []
    for part in _walk(payload):
        data = (part.get("body") or {}).get("data")
        if not data or part.get("filename"):
            continue
        text = _b64(data).decode("utf-8", "replace")
        (plain if part.get("mimeType") == "text/plain" else
         rich if part.get("mimeType") == "text/html" else []).append(text)
    if plain:
        return "\n".join(plain)
    text = re.sub(r"(?is)<(script|style).*?</\1>", "", "\n".join(rich))
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", text)
    return re.sub(r"\n\s*\n+", "\n\n", html.unescape(re.sub(r"<[^>]+>", "", text))).strip()


def _attachments(payload):
    return [{"filename": p["filename"], "mime": p.get("mimeType"),
             "size": (p.get("body") or {}).get("size"),
             "attachment_id": (p.get("body") or {}).get("attachmentId")}
            for p in _walk(payload) if p.get("filename")]


def cmd_gmail_list(args):
    params = {"maxResults": min(args.max or 500, 500)}
    if args.q:
        params["q"] = args.q
    if args.label:
        params["labelIds"] = args.label
    for ref in paged(f"{GMAIL}/messages", params, "messages", args.max):
        if args.ids_only:
            emit({"id": ref["id"], "thread_id": ref["threadId"]})
            continue
        msg = request(f"{GMAIL}/messages/{ref['id']}",
                      {"format": "metadata", "metadataHeaders": ["Subject", "From", "To", "Date"]})
        emit({"id": msg["id"], "thread_id": msg["threadId"], **_headers(msg["payload"]),
              "labels": msg.get("labelIds", []), "snippet": msg.get("snippet")})


def cmd_gmail_get(args):
    msg = request(f"{GMAIL}/messages/{args.id}", {"format": "full"})
    out = {"id": msg["id"], "thread_id": msg["threadId"], **_headers(msg["payload"]),
           "labels": msg.get("labelIds", []), "attachments": _attachments(msg["payload"]),
           "body": _body_text(msg["payload"])}
    if args.out:
        write_out(args.out, (json.dumps(out, ensure_ascii=False, indent=2) + "\n").encode())
    else:
        emit(out)


def cmd_gmail_thread(args):
    thread = request(f"{GMAIL}/threads/{args.id}", {"format": "full"})
    for msg in thread.get("messages", []):
        emit({"id": msg["id"], **_headers(msg["payload"]), "body": _body_text(msg["payload"])})


def cmd_gmail_raw(args):
    msg = request(f"{GMAIL}/messages/{args.id}", {"format": "raw"})
    write_out(args.out, _b64(msg["raw"]))


def cmd_gmail_attachment(args):
    att = request(f"{GMAIL}/messages/{args.id}/attachments/{args.attachment_id}")
    write_out(args.out, _b64(att["data"]))


def cmd_gmail_labels(args):
    for label in request(f"{GMAIL}/labels").get("labels", []):
        emit({"id": label["id"], "name": label["name"], "type": label.get("type")})


# --- Calendar ---------------------------------------------------------------

def cmd_cal_calendars(args):
    for cal in paged(f"{CALENDAR}/users/me/calendarList", {}, "items", 0):
        emit({"id": cal["id"], "summary": cal.get("summary"),
              "access_role": cal.get("accessRole"), "primary": cal.get("primary", False)})


def _rfc3339(value):
    """Accept YYYY-MM-DD or a full RFC 3339 timestamp; dates mean UTC midnight."""
    return f"{value}T00:00:00Z" if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) else value


def cmd_cal_events(args):
    params = {"singleEvents": "true", "orderBy": "startTime", "maxResults": 250}
    if args.start:
        params["timeMin"] = _rfc3339(args.start)
    if args.end:
        params["timeMax"] = _rfc3339(args.end)
    if args.q:
        params["q"] = args.q
    cal = urllib.parse.quote(args.calendar, safe="")
    for ev in paged(f"{CALENDAR}/calendars/{cal}/events", params, "items", args.max):
        start, end = ev.get("start", {}), ev.get("end", {})
        emit({"id": ev["id"], "summary": ev.get("summary"),
              "start": start.get("dateTime") or start.get("date"),
              "end": end.get("dateTime") or end.get("date"),
              "status": ev.get("status"), "location": ev.get("location"),
              "attendees": [a.get("email") for a in ev.get("attendees", [])],
              "html_link": ev.get("htmlLink")})


# --- Drive ------------------------------------------------------------------

FILE_FIELDS = "id,name,mimeType,modifiedTime,size,owners(emailAddress),webViewLink"


def _quote(value):
    return value.replace("\\", "\\\\").replace("'", "\\'")


def cmd_drive_search(args):
    clauses = ["trashed = false"]
    if args.name:
        clauses.append(f"name contains '{_quote(args.name)}'")
    if args.fulltext:
        clauses.append(f"fullText contains '{_quote(args.fulltext)}'")
    if args.mime:
        clauses.append(f"mimeType = '{args.mime}'")
    if args.q:
        clauses.append(f"({args.q})")
    params = {"q": " and ".join(clauses), "pageSize": 100, "orderBy": "modifiedTime desc",
              "fields": f"nextPageToken,files({FILE_FIELDS})",
              "supportsAllDrives": "true", "includeItemsFromAllDrives": "true",
              "corpora": "allDrives" if args.all_drives else "user"}
    for f in paged(f"{DRIVE}/files", params, "files", args.max):
        emit({"id": f["id"], "name": f["name"], "mime": f["mimeType"],
              "modified": f.get("modifiedTime"), "size": f.get("size"),
              "owners": [o.get("emailAddress") for o in f.get("owners", [])],
              "link": f.get("webViewLink")})


def cmd_drive_meta(args):
    emit(request(f"{DRIVE}/files/{args.id}",
                 {"fields": FILE_FIELDS, "supportsAllDrives": "true"}))


def cmd_drive_export(args):
    """Export a Google-native file (Docs/Sheets/Slides) or download a binary."""
    meta = request(f"{DRIVE}/files/{args.id}", {"fields": "id,name,mimeType",
                                                 "supportsAllDrives": "true"})
    mime = meta["mimeType"]
    if mime.startswith("application/vnd.google-apps."):
        target = args.mime or EXPORT_DEFAULTS.get(mime)
        if not target:
            raise ApiError(f"{mime} has no default export type; pass --mime")
        data = request(f"{DRIVE}/files/{args.id}/export", {"mimeType": target}, raw=True)
    else:
        data = request(f"{DRIVE}/files/{args.id}",
                       {"alt": "media", "supportsAllDrives": "true"}, raw=True)
    write_out(args.out, data)


def cmd_sheet_values(args):
    """All tabs of a spreadsheet, or one A1 range, as CSV (Sheets API, read-only GET)."""
    import csv
    import io
    base = f"https://sheets.googleapis.com/v4/spreadsheets/{args.id}"
    ranges = [args.range] if args.range else [
        s["properties"]["title"]
        for s in request(base, {"fields": "sheets.properties.title"})["sheets"]]
    for rng in ranges:
        values = request(f"{base}/values/{urllib.parse.quote(rng, safe='')}").get("values", [])
        buf = io.StringIO()
        csv.writer(buf).writerows(values)
        if args.out_dir:
            safe = re.sub(r"[^A-Za-z0-9._-]+", "_", rng)
            write_out(os.path.join(args.out_dir, f"{safe}.csv"), buf.getvalue().encode())
        else:
            print(f"# {rng}")
            sys.stdout.write(buf.getvalue())


def build_parser():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="service", required=True)

    sub.add_parser("whoami", help="Token's account, scopes, seconds left").set_defaults(fn=cmd_whoami)

    gmail = sub.add_parser("gmail").add_subparsers(dest="cmd", required=True)
    g = gmail.add_parser("list", help="Messages matching a Gmail search, newest first (JSONL)")
    g.add_argument("--q", help="Gmail search syntax, e.g. 'from:x newer_than:7d'")
    g.add_argument("--label", action="append", help="Label id filter (repeatable), e.g. INBOX")
    g.add_argument("--max", type=int, default=20, help="Stop after N messages; 0 = all pages")
    g.add_argument("--ids-only", action="store_true", help="Skip the per-message metadata fetch")
    g.set_defaults(fn=cmd_gmail_list)
    g = gmail.add_parser("get", help="One message: headers, decoded body, attachment list")
    g.add_argument("id")
    g.add_argument("--out", help="Write JSON to this file instead of stdout")
    g.set_defaults(fn=cmd_gmail_get)
    g = gmail.add_parser("thread", help="Every message in a thread with bodies (JSONL)")
    g.add_argument("id")
    g.set_defaults(fn=cmd_gmail_thread)
    g = gmail.add_parser("raw", help="Original RFC 822 message (.eml)")
    g.add_argument("id")
    g.add_argument("--out", default="-")
    g.set_defaults(fn=cmd_gmail_raw)
    g = gmail.add_parser("attachment", help="Download one attachment by id")
    g.add_argument("id")
    g.add_argument("attachment_id")
    g.add_argument("--out", required=True)
    g.set_defaults(fn=cmd_gmail_attachment)
    gmail.add_parser("labels", help="Label ids and names").set_defaults(fn=cmd_gmail_labels)

    cal = sub.add_parser("calendar").add_subparsers(dest="cmd", required=True)
    c = cal.add_parser("calendars", help="Calendars in the account's list")
    c.set_defaults(fn=cmd_cal_calendars)
    c = cal.add_parser("events", help="Events in a time window, expanded and ordered (JSONL)")
    c.add_argument("--calendar", default="primary")
    c.add_argument("--start", help="YYYY-MM-DD or RFC 3339; default unbounded")
    c.add_argument("--end", help="YYYY-MM-DD or RFC 3339 (exclusive)")
    c.add_argument("--q", help="Free-text filter")
    c.add_argument("--max", type=int, default=50, help="0 = all pages")
    c.set_defaults(fn=cmd_cal_events)

    drive = sub.add_parser("drive").add_subparsers(dest="cmd", required=True)
    d = drive.add_parser("search", help="Files by name/content/type, newest first (JSONL)")
    d.add_argument("--name", help="name contains ...")
    d.add_argument("--fulltext", help="fullText contains ...")
    d.add_argument("--mime", help="exact mimeType, e.g. application/vnd.google-apps.document")
    d.add_argument("--q", help="extra raw Drive query clause, ANDed")
    d.add_argument("--all-drives", action="store_true", help="Include shared drives")
    d.add_argument("--max", type=int, default=25, help="0 = all pages")
    d.set_defaults(fn=cmd_drive_search)
    d = drive.add_parser("meta", help="One file's metadata")
    d.add_argument("id")
    d.set_defaults(fn=cmd_drive_meta)
    d = drive.add_parser("export", help="Docs->txt, Sheets->csv (first tab), else raw download")
    d.add_argument("id")
    d.add_argument("--mime", help="Export type override, e.g. text/markdown, "
                                  "application/pdf, application/vnd.openxmlformats-"
                                  "officedocument.spreadsheetml.sheet")
    d.add_argument("--out", default="-")
    d.set_defaults(fn=cmd_drive_export)
    d = drive.add_parser("sheet", help="Spreadsheet tabs as CSV via the Sheets API")
    d.add_argument("id")
    d.add_argument("--range", help="One A1 range or tab name; default every tab")
    d.add_argument("--out-dir", help="Write one <tab>.csv per tab here")
    d.set_defaults(fn=cmd_sheet_values)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        args.fn(args)
    except ApiError as exc:
        sys.exit(str(exc))
    except BrokenPipeError:
        pass


if __name__ == "__main__":
    main()
