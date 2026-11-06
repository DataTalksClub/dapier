"""Slack search uses its documented GET query contract, not JSON POST."""
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from plugins.slack.runners import slack


@pytest.mark.parametrize("count,expected", [(0, "1"), (500, "100"), (None, "20")])
def test_search_encodes_literal_query_and_bounded_count(monkeypatch, count, expected):
    monkeypatch.setattr(slack, "_token_for", lambda action: "synthetic-user-token")
    query = 'in:release-notes "GPU & audio" + café?'
    calls = []

    def transport(method, url, *, headers, body, timeout):
        calls.append((method, url, headers, body))
        assert method == "GET"
        assert urlsplit(url).scheme == "https"
        assert urlsplit(url).netloc == "slack.com"
        assert urlsplit(url).path == "/api/search.messages"
        assert parse_qs(urlsplit(url).query) == {"query": [query], "count": [expected]}
        assert headers == {"authorization": "Bearer synthetic-user-token"}
        assert body is None
        return 200, json.dumps({"ok": True, "messages": {"matches": []}}).encode()

    assert slack.run_slack_find_message({"query": query, "count": count}, {}, transport=transport) == {
        "found": False, "messages": [], "count": 0,
    }
    assert len(calls) == 1
