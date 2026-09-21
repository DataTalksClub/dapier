from src import credentials


class Table:
    def __init__(self):
        self.items = {}
        self.reads = []

    def put_item(self, *, Item):
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
