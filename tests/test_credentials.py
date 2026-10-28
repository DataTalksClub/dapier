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

    def scan(self, **kwargs):
        return {"Items": [dict(item) for item in self.items.values()]}


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
            if current is not None and current.get("version") != expected:
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


def test_api_save_credential_openrouter_keeps_the_key(monkeypatch):
    configure(monkeypatch)
    key = "sk-or-" + "a" * 40
    status, payload = credentials.api_save_credential("openrouter",
                                                      {"api_key": key})
    assert (status, payload) == (200, {"provider": "openrouter", "configured": True})
    assert credentials.get_credential("openrouter") == {"api_key": key}


def test_api_save_credential_openrouter_rejects_non_keys(monkeypatch):
    table = configure(monkeypatch)
    for value in ("nope", "sk-" + "a" * 40, "", "sk-or-short"):
        status, payload = credentials.api_save_credential("openrouter",
                                                          {"api_key": value})
        assert status == 400, value
    assert table.items == {}


def test_conditional_write_rejects_legacy_unversioned_item(monkeypatch):
    table = configure_versioned(monkeypatch)
    table.items["oauth#legacy"] = {"credential_id": "oauth#legacy", "value": {"a": 1}}
    try:
        credentials.put_credential_if_version(
            "oauth#legacy", {"a": 2}, provider="youtube", expected_version=0,
        )
    except credentials.VersionConflict as error:
        assert error.args == ("oauth#legacy",)
    else:
        raise AssertionError("unversioned legacy item should conflict, not be taken over")
    assert credentials.get_credential("oauth#legacy") == {"a": 1}


def test_put_over_legacy_unversioned_record_fails_loudly(monkeypatch):
    table = configure(monkeypatch)
    table.items["legacy"] = {"credential_id": "legacy", "value": {"a": 1}}
    try:
        credentials.put_credential("legacy", {"a": 2}, provider="youtube")
    except KeyError as error:
        assert "version" in str(error)
    else:
        raise AssertionError("missing version should fail loudly, not reset to 1")


def test_credential_shapes_report_key_names_only(monkeypatch):
    configure(monkeypatch)
    credentials.put_credential("aws", {"role_arn": "arn:aws:iam::1:role/r",
                                       "secret_value": "x"}, provider="aws")
    credentials.put_credential("conn-slack-abc", {"token": "xoxb-private",
                                                  "signing_secret": "private"},
                               provider="slack")

    shapes = credentials.credential_shapes()

    assert [shape["credential_id"] for shape in shapes] == ["aws", "conn-slack-abc"]
    assert shapes[0] == {
        "credential_id": "aws", "provider": "aws",
        "keys": ["role_arn", "secret_value"],
        "version": 1, "updated_at": shapes[0]["updated_at"],
    }
    assert shapes[1]["keys"] == ["signing_secret", "token"]
    flat = repr(shapes)
    assert "xoxb-private" not in flat and "private" not in flat


def test_credential_shapes_non_dict_value_has_no_keys(monkeypatch):
    configure(monkeypatch)
    table = credentials._table()
    table.put_item(Item={"credential_id": "legacy", "provider": "slack",
                         "value": "xoxb-raw-token"})

    shapes = credentials.credential_shapes()

    assert shapes == [{"credential_id": "legacy", "provider": "slack",
                       "keys": [], "version": None, "updated_at": None}]
    assert "xoxb-raw-token" not in repr(shapes)
