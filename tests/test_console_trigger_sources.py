"""The console's hardcoded poll-source lists vs the registered sources.

The Triggers view hardcodes two lists — the Source select's options
(index.html) and CONNECTION_POLL_SOURCES (triggers.js, the sources that
must carry a connection_id). This gate pins both to
triggers.poll_sources, so a newly registered source cannot land without
its console surface (the gap this closed: google-drive.updates,
google-drive.deletions, mailchimp.members and slack.messages were
registered but unreachable from the console dialog).
"""
import re
from pathlib import Path

from src.dapier.triggers import poll_sources

WEB = Path(__file__).resolve().parents[1] / "src" / "web"

# Sources that must stay OUT of the connection-requiring list the console
# validates against: credential-backed ones (the three s3 sources share the
# stored aws keys, mailchimp.members the shared mailchimp key or a named
# credential) and rss, which polls a public feed URL and needs no auth at
# all.
CREDENTIAL_SOURCES = {"s3", "s3.updates", "s3.deletions",
                      "mailchimp.members", "rss"}

# Registered without its console dialog entry yet: gmail.messages (the
# Gmail chip's New Email source) belongs in both hardcoded lists below, but
# index.html and triggers.js are mid-change in another writer's working
# tree — the dialog entries land with the next console change and this
# carve-out goes away with them.
CONSOLE_PENDING_SOURCES = {"gmail.messages"}


def _select_sources():
    html = (WEB / "index.html").read_text()
    match = re.search(r'name="source">(.*?)</select>', html, re.S)
    assert match, "the poll dialog's Source select is missing"
    return set(re.findall(r'<option value="([^"]*)"', match.group(1))) - {""}


def _connection_sources():
    js = (WEB / "js" / "views" / "triggers.js").read_text()
    match = re.search(r"CONNECTION_POLL_SOURCES = \[(.*?)\]", js, re.S)
    assert match, "CONNECTION_POLL_SOURCES is missing"
    return set(re.findall(r"'([^']+)'", match.group(1)))


def test_the_source_select_offers_every_registered_poll_source():
    registered = set(poll_sources.source_names()) - CONSOLE_PENDING_SOURCES
    missing = registered - _select_sources()
    assert not missing, f"poll sources missing from the console Source select: {sorted(missing)}"
    unknown = _select_sources() - registered
    assert not unknown, f"Source select names unregistered sources: {sorted(unknown)}"


def test_connection_sources_are_registered_and_exhaustive():
    registered = set(poll_sources.source_names())
    listed = _connection_sources()
    assert listed <= registered, (
        f"CONNECTION_POLL_SOURCES names unregistered sources: {sorted(listed - registered)}")
    missing = registered - CREDENTIAL_SOURCES - CONSOLE_PENDING_SOURCES - listed
    assert not missing, (
        "connection-backed poll sources missing from CONNECTION_POLL_SOURCES: "
        f"{sorted(missing)}")
