"""Gmail operations behind the ``/api/agent/gmail/*`` routes (the `dapier
gmail` CLI's backend; the connection token stays server-side).

The ops ride the same Google OAuth connections the Gmail chip uses: one
Google connection per signed-in account, the account picked by
``google_accounts.resolve_account`` from the operation's explicit
``--account``. Everything goes through the provider layer's ``_request``
(the shared transport seam tests patch), and every failure surfaces as a
``GmailOpsError`` whose message is the provider's redacted detail — never
token material. Scopes are honored, never widened: reads need the
connection's ``gmail.readonly``, sends ``gmail.send``, drafts and label
changes ``gmail.modify``/``gmail.compose`` — a connection without the
scope gets Gmail's own 403, relayed with a hint to fix the grant.

MIME construction (bodies, Unicode, binary attachments, threading headers)
reuses ``engine.actions.email._raw_message``, the builder the gmail_send
action already ships with: In-Reply-To/References, ``Re:`` subjects and the
original threadId are set from the replied-to message's headers.
"""

import base64
import hashlib
import re
from email.utils import getaddresses
from urllib.parse import quote, urlencode

from ...connections import discovery as provider
from ...connections import tokens
from ...engine.actions.email import _raw_message
from . import google_accounts

GMAIL_API_URL = "https://gmail.googleapis.com/gmail/v1"
DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100

# System labels the CLI's mark-read/unread/archive flags map onto.
UNREAD = "UNREAD"
INBOX = "INBOX"

_HEADER_NAMES = ("from", "to", "cc", "bcc", "subject", "date",
                 "message-id", "in-reply-to", "references", "reply-to")


class GmailOpsError(Exception):
    """Operator-facing Gmail failure; message must never carry a token."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _status_from(detail):
    match = re.search(r"HTTP (\d{3})", str(detail))
    return int(match.group(1)) if match else 502


def _scope_hint(status, detail):
    if status == 403 and ("insufficient" in detail.lower() or "not authorized" in detail.lower()):
        return (" — the connection lacks the Gmail scope for this operation; "
                "update its scopes (dapier connections edit --scopes) and reconnect")
    return ""


def _connection_and_token(account):
    """The resolved Google connection and a live bearer token for it."""
    try:
        connection_id = google_accounts.resolve_account("gmail", account)
    except google_accounts.GoogleAccountError as exc:
        raise GmailOpsError(str(exc), status=exc.status) from None
    from .. import discovery as discovery_api

    connection, error = discovery_api._load(connection_id, None)
    if error:
        raise GmailOpsError("Gmail connection not found", status=404)
    try:
        token, _info = tokens.get_access_token(connection)
    except Exception as exc:
        raise GmailOpsError(
            f"Gmail token unavailable for {connection_id}: {type(exc).__name__}",
            status=502) from None
    return connection_id, connection, token


def _call(token, method, path, payload=None):
    """One Gmail REST call; DiscoveryError relays the provider's detail."""
    try:
        return provider._request(method, GMAIL_API_URL + path, token, payload)
    except provider.DiscoveryError as exc:
        status = _status_from(str(exc))
        raise GmailOpsError(
            f"Gmail {method} failed: {exc}{_scope_hint(status, str(exc))}",
            status=status) from None


def _b64url_encode(data):
    return base64.urlsafe_b64encode(data).decode("ascii")


def _b64url_decode(text):
    text = str(text or "")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _page_params(max_results, page_token, extra=None):
    params = {"maxResults": max(1, min(int(max_results or DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE))}
    if page_token:
        params["pageToken"] = str(page_token)
    if extra:
        params.update(extra)
    return urlencode(params)


def _headers_of(payload):
    headers = {}
    for header in (payload or {}).get("headers") or []:
        name = str(header.get("name") or "").lower()
        if name in _HEADER_NAMES:
            headers[name] = str(header.get("value") or "")
    return headers


def _part_texts(payload):
    """(text, html) decoded from the first text/plain and text/html parts."""
    text = html = None
    stack = [payload] if isinstance(payload, dict) else []
    while stack:
        part = stack.pop(0)
        body = part.get("body") if isinstance(part.get("body"), dict) else {}
        mime = str(part.get("mimeType") or "")
        data = body.get("data")
        if mime in ("text/plain", "text/html") and data:
            try:
                decoded = _b64url_decode(data).decode("utf-8", "replace")
            except (ValueError, UnicodeDecodeError):
                decoded = None
            if decoded is not None:
                if mime == "text/plain" and text is None:
                    text = decoded
                elif mime == "text/html" and html is None:
                    html = decoded
        stack.extend(part.get("parts") or [])
    return text, html


def _attachments_of(payload):
    """Every attachment part: {filename, mime_type, size, attachment_id}."""
    found = []
    stack = [payload] if isinstance(payload, dict) else []
    while stack:
        part = stack.pop(0)
        body = part.get("body") if isinstance(part.get("body"), dict) else {}
        filename = str(part.get("filename") or "").strip()
        attachment_id = str(body.get("attachmentId") or "").strip()
        if filename or attachment_id:
            found.append({
                "filename": filename or None,
                "mime_type": part.get("mimeType") or None,
                "size": int(body.get("size") or 0),
                "attachment_id": attachment_id or None,
            })
        stack.extend(part.get("parts") or [])
    return found


def _message_view(data, *, include_text=True, include_html=False):
    payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
    headers = _headers_of(payload)
    view = {
        "id": data.get("id"),
        "thread_id": data.get("threadId"),
        "labels": data.get("labelIds") or [],
        "snippet": data.get("snippet"),
        "internal_date": str(data.get("internalDate") or ""),
        **headers,
    }
    if include_text:
        text, html = _part_texts(payload)
        view["text"] = text
        if include_html:
            view["html"] = html
        view["attachments"] = _attachments_of(payload)
    return view


def _draft_view(data):
    message = data.get("message") if isinstance(data.get("message"), dict) else {}
    payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
    headers = _headers_of(payload)
    text, html = _part_texts(payload)
    return {
        "id": data.get("id"),
        "message_id": message.get("id"),
        "thread_id": message.get("threadId"),
        **headers,
        "text": text,
        "html": html,
        "attachments": _attachments_of(payload),
    }


# --- reads --------------------------------------------------------------------


def accounts(account=None):
    """The Google accounts that serve Gmail (the CLI's `gmail accounts`)."""
    found = google_accounts.list_accounts("gmail")
    return {"accounts": found, "count": len(found)}


def list_messages(account, query=None, label_ids=None, max_results=DEFAULT_PAGE_SIZE,
                  page_token=None):
    """Search/list the mailbox; every stub expanded to a compact view."""
    _connection_id, _connection, token = _connection_and_token(account)
    extra = {}
    if query:
        extra["q"] = str(query)
    if label_ids:
        extra["labelIds"] = ",".join(str(label) for label in label_ids)
    data = _call(token, "GET", f"/users/me/messages?{_page_params(max_results, page_token, extra)}")
    stubs = [entry for entry in data.get("messages") or [] if isinstance(entry, dict) and entry.get("id")]
    messages = []
    for stub in stubs:
        full = _call(token, "GET",
                     f"/users/me/messages/{quote(str(stub['id']), safe='')}?format=metadata")
        messages.append(_message_view(full, include_text=False))
    return {
        "messages": messages,
        "count": len(messages),
        "next_page_token": data.get("nextPageToken"),
        "result_size_estimate": data.get("resultSizeEstimate"),
    }


def get_message(account, message_id, include_html=False):
    _connection_id, _connection, token = _connection_and_token(account)

    data = _call(token, "GET",
                 f"/users/me/messages/{quote(str(message_id), safe='')}?format=full")
    return _message_view(data, include_text=True, include_html=include_html)


ATTACHMENT_CHUNK_BYTES = 1024 * 1024
INLINE_ATTACHMENT_BYTES = 4 * 1024 * 1024


def get_attachment(account, message_id, attachment_id, *, offset=0, chunk_size=None):
    try:
        offset = int(offset)
        chunk_size = int(chunk_size) if chunk_size is not None else None
    except (ValueError, TypeError):
        raise GmailOpsError("offset and chunk_size must be integers") from None
    if offset < 0 or (chunk_size is not None and not 1 <= chunk_size <= ATTACHMENT_CHUNK_BYTES):
        raise GmailOpsError("offset must be nonnegative; chunk_size must be 1..1048576")
    if offset and chunk_size is None:
        raise GmailOpsError("offset requires chunk_size")
    _connection_id, _connection, token = _connection_and_token(account)

    data = _call(
        token, "GET",
        f"/users/me/messages/{quote(str(message_id), safe='')}"
        f"/attachments/{quote(str(attachment_id), safe='')}")
    encoded = data.get("data") or ""
    content = _b64url_decode(encoded)
    size = len(content)
    if int(data.get("size") or 0) != size:
        raise GmailOpsError("Gmail attachment size does not match its content", status=502)
    result = {"attachment_id": data.get("attachmentId") or attachment_id,
              "size": size, "data_b64": encoded}
    if chunk_size is None:
        if size > INLINE_ATTACHMENT_BYTES:
            raise GmailOpsError("Large attachment requires chunk_size; update the Dapier CLI", status=413)
        return result
    if offset > size:
        raise GmailOpsError("offset exceeds attachment size")
    part = content[offset:offset + chunk_size]
    return {**result, "data_b64": base64.urlsafe_b64encode(part).decode("ascii"),
            "chunked": True, "offset": offset, "chunk_size": len(part),
            "next_offset": offset + len(part) if offset + len(part) < size else None,
            "sha256": hashlib.sha256(content).hexdigest()}



def list_threads(account, query=None, max_results=DEFAULT_PAGE_SIZE, page_token=None):
    _connection_id, _connection, token = _connection_and_token(account)
    extra = {"q": str(query)} if query else None
    data = _call(token, "GET",
                 f"/users/me/threads?{_page_params(max_results, page_token, extra)}")
    threads = [
        {"id": entry.get("id"), "snippet": entry.get("snippet")}
        for entry in data.get("threads") or []
        if isinstance(entry, dict) and entry.get("id")
    ]
    return {"threads": threads, "count": len(threads),
            "next_page_token": data.get("nextPageToken")}


def get_thread(account, thread_id):
    _connection_id, _connection, token = _connection_and_token(account)

    data = _call(token, "GET", f"/users/me/threads/{quote(str(thread_id), safe='')}?format=full")
    messages = [
        _message_view(message, include_text=True, include_html=False)
        for message in data.get("messages") or []
        if isinstance(message, dict) and message.get("id")
    ]
    return {"id": data.get("id"), "messages": messages, "count": len(messages)}


def list_labels(account):
    _connection_id, _connection, token = _connection_and_token(account)
    data = _call(token, "GET", "/users/me/labels")
    labels = [
        {"id": label.get("id"), "name": label.get("name"),
         "type": label.get("type"), "message_list_visibility":
         label.get("messageListVisibility")}
        for label in data.get("labels") or [] if isinstance(label, dict) and label.get("id")
    ]
    return {"labels": labels, "count": len(labels)}


# --- mutations -----------------------------------------------------------------


def modify(account, message_id, *, add_labels=None, remove_labels=None,
           mark_read=False, mark_unread=False, archive=False, trash=False,
           untrash=False):
    """Labels/trash on one message: the CLI's flag names map onto Gmail's
    system labels (read/unread → UNREAD, archive → INBOX) and the
    trash/untrash endpoints."""
    _connection_id, _connection, token = _connection_and_token(account)

    quoted = quote(str(message_id), safe="")
    if trash:
        data = _call(token, "POST", f"/users/me/messages/{quoted}/trash")
    elif untrash:
        data = _call(token, "POST", f"/users/me/messages/{quoted}/untrash")
    else:
        add = list(add_labels or [])
        remove = list(remove_labels or [])
        if mark_read:
            remove.append(UNREAD)
        if mark_unread:
            add.append(UNREAD)
        if archive:
            remove.append(INBOX)
        if not add and not remove:
            raise GmailOpsError("modify needs a change: --mark-read/--mark-unread/"
                                "--archive/--trash/--untrash, or --add-label/--remove-label")
        body = {key: value for key, value in
                (("addLabelIds", add), ("removeLabelIds", remove)) if value}
        data = _call(token, "POST", f"/users/me/messages/{quoted}/modify", body)
    return {"id": data.get("id"), "thread_id": data.get("threadId"),
            "labels": data.get("labelIds") or [], "trashed": trash}


def _attachments_b64(attachments):
    """CLI attachments (filename + base64 content) to (filename, bytes)."""
    pairs = []
    for index, entry in enumerate(attachments or []):
        if not isinstance(entry, dict) or not str(entry.get("filename") or "").strip():
            raise GmailOpsError(f"attachment {index} needs a filename")
        try:
            data = _b64url_decode(entry.get("content_b64") or "")
        except (ValueError, TypeError):
            raise GmailOpsError(
                f"attachment {entry.get('filename')!r} content is not valid base64") from None
        pairs.append((str(entry["filename"]), data))
    return pairs


def _sender(token):
    profile = _call(token, "GET", "/users/me/profile")
    sender = str(profile.get("emailAddress") or "").strip()
    if not sender:
        raise GmailOpsError("could not resolve the account's address: "
                            "users/me/profile returned no emailAddress", status=502)
    return sender


def _parse_addresses(value):
    if value in (None, "", []):
        return []
    if isinstance(value, str):
        return [address for _name, address in getaddresses([value]) if address]
    if isinstance(value, (list, tuple)):
        return [address for _name, address in getaddresses([str(item) for item in value]) if address]
    raise GmailOpsError("recipients must be a string or list of addresses")


def _compose(token, *, to, cc=None, bcc=None, subject, text=None, html=None,
             attachments=None, in_reply_to=None, references=None):
    """The base64url raw MIME document for a send/draft."""
    if not to:
        raise GmailOpsError("a to address is required")
    if not text and not html:
        raise GmailOpsError("a text or html body is required")
    sender = _sender(token)
    message = _raw_message(sender, _parse_addresses(to), _parse_addresses(cc),
                           _parse_addresses(bcc), [], subject, text or "", html or "",
                           _attachments_b64(attachments), "", in_reply_to=in_reply_to,
                           references=references)
    for part, spec in zip(message.iter_attachments(), attachments or []):
        mime = spec.get("mime_type")
        if mime:
            if not re.fullmatch(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+", str(mime)):
                raise GmailOpsError("attachment MIME type must be type/subtype")
            part.set_type(mime)
    return _b64url_encode(message.as_bytes())


def send(account, *, to, cc=None, bcc=None, subject=None, text=None, html=None,
         attachments=None):
    """Direct send (users.messages.send), attachments included."""
    _connection_id, _connection, token = _connection_and_token(account)
    raw = _compose(token, to=to, cc=cc, bcc=bcc, subject=subject or "(no subject)",
                   text=text, html=html, attachments=attachments)
    data = _call(token, "POST", "/users/me/messages/send", {"raw": raw})
    return {"message_id": data.get("id"), "thread_id": data.get("threadId"),
            "account": _connection_id, "to": _parse_addresses(to), "subject": subject}


def reply(account, message_id, *, reply_all=False, text=None, html=None,
          attachments=None, draft=False, to=None, cc=None, bcc=None):
    """Reply to one message in its thread: recipients from Reply-To/From
    (reply-all adds the original To+Cc minus the account itself), ``Re:``
    subject, In-Reply-To/References from the original, and the original
    threadId so Gmail threads it. ``draft`` parks it as a reply draft
    instead of sending."""
    _connection_id, _connection, token = _connection_and_token(account)

    original = _call(token, "GET",
                     f"/users/me/messages/{quote(str(message_id), safe='')}?format=full")
    headers = _headers_of(original.get("payload") if isinstance(original.get("payload"), dict) else {})
    sender = _sender(token)
    reply_address = headers.get("reply-to") or headers.get("from") or ""
    recipients = _parse_addresses(reply_address)
    if reply_all:
        everyone = _parse_addresses(headers.get("to")) + _parse_addresses(headers.get("cc"))
        seen = {address.lower() for address in recipients}
        for address in everyone:
            if address.lower() not in seen and address.lower() != sender.lower():
                recipients.append(address)
                seen.add(address.lower())
    if to is not None:
        recipients = _parse_addresses(to)
    if not recipients:
        raise GmailOpsError("the original message names no reply address", status=502)
    subject = headers.get("subject") or "(no subject)"
    if not re.match(r"^\s*(re|aw|fwd)\s*:", subject, re.IGNORECASE):
        subject = "Re: " + subject
    message_id_header = (headers.get("message-id") or "").strip()
    references = " ".join(part for part in
                          (headers.get("references") or "", message_id_header) if part.strip())
    raw = _compose(token, to=", ".join(recipients), cc=cc, bcc=bcc, subject=subject, text=text,
                   html=html, attachments=attachments,
                   in_reply_to=message_id_header or None,
                   references=references or None)
    if draft:
        data = _call(token, "POST", "/users/me/drafts", {
            "message": {"raw": raw, "threadId": original.get("threadId")}})
        return {"draft_id": data.get("id"), "thread_id": (data.get("message") or {}).get("threadId"),
                "reply_to": recipients, "subject": subject, "draft": True,
                "original_message_id": original.get("id")}
    data = _call(token, "POST", "/users/me/messages/send",
                 {"raw": raw, "threadId": original.get("threadId")})
    return {"message_id": data.get("id"), "thread_id": data.get("threadId"),
            "reply_to": recipients, "subject": subject,
            "original_message_id": original.get("id")}


def create_draft(account, *, to, cc=None, bcc=None, subject=None, text=None,
                 html=None, attachments=None):
    _connection_id, _connection, token = _connection_and_token(account)
    raw = _compose(token, to=to, cc=cc, bcc=bcc, subject=subject or "(no subject)",
                   text=text, html=html, attachments=attachments)
    data = _call(token, "POST", "/users/me/drafts", {"message": {"raw": raw}})
    return _draft_view(data)


def update_draft(account, draft_id, *, to, cc=None, bcc=None, subject=None,
                 text=None, html=None, attachments=None):
    """Replace draft content while retaining its reply thread and headers.

    Body and attachments are a full replacement; omitted recipients and
    subject retain their stored values so a reply draft stays a reply.
    """
    _connection_id, _connection, token = _connection_and_token(account)
    path = f"/users/me/drafts/{quote(str(draft_id), safe='')}"
    stored = _call(token, "GET", path)
    message = stored.get("message") or {}
    headers = _headers_of(message.get("payload") or {})
    raw = _compose(token, to=to if to is not None else headers.get("to"),
                   cc=cc if cc is not None else headers.get("cc"),
                   bcc=bcc if bcc is not None else headers.get("bcc"),
                   subject=subject if subject is not None else headers.get("subject", "(no subject)"),
                   text=text, html=html, attachments=attachments,
                   in_reply_to=headers.get("in-reply-to"), references=headers.get("references"))
    replacement = {"raw": raw}
    if message.get("threadId"):
        replacement["threadId"] = message["threadId"]
    data = _call(token, "PUT", path, {"message": replacement})
    return _draft_view(data)


def list_drafts(account, max_results=DEFAULT_PAGE_SIZE, page_token=None):
    _connection_id, _connection, token = _connection_and_token(account)

    data = _call(token, "GET", f"/users/me/drafts?{_page_params(max_results, page_token)}")
    drafts = []
    for stub in data.get("drafts") or []:
        if not (isinstance(stub, dict) and stub.get("id")):
            continue
        full = _call(token, "GET", f"/users/me/drafts/{quote(str(stub['id']), safe='')}")
        drafts.append(_draft_view(full))
    return {"drafts": drafts, "count": len(drafts),
            "next_page_token": data.get("nextPageToken")}


def get_draft(account, draft_id):
    _connection_id, _connection, token = _connection_and_token(account)

    data = _call(token, "GET", f"/users/me/drafts/{quote(str(draft_id), safe='')}")
    return _draft_view(data)


def delete_draft(account, draft_id):
    _connection_id, _connection, token = _connection_and_token(account)

    _call(token, "DELETE", f"/users/me/drafts/{quote(str(draft_id), safe='')}")
    return {"deleted": draft_id, "account": _connection_id}


def send_draft(account, draft_id):
    """Send an existing draft (drafts.send) — the amendment's third path."""
    _connection_id, _connection, token = _connection_and_token(account)

    stored = _call(token, "GET", f"/users/me/drafts/{quote(str(draft_id), safe='')}")
    data = _call(token, "POST", "/users/me/drafts/send", {"id": stored.get("id")})
    sent = data if data.get("id") else (data.get("message") or {})
    return {"message_id": sent.get("id"), "thread_id": sent.get("threadId"),
            "account": _connection_id, "draft_id": draft_id}
