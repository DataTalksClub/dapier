"""Transport regressions: the real body must reach AI, including closing ads.

The completion is stubbed; these tests verify content delivery and failure
semantics, not a model's ability to identify a paid placement.
"""
from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from src.dapier.connectors import ai, email_content

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((ROOT / "tests/fixtures/newsletters/referenced-body.json").read_text())
ACTION = yaml.safe_load((ROOT / "workflows/newsletter-sponsors.yaml").read_text())["actions"][0]


class MailStorage:
    def __init__(self, objects):
        self.objects = objects
        self.reads = []

    def get_object(self, *, Bucket, Key):
        self.reads.append((Bucket, Key))
        return {"Body": BytesIO(self.objects[Key])}


def complete(event, storage):
    requests = []

    def transport(method, url, *, headers, body, timeout):
        requests.append(json.loads(body))
        reply = json.dumps({"newsletter": "Example Weekly", "sponsors": FIXTURE["expected_sponsors"]})
        return 200, json.dumps({"choices": [{"message": {"content": reply}}]}).encode()

    config = {"token": "fixture-key", "base_url": "https://llm.example.test/v1"}
    with patch.object(ai, "_configuration", return_value=config), patch("boto3.client", return_value=storage):
        output = ai.run_ai_complete(ACTION, event, transport=transport)
    return requests, output


def test_referenced_bodies_reach_the_shipped_prompt_in_full():
    event = deepcopy(FIXTURE["event"])
    original = deepcopy(event)
    # The paid placement is beyond the legacy intake's 60k clipping point.
    text = ("Editorial context. " * 4_000) + FIXTURE["text"]
    storage = MailStorage({"text.txt": text.encode(), "html.txt": FIXTURE["html"].encode()})
    requests, output = complete(event, storage)
    prompt = requests[0]["messages"][-1]["content"]
    assert text in prompt
    assert FIXTURE["html"] in prompt
    assert output["data"]["sponsors"] == FIXTURE["expected_sponsors"]
    assert event == original  # Stored trigger/step input remains small.
    assert storage.reads == [("fixture-mail", "text.txt"), ("fixture-mail", "html.txt")]


@pytest.mark.parametrize("objects", [
    {},  # Deleted object or denied read must not become sponsors: [].
    {"text.txt": b"\xff"},
    {"text.txt": b"x" * (email_content.MAX_BODY_BYTES + 1)},
])
def test_unavailable_or_incomplete_content_stops_before_ai(objects):
    storage = MailStorage(objects)
    with patch.object(ai, "render") as render:
        with pytest.raises(ValueError, match="email body"):
            complete(deepcopy(FIXTURE["event"]), storage)
    render.assert_not_called()


def test_html_only_referenced_email_is_visible_to_the_model():
    event = deepcopy(FIXTURE["event"])
    event["data"]["body"]["text"] = {"value": "", "size": 0}
    requests, _ = complete(event, MailStorage({"html.txt": FIXTURE["html"].encode()}))
    assert FIXTURE["html"] in requests[0]["messages"][-1]["content"]


def test_inline_and_other_connector_events_need_no_storage():
    for event in ({"connector": "email", "data": {"text": "Inline editorial"}},
                  {"connector": "webhook", "data": {"body": FIXTURE["event"]["data"]["body"]}}):
        storage = MailStorage({})
        requests, _ = complete(event, storage)
        assert requests
        assert storage.reads == []


def test_slim_email_resolves_raw_mime_instead_of_classifying_only_subject():
    event = {"connector": "email", "data": {
        "subject": "Example Weekly", "raw_mime": {"bucket": "fixture-mail", "key": "raw.eml"}}}
    raw = b"Content-Type: text/plain; charset=utf-8\r\n\r\n" + FIXTURE["text"].encode()
    requests, _ = complete(event, MailStorage({"raw.eml": raw}))
    assert FIXTURE["text"].strip() in requests[0]["messages"][-1]["content"]
    assert "text" not in event["data"]
