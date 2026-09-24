"""Unit tests for operator-issued API tokens (src/api_tokens.py)."""

from src import api_tokens


class TokenTable:
    def __init__(self):
        self.items = {}

    def put_item(self, **kwargs):
        item = kwargs["Item"]
        self.items[item["token_hash"]] = item

    def get_item(self, **kwargs):
        item = self.items.get(kwargs["Key"]["token_hash"])
        return {"Item": dict(item)} if item else {}

    def update_item(self, **kwargs):
        item = self.items.get(kwargs["Key"]["token_hash"])
        if item is not None:
            item["last_used_at"] = kwargs["ExpressionAttributeValues"][":now"]

    def scan(self, **kwargs):
        return {"Items": list(self.items.values())}


def make(table=None, **overrides):
    body = {"token_id": "personal-scheduler", "agent": "personal-scheduler"}
    body.update(overrides)
    return api_tokens.create(body, "operator-1", table_ref=table or TokenTable())


def test_create_returns_plaintext_once_and_stores_only_the_hash():
    table = TokenTable()

    status, payload = make(table)

    assert status == 200
    assert payload["token"].startswith("dap_")
    assert len(payload["token"]) > 40
    assert payload["subject"] == "token:personal-scheduler"
    assert payload["agent"] == "personal-scheduler"
    stored = list(table.items.values())[0]
    assert "token" not in stored
    assert len(stored["token_hash"]) == 64
    assert stored["token_hash"] not in payload["token"]


def test_verify_roundtrips_a_presented_token():
    table = TokenTable()
    _, payload = make(table)

    item = api_tokens.verify(payload["token"], table_ref=table)

    assert item["token_id"] == "personal-scheduler"
    assert item["subject"] == "token:personal-scheduler"


def test_verify_rejects_non_dap_and_unknown_values():
    table = TokenTable()
    _, payload = make(table)

    assert api_tokens.verify("eyJhbGciOi.JKYlI.0iLCJ0eXA", table_ref=table) is None
    assert api_tokens.verify("", table_ref=table) is None
    assert api_tokens.verify("dap_" + "a" * 40, table_ref=table) is None
    assert api_tokens.verify(payload["token"][:-1] + "x", table_ref=table) is None


def test_duplicate_token_id_is_rejected_even_after_revocation():
    table = TokenTable()
    make(table)

    status, payload = make(table)
    assert status == 409

    api_tokens.revoke("personal-scheduler", table_ref=table)
    status, payload = make(table)
    assert status == 409


def test_invalid_names_are_rejected():
    status, _ = make(agent="Personal Scheduler!")
    assert status == 400
    status, _ = make(token_id="X")
    assert status == 400
    status, _ = make(agent="")
    assert status == 400


def test_revoke_stops_verification_immediately():
    table = TokenTable()
    _, payload = make(table)

    status, view = api_tokens.revoke("personal-scheduler", table_ref=table)
    assert status == 200
    assert view["revoked_at"]
    assert api_tokens.verify(payload["token"], table_ref=table) is None
    # Revoking again is idempotent.
    assert api_tokens.revoke("personal-scheduler", table_ref=table)[0] == 200


def test_revoke_unknown_token_is_404():
    assert api_tokens.revoke("nope", table_ref=TokenTable())[0] == 404
    assert api_tokens.revoke("", table_ref=TokenTable())[0] == 400


def test_mark_used_stamps_last_used():
    table = TokenTable()
    _, payload = make(table)
    token_hash = list(table.items)[0]

    api_tokens.mark_used(token_hash, table_ref=table)

    assert list(table.items.values())[0]["last_used_at"]


def test_public_view_carries_no_hash():
    table = TokenTable()
    make(table)

    view = api_tokens.public_view(list(table.items.values())[0])

    assert "token_hash" not in view
    assert set(view) == set(api_tokens.PUBLIC_FIELDS)
    assert view["token_prefix"].startswith("dap_")
