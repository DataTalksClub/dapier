import json
from unittest.mock import patch

import pytest

from plugins.zoom.runners import artifacts
from src.dapier.connectors.registry import ACTIONS
from src.dapier.connections.zoom import API_DEFAULT_SCOPES


@pytest.fixture
def zoom():
    with patch("plugins.zoom.runners._zoom_connection", return_value={}), patch.object(
        artifacts.tokens, "get_access_token", return_value=("fake-token", {})
    ), patch("src.dapier.engine.actions.base._default_transport") as transport:
        yield transport


@pytest.mark.parametrize(("meeting_id", "encoded"), [
    (86574830782, "86574830782"),
    ("x6Fb48YsR7ylVj9cDPa+Jw==", "x6Fb48YsR7ylVj9cDPa%2BJw%3D%3D"),
    ("/abc==", "%252Fabc%253D%253D"),
    ("a//b==", "a%252F%252Fb%253D%253D"),
])
@pytest.mark.parametrize(("kind", "endpoint"), [
    ("zoom_get_meeting_transcript", "transcript"),
    ("zoom_get_meeting_summary", "meeting_summary"),
])
def test_meeting_artifact_dispatch(zoom, kind, endpoint, meeting_id, encoded):
    payload = {"meeting_topic": "Meeting"}
    zoom.return_value = 200, json.dumps(payload).encode()
    assert ACTIONS[kind].run(
        {"connection_id": "zoom-api", "meeting_id": meeting_id}, {}, "flow"
    ) == payload
    assert zoom.call_args.args == ("GET", f"https://api.zoom.us/v2/meetings/{encoded}/{endpoint}")
    assert zoom.call_args.kwargs["headers"] == {"authorization": "Bearer fake-token"}


def test_summary_list_pagination_and_zoom_error(zoom):
    run = ACTIONS["zoom_list_meeting_summaries"].run
    action = {"connection_id": "zoom-api", "next_page_token": "{cursor}"}
    zoom.return_value = 200, b'{"summaries":[],"next_page_token":"next"}'
    assert run(action, {"data": {"cursor": "a+b="}}, "flow")["next_page_token"] == "next"
    assert zoom.call_args.args == (
        "GET", "https://api.zoom.us/v2/users/me/meeting_summaries?next_page_token=a%2Bb%3D"
    )

    zoom.return_value = 400, b'{"code":4711,"message":"Missing scopes"}'
    with pytest.raises(RuntimeError, match=r"HTTP 400 .*4711.*Missing scopes"):
        run(action, {"data": {"cursor": "next"}}, "flow")


def test_meeting_artifact_default_scopes():
    assert {
        "cloud_recording:read:meeting_transcript",
        "meeting:read:summary",
        "meeting:read:list_summaries",
    }.issubset(API_DEFAULT_SCOPES)
