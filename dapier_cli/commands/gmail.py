"""Implementation of the `dapier gmail` commands.

A thin client over ``/api/agent/gmail/*`` (the slack pattern): every call
names its ``account`` explicitly, the connection token stays server-side,
and bodies/attachments ride stdin/files on the CLI side as base64 for the
JSON API. Nothing here talks to Google directly.
"""

import base64
import hashlib
import tempfile
import mimetypes
import os
import sys
from urllib.parse import urlencode

from .. import api
from .shared import preview_body, print_json

__all__ = [
    "gmail_accounts", "gmail_search", "gmail_list", "gmail_read",
    "gmail_attachment", "gmail_threads", "gmail_thread", "gmail_labels",
    "gmail_mark", "gmail_send", "gmail_reply",
    "gmail_draft_list", "gmail_draft_get", "gmail_draft_delete",
    "gmail_draft_send", "gmail_draft_create", "gmail_draft_update",
]

BASE = "/api/agent/gmail"


# Agent-to-agent mail must never leave Alexey's own mailboxes (intake a7a0e545,
# Alexey t2167u-t2169u). The only email fallback between agents is
# dev@dtcdev.click -> orin@dtcdev.click through the dev relay.
AGENT_DOMAIN = "dtcdev.click"


def _is_alexey_account(account):
    value = (account or "").strip().lower()
    if not value:
        return True  # unspecified account could resolve to Alexey's mailbox
    local = value.split("@", 1)[0]
    return local.startswith("alexey") or "grigoriev" in local


def refuse_agent_mail(account, recipients=(), subject=None):
    """Raise loudly when an Alexey mailbox would send agent-to-agent mail."""
    if not _is_alexey_account(account):
        return
    addresses = [a.strip().lower() for r in recipients if r for a in str(r).replace(";", ",").split(",") if a.strip()]
    agent = [a for a in addresses if a.rsplit("@", 1)[-1].strip("> ") == AGENT_DOMAIN]
    handoff = "[handoff]" in (subject or "").lower()
    if agent or handoff:
        raise api.ApiError(
            "REFUSED: agent-to-agent/handoff mail is never sent from Alexey's mailbox "
            f"(account {account or '<default>'}; to {', '.join(agent) or 'n/a'}). "
            "Use Hub intake notes or SendToAgent; the only email fallback is "
            "dev@dtcdev.click -> orin@dtcdev.click via bots-management scripts/dev-relay-mail.py")


def _guard_message(api_url, message_id, account, debug):
    """Reply guard: the recipients are resolved server-side, so read the original."""
    if not _is_alexey_account(account):
        return
    path = BASE + "/messages/" + message_id + ("?" + urlencode({"account": account}) if account else "")
    data = api.call(api_url, "GET", path, debug=debug)
    refuse_agent_mail(account, [data.get("from"), data.get("to"), data.get("cc")], data.get("subject"))


def _body_text(text, file):
    if text is not None:
        return text
    if file:
        if file == "-":
            return sys.stdin.read()
        with open(file, encoding="utf-8") as handle:
            return handle.read()
    return None


def parse_attach(spec):
    """``PATH[;MIME]`` → {filename, content_b64, mime_type} (content read now)."""
    path, _, mime = str(spec).partition(";")
    path = path.strip()
    if not path:
        raise api.ApiError(f"attachment {spec!r} names no file")
    filename = os.path.basename(path)
    with open(path, "rb") as handle:
        content = handle.read()
    return {
        "filename": filename,
        "mime_type": mime.strip() or mimetypes.guess_type(filename)[0]
        or "application/octet-stream",
        "content_b64": base64.b64encode(content).decode("ascii"),
    }


def _attachments(specs):
    return [parse_attach(spec) for spec in (specs or [])]


def gmail_accounts(api_url, as_json=False, debug=False):
    data = api.call(api_url, "GET", BASE + "/accounts", debug=debug)
    accounts = data.get("accounts") or []
    if as_json:
        print_json(data)
        return 0
    if not accounts:
        print("No Google connections serve Gmail. Connect one first "
              "(dapier connections create).")
        return 0
    print(f"{'CONNECTION':<24} {'EMAIL':<36} TITLE / REFS")
    for item in accounts:
        refs = ", ".join(item.get("refs") or [])
        title = " / ".join(part for part in (item.get("title"), refs) if part)
        print(f"{str(item.get('connection_id') or ''):<24} "
              f"{str(item.get('email') or '(not signed in)'):<36} {title}")
    return 0


def gmail_search(api_url, query, account=None, max_results=25, label=None,
                 page_token=None, as_json=False, debug=False):
    return _list_messages(api_url, query=query, account=account,
                          max_results=max_results, label=label,
                          page_token=page_token, as_json=as_json, debug=debug)


def gmail_list(api_url, account=None, max_results=25, label=None,
               page_token=None, as_json=False, debug=False):
    return _list_messages(api_url, query=None, account=account,
                          max_results=max_results, label=label,
                          page_token=page_token, as_json=as_json, debug=debug)


def _list_messages(api_url, query, account, max_results, label, page_token, as_json, debug):
    params = {"max": int(max_results)}
    if query:
        params["q"] = query
    if account:
        params["account"] = account
    if label:
        params["label"] = label
    if page_token:
        params["page_token"] = page_token
    data = api.call(api_url, "GET", BASE + "/messages?" + urlencode(params), debug=debug)
    messages = data.get("messages") or []
    if as_json:
        print_json(data)
        return 0
    print(f"{'ID':<20} {'FROM':<32} {'DATE':<26} SUBJECT")
    for item in messages:
        print(f"{str(item.get('id') or ''):<20} "
              f"{str(item.get('from') or '')[:32]:<32} "
              f"{str(item.get('date') or '')[:26]:<26} "
              f"{str(item.get('subject') or '(no subject)')[:60]}")
    print(f"({data.get('count') or 0} messages)")
    if data.get("next_page_token"):
        print(f"next page: --page-token {data['next_page_token']}")
    return 0


def gmail_read(api_url, message_id, account=None, include_html=False, as_json=False,
               save_attachments=None, debug=False):
    params = {}
    if account:
        params["account"] = account
    if include_html:
        params["html"] = "true"
    path = BASE + "/messages/" + message_id
    if params:
        path += "?" + urlencode(params)
    data = api.call(api_url, "GET", path, debug=debug)
    if save_attachments:
        _save_all_attachments(api_url, data, save_attachments, account, debug)
    if as_json:
        print_json(data)
        return 0
    header_lines = [
        f"From:    {data.get('from') or ''}",
        f"To:      {data.get('to') or ''}",
    ]
    if data.get("cc"):
        header_lines.append(f"Cc:      {data.get('cc')}")
    header_lines.append(f"Date:    {data.get('date') or ''}")
    header_lines.append(f"Subject: {data.get('subject') or '(no subject)'}")
    header_lines.append(f"ID:      {data.get('id')}  thread: {data.get('thread_id')}")
    print("\n".join(header_lines))
    labels = ", ".join(data.get("labels") or [])
    if labels:
        print(f"Labels:  {labels}")
    attachments = data.get("attachments") or []
    if attachments:
        print("Attachments:")
        for item in attachments:
            print(f"  {item.get('attachment_id')}  {item.get('filename') or '(unnamed)'} "
                  f"({item.get('mime_type')}, {item.get('size')} bytes)")
    print()
    if data.get("text"):
        print(data["text"])
    elif include_html and data.get("html"):
        print(data["html"])
    elif data.get("snippet"):
        print(f"[no text part] {data['snippet']}")
    if save_attachments:
        print(f"(attachments saved to {save_attachments})")
    return 0


def _decode_attachment(value):
    value = str(value or "")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _download_attachment(api_url, message_id, attachment_id, account, target, debug):
    """Bounded API responses; replace the output only after complete integrity checks."""
    params = {"chunk_size": 1024 * 1024}
    if account:
        params["account"] = account
    path = BASE + f"/messages/{message_id}/attachments/{attachment_id}"
    offset, expected_size, expected_sha = 0, None, None
    digest = hashlib.sha256()
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=os.path.dirname(os.path.abspath(target)), delete=False) as handle:
            temporary = handle.name
            while True:
                blob = api.call(api_url, "GET", path + "?" + urlencode(params), debug=debug)
                content = _decode_attachment(blob.get("data_b64"))
                if blob.get("chunked"):
                    size, sha = blob.get("size"), blob.get("sha256")
                    if not isinstance(size, int) or size < 0 or not isinstance(sha, str) or len(sha) != 64:
                        raise api.ApiError("Attachment chunk has invalid integrity metadata")
                    if expected_size is None:
                        expected_size, expected_sha = size, sha
                    if (size, sha) != (expected_size, expected_sha):
                        raise api.ApiError("Attachment changed during download")
                    if (blob.get("offset") != offset or blob.get("chunk_size") != len(content)
                            or len(content) != min(1024 * 1024, size - offset)):
                        raise api.ApiError("Attachment chunk offset or size mismatch")
                    following = blob.get("next_offset")
                    if following is not None and (not content or following != offset + len(content)):
                        raise api.ApiError("Attachment chunk did not advance")
                else:
                    if offset:
                        raise api.ApiError("Attachment chunk protocol changed during download")
                    # A small attachment from a previous server release remains readable.
                    expected_size, expected_sha = blob.get("size", len(content)), None
                    following = None
                handle.write(content)
                digest.update(content)
                offset += len(content)
                if following is None:
                    break
                params["offset"] = following
            if offset != expected_size or (expected_sha and digest.hexdigest() != expected_sha):
                raise api.ApiError("Attachment size or SHA256 verification failed")
        os.replace(temporary, target)
        temporary = None
        return offset
    finally:
        if temporary is not None:
            os.unlink(temporary)


def _save_all_attachments(api_url, message, directory, account, debug):
    os.makedirs(directory, exist_ok=True)
    for item in message.get("attachments") or []:
        attachment_id = item.get("attachment_id")
        if not attachment_id:
            continue
        filename = item.get("filename") or f"attachment-{attachment_id}"
        _download_attachment(api_url, message["id"], attachment_id, account,
                             os.path.join(directory, os.path.basename(filename)), debug)


def gmail_attachment(api_url, message_id, attachment_id, account=None, out=None, debug=False):
    target = out or f"attachment-{attachment_id}"
    size = _download_attachment(api_url, message_id, attachment_id, account, target, debug)
    print(f"saved {size} bytes to {target}")
    return 0


def gmail_threads(api_url, query=None, account=None, max_results=25,
                  page_token=None, debug=False):
    params = {"max": int(max_results)}
    if query:
        params["q"] = query
    if account:
        params["account"] = account
    if page_token:
        params["page_token"] = page_token
    data = api.call(api_url, "GET", BASE + "/threads?" + urlencode(params), debug=debug)
    print_json(data)
    return 0


def gmail_thread(api_url, thread_id, account=None, as_json=False, debug=False):
    params = {"account": account} if account else None
    data = api.call(api_url, "GET", BASE + f"/threads/{thread_id}"
                    + ("?" + urlencode(params) if params else ""), debug=debug)
    if as_json:
        print_json(data)
        return 0
    for message in data.get("messages") or []:
        print(f"--- {message.get('from')}  {message.get('date')}")
        print(f"    {message.get('subject') or '(no subject)'}")
        text = message.get("text")
        if text:
            for line in text.splitlines()[:20]:
                print(f"    {line}")
        print()
    return 0


def gmail_labels(api_url, account=None, as_json=False, debug=False):
    params = {"account": account} if account else None
    data = api.call(api_url, "GET", BASE + "/labels"
                    + ("?" + urlencode(params) if params else ""), debug=debug)
    if as_json:
        print_json(data)
        return 0
    for label in data.get("labels") or []:
        print(f"{str(label.get('id') or ''):<28} {label.get('name') or ''} "
              f"({label.get('type') or ''})")
    return 0


def gmail_mark(api_url, message_id, account=None, read=False, unread=False,
               archive=False, trash=False, untrash=False, add_label=None,
               remove_label=None, as_json=False, debug=False):
    body = {}
    if account:
        body["account"] = account
    if read:
        body["mark_read"] = True
    if unread:
        body["mark_unread"] = True
    if archive:
        body["archive"] = True
    if trash:
        body["trash"] = True
    if untrash:
        body["untrash"] = True
    if add_label:
        body["add_labels"] = list(add_label)
    if remove_label:
        body["remove_labels"] = list(remove_label)
    data = api.call(api_url, "POST", BASE + f"/messages/{message_id}/modify",
                    body=body, debug=debug)
    if as_json:
        print_json(data)
    else:
        print(f"ok: {message_id} now carries: {', '.join(data.get('labels') or [])}")
    return 0


def _print_sent(data, as_json):
    if as_json:
        print_json(data)
        return
    kind = "draft" if data.get("draft") else "sent"
    print(f"ok: {kind} message {data.get('message_id') or data.get('draft_id')} "
          f"in thread {data.get('thread_id')}")


def gmail_send(api_url, account=None, to=None, cc=None, bcc=None, subject=None,
               text=None, file=None, attach=None, as_json=False, preview=False,
               debug=False):
    refuse_agent_mail(account, [to, cc, bcc], subject)
    body = {
        "to": to,
        "subject": subject,
        "attachments": _attachments(attach),
    }
    if account:
        body["account"] = account
    if cc:
        body["cc"] = cc
    if bcc:
        body["bcc"] = bcc
    text = _body_text(text, file)
    if text:
        body["text"] = text
    if preview:
        return preview_body(body)
    data = api.call(api_url, "POST", BASE + "/send", body=body, debug=debug)
    _print_sent(data, as_json)
    return 0


def gmail_reply(api_url, message_id, account=None, reply_all=False, text=None,
                file=None, attach=None, draft=False, as_json=False, preview=False,
                debug=False, to=None, cc=None, bcc=None):
    refuse_agent_mail(account, [to, cc, bcc])
    _guard_message(api_url, message_id, account, debug)
    body = {"message_id": message_id, "reply_all": bool(reply_all),
            "draft": bool(draft), "attachments": _attachments(attach)}
    for name, value in (("to", to), ("cc", cc), ("bcc", bcc)):
        if value is not None:
            body[name] = value
    if account:
        body["account"] = account
    text = _body_text(text, file)
    if text:
        body["text"] = text
    if preview:
        return preview_body(body)
    data = api.call(api_url, "POST", BASE + "/reply", body=body, debug=debug)
    _print_sent(data, as_json)
    return 0


def gmail_draft_list(api_url, account=None, as_json=False, debug=False,
                     max_results=25, page_token=None):
    params = {"max": max_results}
    if page_token:
        params["page_token"] = page_token
    if account:
        params["account"] = account
    data = api.call(api_url, "GET", BASE + "/drafts?" + urlencode(params), debug=debug)
    drafts = data.get("drafts") or []
    if as_json:
        print_json(data)
        return 0
    print(f"{'DRAFT ID':<24} {'TO':<36} SUBJECT")
    for item in drafts:
        print(f"{str(item.get('id') or ''):<24} "
              f"{str(item.get('to') or ''):<36} "
              f"{str(item.get('subject') or '(no subject)')[:60]}")
    print(f"({data.get('count') or 0} drafts)")
    if data.get("next_page_token"):
        print(f"next page: --page-token {data['next_page_token']}")
    return 0


def gmail_draft_get(api_url, draft_id, account=None, as_json=False, debug=False,
                     include_html=False):
    params = {"account": account} if account else None
    data = api.call(api_url, "GET", BASE + f"/drafts/{draft_id}"
                    + ("?" + urlencode(params) if params else ""), debug=debug)
    if as_json:
        print_json(data)
        return 0
    print(f"Draft {data.get('id')}  (thread {data.get('thread_id')})")
    print(f"To:      {data.get('to') or ''}")
    if data.get("cc"):
        print(f"Cc:      {data.get('cc')}")
    print(f"Subject: {data.get('subject') or '(no subject)'}")
    body = data.get("html") if include_html else data.get("text")
    if body:
        print()
        print(body)
    return 0


def gmail_draft_delete(api_url, draft_id, account=None, debug=False):
    params = {"account": account} if account else None
    api.call(api_url, "DELETE", BASE + f"/drafts/{draft_id}"
             + ("?" + urlencode(params) if params else ""), debug=debug)
    print(f"ok: draft {draft_id} deleted")
    return 0


def gmail_draft_send(api_url, draft_id, account=None, as_json=False, debug=False):
    if _is_alexey_account(account):
        params = {"account": account} if account else None
        draft = api.call(api_url, "GET", BASE + f"/drafts/{draft_id}"
                         + ("?" + urlencode(params) if params else ""), debug=debug)
        refuse_agent_mail(account, [draft.get("to"), draft.get("cc"), draft.get("bcc")], draft.get("subject"))
    body = {"account": account} if account else None
    data = api.call(api_url, "POST", BASE + f"/drafts/{draft_id}/send",
                    body=body or {}, debug=debug)
    _print_sent(data, as_json)
    return 0


def _draft_body(account, to, cc, bcc, subject, text, file, attach, html_file=None):
    if file == "-" and html_file == "-":
        raise api.ApiError("--file and --html-file cannot both read stdin")
    body = {"to": to, "subject": subject, "attachments": _attachments(attach)}
    if account:
        body["account"] = account
    if cc:
        body["cc"] = cc
    if bcc:
        body["bcc"] = bcc
    text = _body_text(text, file)
    if text:
        body["text"] = text
    if html_file:
        body["html"] = _body_text(None, html_file)
    return body


def gmail_draft_create(api_url, account=None, to=None, cc=None, bcc=None,
                       subject=None, text=None, file=None, attach=None,
                       as_json=False, preview=False, debug=False, html_file=None):
    refuse_agent_mail(account, [to, cc, bcc], subject)
    if preview:
        return preview_body(_draft_body(account, to, cc, bcc, subject, text, file, attach, html_file))
    data = api.call(api_url, "POST", BASE + "/drafts",
                    body=_draft_body(account, to, cc, bcc, subject, text, file, attach, html_file),
                    debug=debug)
    if as_json:
        print_json(data)
        return 0
    print(f"ok: draft {data.get('id')} created "
          f"({'send it: dapier gmail draft send ' + str(data.get('id'))})")
    return 0


def gmail_draft_update(api_url, draft_id, account=None, to=None, cc=None,
                       bcc=None, subject=None, text=None, file=None, attach=None,
                       as_json=False, preview=False, debug=False, html_file=None):
    refuse_agent_mail(account, [to, cc, bcc], subject)
    if preview:
        return preview_body(_draft_body(account, to, cc, bcc, subject, text, file, attach, html_file))
    params = {"account": account} if account else None
    data = api.call(api_url, "PUT", BASE + f"/drafts/{draft_id}"
                    + ("?" + urlencode(params) if params else ""),
                    body=_draft_body(account, to, cc, bcc, subject, text, file, attach, html_file),
                    debug=debug)
    if as_json:
        print_json(data)
        return 0
    print(f"ok: draft {data.get('id')} updated")
    return 0
