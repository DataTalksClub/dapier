import json

from src.dapier.triggers.intake import dropbox_resolver


class FakeDropbox:
    """Canned list_folder responses consumed in call order."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def __call__(self, method, url, *, headers=None, body=None, timeout=15):
        self.calls.append((method, url, json.loads(body.decode())))
        status, payload = self.pages.pop(0)
        return status, json.dumps(payload).encode()

    def urls(self):
        return [url for _method, url, _payload in self.calls]


def file_entry(path, rev, file_id="id:file1", size=10):
    return {
        ".tag": "file",
        "path_lower": path,
        "path_display": path,
        "id": file_id,
        "rev": rev,
        "size": size,
        "content_hash": f"hash-{rev}",
    }


def deleted_entry(path):
    return {".tag": "deleted", "path_lower": path, "path_display": path}


class FakePublish:
    def __init__(self):
        self.events = []

    def __call__(self, event_type, data, *, event_id, occurred_at=None, correlation_id=None):
        self.events.append({
            "event": event_type,
            "data": data,
            "id": event_id,
            "occurred_at": occurred_at,
            "correlation_id": correlation_id,
        })

    def types(self):
        return [item["event"] for item in self.events]

    def ids(self):
        return [item["id"] for item in self.events]


class FakeTable:
    def __init__(self):
        self.items = {}

    def get_item(self, Key):
        return {"Item": self.items.get(Key["cursor_id"])}

    def put_item(self, Item):
        self.items[Item["cursor_id"]] = Item

    def delete_item(self, Key):
        self.items.pop(Key["cursor_id"], None)


class FakeConnectionsTable:
    """Applies the provider/verified_account_id filter like DynamoDB would."""

    def __init__(self, items):
        self.items = items

    def scan(self, **kwargs):
        names = kwargs.get("ExpressionAttributeNames") or {}
        values = kwargs.get("ExpressionAttributeValues") or {}
        provider_field = names.get("#provider", "provider")
        verified_field = names.get("#verified", "verified_account_id")
        matched = [
            item for item in self.items
            if item.get(provider_field) == values.get(":provider")
            and item.get(verified_field) == values.get(":account")
        ]
        return {"Items": matched}


def connection(account="dbid:acct1", status="connected", **extra):
    return {
        "connection_id": "team-dropbox",
        "provider": "dropbox",
        "status": status,
        "verified_account_id": account,
        **extra,
    }


def setup_resolver(monkeypatch, pages, connections_items=None, cursor_table=None):
    """Wire a resolver run against fakes. Returns (transport, publish, cursors)."""
    transport = FakeDropbox(pages)
    publish = FakePublish()
    cursors = cursor_table or FakeTable()
    connections_table = FakeConnectionsTable(
        connections_items if connections_items is not None else [connection()],
    )
    seen = []
    monkeypatch.setattr(
        dropbox_resolver.tokens, "get_access_token",
        lambda conn, *, transport=None: seen.append(conn) or ("token-123", {}),
    )
    return transport, publish, cursors, connections_table, seen


def test_first_notification_starts_fresh_listing(monkeypatch):
    transport, publish, cursors, connections_table, seen = setup_resolver(monkeypatch, [
        (200, {"entries": [
            {".tag": "folder", "path_lower": "/incoming"},
            file_entry("/incoming/invoice.pdf", "r1"),
        ], "cursor": "cursor-1", "has_more": True}),
        (200, {"entries": [deleted_entry("/incoming/old.pdf")],
               "cursor": "cursor-2", "has_more": False}),
    ])

    dropbox_resolver.resolve_account(
        "dbid:acct1", correlation_id="corr-1", occurred_at="2026-09-22T00:00:00+00:00",
        transport=transport, connections_table=connections_table, cursor_table=cursors,
        publish=publish,
    )

    assert seen == [connection()]
    assert transport.urls() == [
        dropbox_resolver.LIST_FOLDER_URL,
        dropbox_resolver.LIST_CONTINUE_URL,
    ]
    method, _url, payload = transport.calls[0]
    assert method == "POST" and payload == {"path": "", "recursive": True}
    assert publish.types() == ["file.created", "file.deleted"]
    created = publish.events[0]
    assert created["id"] == "dropbox:dbid:acct1:id:file1:r1"
    assert created["correlation_id"] == "corr-1"
    assert created["occurred_at"] == "2026-09-22T00:00:00+00:00"
    assert created["data"]["path"] == "/incoming/invoice.pdf"
    assert created["data"]["rev"] == "r1"
    assert publish.events[1]["id"] == "dropbox:dbid:acct1:/incoming/old.pdf:deleted"
    assert cursors.items["dropbox#dbid:acct1"]["cursor"] == "cursor-2"
    assert cursors.items["dropbox#dbid:acct1//incoming/invoice.pdf"]["rev"] == "r1"
    assert "dropbox#dbid:acct1//incoming/old.pdf" not in cursors.items


def test_connection_root_path_scopes_the_fresh_listing(monkeypatch):
    """The listing root is per-connection config: only that subtree resolves."""
    transport, publish, cursors, connections_table, _seen = setup_resolver(monkeypatch, [
        (200, {"entries": [file_entry("/incoming/invoice.pdf", "r1")],
               "cursor": "cursor-1", "has_more": False}),
    ], connections_items=[connection(root_path="/incoming")])

    dropbox_resolver.resolve_account(
        "dbid:acct1", correlation_id="corr-1", transport=transport,
        connections_table=connections_table, cursor_table=cursors, publish=publish,
    )

    _method, _url, payload = transport.calls[0]
    assert payload == {"path": "/incoming", "recursive": True}


def test_known_rev_is_skipped_and_changed_rev_updates(monkeypatch):
    cursors = FakeTable()
    cursors.put_item({
        "cursor_id": "dropbox#dbid:acct1//incoming/invoice.pdf",
        "account_id": "dbid:acct1", "file_id": "id:file1", "rev": "r1",
        "expires_at": 9999999999,
    })
    transport, publish, cursors, connections_table, _seen = setup_resolver(
        monkeypatch, [
            (200, {"entries": [
                file_entry("/incoming/invoice.pdf", "r1"),
                file_entry("/incoming/invoice.pdf", "r2"),
            ], "cursor": "cursor-9", "has_more": False}),
        ],
        cursor_table=cursors,
    )

    dropbox_resolver.resolve_account(
        "dbid:acct1", correlation_id="corr-2",
        transport=transport, connections_table=connections_table,
        cursor_table=cursors, publish=publish,
    )

    assert publish.types() == ["file.updated"]
    assert publish.events[0]["id"] == "dropbox:dbid:acct1:id:file1:r2"
    assert cursors.items["dropbox#dbid:acct1"]["cursor"] == "cursor-9"


def test_replayed_notification_does_not_duplicate_events(monkeypatch):
    pages = [
        (200, {"entries": [file_entry("/incoming/invoice.pdf", "r1")],
               "cursor": "cursor-1", "has_more": False}),
    ]

    def run():
        transport, publish, _cursors, connections_table, _seen = setup_resolver(
            monkeypatch, list(pages), cursor_table=cursors,
        )
        dropbox_resolver.resolve_account(
            "dbid:acct1", correlation_id="corr-3",
            transport=transport, connections_table=connections_table,
            cursor_table=cursors, publish=publish,
        )
        return publish

    cursors = FakeTable()
    first = run()
    second = run()
    assert first.types() == ["file.created"]
    assert second.types() == []


def test_invalid_cursor_falls_back_to_fresh_listing(monkeypatch):
    cursors = FakeTable()
    cursors.put_item({"cursor_id": "dropbox#dbid:acct1", "cursor": "stale"})
    transport, publish, cursors, connections_table, _seen = setup_resolver(monkeypatch, [
        (409, {"error": {".tag": "reset"}}),
        (200, {"entries": [file_entry("/incoming/invoice.pdf", "r1")],
               "cursor": "cursor-fresh", "has_more": False}),
    ], cursor_table=cursors)

    dropbox_resolver.resolve_account(
        "dbid:acct1", correlation_id="corr-4",
        transport=transport, connections_table=connections_table,
        cursor_table=cursors, publish=publish,
    )

    assert transport.urls() == [
        dropbox_resolver.LIST_CONTINUE_URL,
        dropbox_resolver.LIST_FOLDER_URL,
    ]
    assert publish.types() == ["file.created"]
    assert cursors.items["dropbox#dbid:acct1"]["cursor"] == "cursor-fresh"


def test_unknown_account_is_skipped_without_api_calls(monkeypatch):
    transport, publish, cursors, connections_table, seen = setup_resolver(
        monkeypatch, [], connections_items=[],
    )

    dropbox_resolver.resolve_account(
        "dbid:other", correlation_id="corr-5",
        transport=transport, connections_table=connections_table,
        cursor_table=cursors, publish=publish,
    )

    assert seen == []
    assert transport.calls == []
    assert publish.events == []


def test_only_connected_connections_are_used(monkeypatch):
    transport, publish, cursors, connections_table, seen = setup_resolver(
        monkeypatch,
        [(200, {"entries": [], "cursor": "cursor-1", "has_more": False})],
        connections_items=[
            connection(status="ready"),
            connection(account="dbid:acct2"),
        ],
    )

    dropbox_resolver.resolve_account(
        "dbid:acct2", correlation_id="corr-6",
        transport=transport, connections_table=connections_table,
        cursor_table=cursors, publish=publish,
    )

    assert [item["connection_id"] for item in seen] == ["team-dropbox"]


def test_handler_routes_records_and_reports_failures(monkeypatch):
    transport, publish, cursors, connections_table, _seen = setup_resolver(monkeypatch, [
        (200, {"entries": [file_entry("/incoming/invoice.pdf", "r1")],
               "cursor": "cursor-1", "has_more": False}),
    ])
    real_resolve = dropbox_resolver.resolve_account

    def resolve(account_id, *, correlation_id, occurred_at=None, **kwargs):
        return real_resolve(
            account_id, correlation_id=correlation_id, occurred_at=occurred_at,
            transport=transport, connections_table=connections_table,
            cursor_table=cursors, publish=publish,
        )

    monkeypatch.setattr(dropbox_resolver, "resolve_account", resolve)
    result = dropbox_resolver.handler({
        "Records": [
            {"messageId": "m1", "body": json.dumps({
                "schema_version": "1.0",
                "connector": "dropbox",
                "event": "account.changed",
                "correlation_id": "corr-9",
                "occurred_at": "2026-09-22T01:00:00+00:00",
                "data": {"account_id": "dbid:acct1"},
            })},
            {"messageId": "m2", "body": "{not json"},
            {"messageId": "m3", "body": json.dumps({
                "connector": "email", "event": "message.received",
                "data": {"account_id": "dbid:acct1"},
            })},
        ],
    }, None)

    assert result == {"batchItemFailures": [{"itemIdentifier": "m2"}]}
    assert publish.types() == ["file.created"]
    assert publish.events[0]["correlation_id"] == "corr-9"
    assert publish.events[0]["occurred_at"] == "2026-09-22T01:00:00+00:00"
