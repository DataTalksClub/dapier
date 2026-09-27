"""YouTube connector: channel, playlist, and playlist-video discovery plus
the YouTube identity check.

YouTube connections are Google OAuth connections (provider "youtube"); the
discovery and health-check runners delegate to the shared provider layer
(``connections.discovery``), which hits the YouTube Data API with the
connection's own token.
"""
from ..connections import discovery as provider
from ..engine.actions.youtube import run_youtube_find_video
from .registry import (
    Action,
    ConnectionTest,
    Discovery,
    register,
    register_connection_test,
    register_discovery,
)

register(Action(
    type="youtube_find_video",
    label="YouTube",
    description="Find the top videos for a search query (YouTube Data API search.list)",
    icon="youtube",
    run=lambda action, event, workflow_id, steps=None: run_youtube_find_video(
        action, event, steps=steps),
    required=frozenset({"connection_id", "query"}),
    optional=frozenset(),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "youtube",
         "required": True},
        {"key": "query", "label": "Search query", "required": True,
         "placeholder": "DataTalks kubernetes"},
    ),
))


def _run_channel(connection, params, *, transport=None):
    return provider.discover(connection, "channel", params, transport=transport)


register_discovery(Discovery(
    name="channel",
    connector="youtube",
    label="Channel",
    description="The connected YouTube channel",
    run=_run_channel,
))


def _run_playlists(connection, params, *, transport=None):
    return provider.discover(connection, "playlists", params, transport=transport)


register_discovery(Discovery(
    name="playlists",
    connector="youtube",
    label="Playlists",
    description="Playlists owned by the channel",
    run=_run_playlists,
))


def _run_playlist_items(connection, params, *, transport=None):
    return provider.discover(connection, "playlist_items", params, transport=transport)


register_discovery(Discovery(
    name="playlist_items",
    connector="youtube",
    label="Playlist videos",
    description="Videos in one playlist, newest first",
    params=({"key": "playlist_id", "label": "Playlist ID", "type": "text",
             "required": True},),
    run=_run_playlist_items,
))


def _run_test(connection, *, transport=None):
    return provider.test_connection(connection, transport=transport)


register_connection_test(ConnectionTest(connector="youtube", run=_run_test))
