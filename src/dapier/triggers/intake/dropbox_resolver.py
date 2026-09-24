"""Resolve Dropbox webhook notifications into individual file events.

Dropbox webhooks only announce that an account changed. This Lambda consumes
the notification queue, resolves each account with ``files/list_folder`` and
``files/list_folder/continue`` using the cursor stored in DynamoDB, and
publishes one envelope per change onto the event queue:

- ``file.created`` for an entry never seen before
- ``file.updated`` for an entry whose ``rev`` changed
- ``file.deleted`` for a deleted path (its stored file state is removed)

Event ids are deterministic (``dropbox:{account_id}:{file_id}:{rev}``), so a
replayed notification cannot re-fire downstream workflow actions. The cursor
is advanced only after every delta page has been processed, so a crash
re-processes the same delta instead of skipping it; the deterministic ids
keep that replay harmless. The per-account bearer token comes from the
connection store via ``tokens.get_access_token``, which refreshes and
enforces the provider-account binding before any call.
"""

import json
import logging
import os
import time
import urllib.request
from datetime import datetime, timezone

import boto3

from ...connections import tokens
from ...connections import records as connections


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

LIST_FOLDER_URL = "https://api.dropboxapi.com/2/files/list_folder"
LIST_CONTINUE_URL = "https://api.dropboxapi.com/2/files/list_folder/continue"

FILE_STATE_TTL_DAYS = 90


class ResolverError(Exception):
    """A Dropbox list_folder call failed; the notification should be retried."""


class CursorReset(ResolverError):
    """Dropbox rejected the stored cursor; a fresh listing is required."""


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _default_transport(method, url, *, headers, body, timeout=15):
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, response.read()


def _call(url, payload, access_token, transport):
    body = json.dumps(payload).encode()
    try:
        status, raw = (transport or _default_transport)(
            "POST", url,
            headers={
                "authorization": f"Bearer {access_token}",
                "content-type": "application/json",
            },
            body=body,
        )
    except Exception as exc:
        raise ResolverError(f"dropbox list_folder unreachable: {type(exc).__name__}")
    try:
        data = json.loads(raw.decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        raise ResolverError(f"dropbox list_folder returned HTTP {status} with an unreadable body")
    if status >= 300:
        tag = (data.get("error") or {}).get(".tag") if isinstance(data, dict) else None
        if status == 409 and tag in ("reset", "bad_cursor"):
            raise CursorReset(url)
        raise ResolverError(f"dropbox list_folder returned HTTP {status}")
    return data


def list_folder(access_token, path, *, transport=None):
    """Start a fresh recursive delta listing at ``path``."""
    return _call(LIST_FOLDER_URL, {"path": path, "recursive": True}, access_token, transport)


def list_continue(access_token, cursor, *, transport=None):
    """Fetch the next delta page for ``cursor``."""
    return _call(LIST_CONTINUE_URL, {"cursor": cursor}, access_token, transport)


class CursorStore:
    """Cursor and per-file state for one Dropbox account.

    Item layout in the cursors table (HASH key ``cursor_id``):
    - ``dropbox#{account_id}`` holds the live list_folder cursor.
    - ``dropbox#{account_id}/{path_lower}`` holds the last seen ``rev`` of a
      file, so created vs updated can be distinguished across notifications.
    """

    def __init__(self, table, account_id):
        self.table = table
        self.account_id = account_id

    @staticmethod
    def _ttl():
        return int(time.time()) + FILE_STATE_TTL_DAYS * 86400

    def _cursor_key(self):
        return {"cursor_id": f"dropbox#{self.account_id}"}

    def _file_key(self, path_lower):
        return {"cursor_id": f"dropbox#{self.account_id}/{path_lower}"}

    def get_cursor(self):
        item = self.table.get_item(Key=self._cursor_key()).get("Item")
        return item.get("cursor") if item else None

    def put_cursor(self, cursor):
        self.table.put_item(Item={
            **self._cursor_key(),
            "account_id": self.account_id,
            "cursor": cursor,
            "expires_at": self._ttl(),
        })

    def get_file(self, path_lower):
        item = self.table.get_item(Key=self._file_key(path_lower)).get("Item")
        if not item:
            return None
        return {"rev": item.get("rev"), "file_id": item.get("file_id")}

    def put_file(self, path_lower, file_id, rev):
        self.table.put_item(Item={
            **self._file_key(path_lower),
            "account_id": self.account_id,
            "file_id": file_id,
            "rev": rev,
            "expires_at": self._ttl(),
        })

    def delete_file(self, path_lower):
        self.table.delete_item(Key=self._file_key(path_lower))


def find_connection(account_id, *, table=None):
    """Return the connected Dropbox connection bound to ``account_id``."""
    table = table or boto3.resource("dynamodb").Table(os.environ["CONNECTIONS_TABLE"])
    kwargs = {
        "FilterExpression": "#provider = :provider AND #verified = :account",
        "ExpressionAttributeNames": {"#provider": "provider", "#verified": "verified_account_id"},
        "ExpressionAttributeValues": {":provider": "dropbox", ":account": account_id},
    }
    items = []
    while True:
        page = table.scan(**kwargs)
        items.extend(page.get("Items", []))
        last_key = page.get("LastEvaluatedKey")
        if not last_key:
            break
        kwargs["ExclusiveStartKey"] = last_key
    for item in items:
        if item.get("status") == connections.STATUS_CONNECTED:
            return item
    return None


def process_entry(store, publish, account_id, entry):
    """Emit zero or one file envelope for ``entry`` and update file state.

    ``publish`` takes ``(event_type, data, event_id)``. Deleting always
    publishes; files publish only when new (``file.created``) or changed
    (``file.updated``).
    """
    path_lower = entry.get("path_lower")
    if not path_lower:
        return
    tag = entry.get(".tag")
    if tag == "deleted":
        publish("file.deleted", {
            "account_id": account_id,
            "path": entry.get("path_display") or path_lower,
            "path_lower": path_lower,
        }, event_id=f"dropbox:{account_id}:{path_lower}:deleted")
        return
    if tag != "file":
        return
    rev = entry.get("rev", "")
    previous = store.get_file(path_lower)
    if previous and previous.get("rev") == rev:
        return
    store.put_file(path_lower, entry.get("id"), rev)
    publish(
        "file.created" if previous is None else "file.updated",
        {
            "account_id": account_id,
            "path": entry.get("path_display") or path_lower,
            "path_lower": path_lower,
            "file_id": entry.get("id"),
            "rev": rev,
            "content_hash": entry.get("content_hash"),
            "size": entry.get("size"),
        },
        event_id=f"dropbox:{account_id}:{entry.get('id')}:{rev}",
    )


def resolve_account(account_id, *, correlation_id, occurred_at=None,
                    transport=None, connections_table=None, cursor_table=None,
                    publish=None):
    """Resolve one notified account into file events on the event queue."""
    connection = find_connection(account_id, table=connections_table)
    if connection is None:
        logger.warning("no connected Dropbox connection for account %s", account_id)
        return
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    store = CursorStore(
        cursor_table or boto3.resource("dynamodb").Table(os.environ["CURSORS_TABLE"]),
        account_id,
    )
    publish = publish or _publish
    occurred_at = occurred_at or _now_iso()

    def emit(event_type, data, *, event_id):
        publish(event_type, data, event_id=event_id, occurred_at=occurred_at,
                correlation_id=correlation_id)

    # Each connection carries its own listing root ("": the whole Dropbox);
    # the resolver scope is per item, not per deployment.
    root_path = str(connection.get("root_path") or "").strip()

    def fresh_page():
        return list_folder(access_token, root_path, transport=transport)

    cursor = store.get_cursor()
    try:
        page = list_continue(access_token, cursor, transport=transport) if cursor else fresh_page()
    except CursorReset:
        page = fresh_page()
    while True:
        for entry in page.get("entries") or []:
            process_entry(store, emit, account_id, entry)
        if not page.get("has_more"):
            break
        try:
            page = list_continue(access_token, page["cursor"], transport=transport)
        except CursorReset:
            page = fresh_page()
    store.put_cursor(page["cursor"])


def _publish(event_type, data, *, event_id, occurred_at, correlation_id):
    envelope = {
        "schema_version": "1.0",
        "id": event_id,
        "correlation_id": correlation_id or event_id,
        "connector": "dropbox",
        "event": event_type,
        "source": data.get("account_id"),
        "occurred_at": occurred_at,
        "data": data,
    }
    boto3.client("sqs").send_message(
        QueueUrl=os.environ["EVENT_QUEUE_URL"],
        MessageBody=json.dumps(envelope),
    )


def handler(event, _context):
    failures = []
    for record in event.get("Records", []):
        try:
            envelope = json.loads(record["body"])
            if envelope.get("connector") != "dropbox" or envelope.get("event") != "account.changed":
                logger.warning("dropping non-dropbox record %s", record.get("messageId"))
                continue
            account_id = (envelope.get("data") or {}).get("account_id")
            if not account_id:
                logger.warning("notification without account id %s", record.get("messageId"))
                continue
            resolve_account(
                account_id,
                correlation_id=envelope.get("correlation_id"),
                occurred_at=envelope.get("occurred_at"),
            )
        except Exception:
            logger.exception("dropbox record failed %s", record.get("messageId"))
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}
