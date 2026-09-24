from src.dapier.connections import credentials


class Table:
    def __init__(self):
        self.items = {}
        self.reads = []

    def put_item(self, *, Item, **kwargs):
        self.items[Item["credential_id"]] = Item

    def get_item(self, **kwargs):
        self.reads.append(kwargs)
        item = self.items.get(kwargs["Key"]["credential_id"])
        if item and "ProjectionExpression" in kwargs:
            item = {key: item[key] for key in ("credential_id", "updated_at")}
        return {"Item": item} if item else {}


def configure(monkeypatch):
    table = Table()

    class Dynamo:
        def Table(self, name):
            assert name == "credentials"
            return table

    monkeypatch.setenv("CREDENTIALS_TABLE", "credentials")
    monkeypatch.setattr(credentials.boto3, "resource", lambda service: Dynamo())
    return table


def test_put_get_and_metadata_only_status(monkeypatch):
    table = configure(monkeypatch)
    credentials.put_credential("slack", {"token": "private"}, provider="slack")

    assert credentials.get_credential("slack") == {"token": "private"}
    status = credentials.credential_status("slack")

    assert status["configured"] is True
    assert status["updated_at"]
    assert table.reads[-1]["ProjectionExpression"] == "credential_id, updated_at"


def test_missing_credential(monkeypatch):
    configure(monkeypatch)
    assert credentials.credential_status("missing") == {"configured": False, "updated_at": None}
    try:
        credentials.get_credential("missing")
    except KeyError as error:
        assert error.args == ("missing",)
    else:
        raise AssertionError("missing credential should fail")


def test_put_bumps_version(monkeypatch):
    configure(monkeypatch)
    credentials.put_credential("slack", {"token": "one"}, provider="slack")
    assert credentials.get_credential_record("slack")["version"] == 1
    credentials.put_credential("slack", {"token": "two"}, provider="slack")
    record = credentials.get_credential_record("slack")
    assert record["version"] == 2
    assert record["value"] == {"token": "two"}


class VersionedTable(Table):
    """Enforces the optimistic-concurrency condition like DynamoDB."""

    def put_item(self, *, Item, ConditionExpression=None, ExpressionAttributeValues=None):
        if ConditionExpression:
            expected = ExpressionAttributeValues[":expected"]
            current = self.items.get(Item["credential_id"])
            current_version = current.get("version") if current else None
            if current_version is None and current is not None:
                current_version = 0 if expected == 0 else "legacy"
            if current is not None and current_version != expected:
                from botocore.exceptions import ClientError

                raise ClientError(
                    {"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem",
                )
        super().put_item(Item=Item)


def configure_versioned(monkeypatch):
    table = VersionedTable()

    class Dynamo:
        def Table(self, name):
            assert name == "credentials"
            return table

    monkeypatch.setenv("CREDENTIALS_TABLE", "credentials")
    monkeypatch.setattr(credentials.boto3, "resource", lambda service: Dynamo())
    return table


def test_conditional_write_succeeds_on_expected_version(monkeypatch):
    configure_versioned(monkeypatch)
    credentials.put_credential("oauth#x", {"a": 1}, provider="youtube")
    credentials.put_credential_if_version(
        "oauth#x", {"a": 2}, provider="youtube", expected_version=1,
    )
    record = credentials.get_credential_record("oauth#x")
    assert record["version"] == 2
    assert record["value"] == {"a": 2}


def test_conditional_write_rejects_stale_version(monkeypatch):
    configure_versioned(monkeypatch)
    credentials.put_credential("oauth#x", {"a": 1}, provider="youtube")
    try:
        credentials.put_credential_if_version(
            "oauth#x", {"stale": True}, provider="youtube", expected_version=0,
        )
    except credentials.VersionConflict as error:
        assert error.args == ("oauth#x",)
    else:
        raise AssertionError("stale write should fail")
    assert credentials.get_credential("oauth#x") == {"a": 1}


def test_conditional_write_accepts_legacy_unversioned_item(monkeypatch):
    table = configure_versioned(monkeypatch)
    table.items["oauth#legacy"] = {"credential_id": "oauth#legacy", "value": {"a": 1}}
    credentials.put_credential_if_version(
        "oauth#legacy", {"a": 2}, provider="youtube", expected_version=0,
    )
    assert credentials.get_credential_record("oauth#legacy")["version"] == 1
