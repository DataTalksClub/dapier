"""The designer's trigger-chip mirror stays in sync with the registry.

The standalone designer app builds its trigger palette from a hand-synced
copy of the connector registry (designer/src/catalog.ts, connectorCatalog)
— the live surfaces read ``GET /api/catalog`` (connectors.registry.catalog)
directly, but the mirror has no runtime link, so a chip registered in
Python (connectors/triggers.py, gmail.py, ai.py) silently misses the
designer palette until someone edits the TS by hand. This file is the
missing link as a test: every registered chip must appear in the mirror
with the same label and the same events (order is presentational), and the
mirror may not carry chips the registry does not know.
"""
import json
import re
from pathlib import Path

import src.dapier.connectors  # noqa: F401  (import = registration)
from src.dapier.connectors import registry

CATALOG_TS = Path(__file__).resolve().parents[1] / "designer" / "src" / "catalog.ts"

ENTRY = re.compile(
    r'\{\s*name:\s*"([^"]+)",\s*label:\s*"([^"]+)",\s*logo:\s*\w+,\s*'
    r"events:\s*\[([^\]]*)\]\s*\}")


def registry_chips():
    return {entry["name"]: entry for entry in registry.catalog()["connectors"]}


def mirrored_chips():
    """connectorCatalog parsed out of the TS source: name → {label, events}.

    Entries are single-line objects; a multi-line entry (or a shape change)
    fails the parse and the test — keep the mirror list machine-readable.
    """
    source = CATALOG_TS.read_text(encoding="utf-8")
    block = re.search(
        r"export const connectorCatalog[^=]*=\s*\[(.*?)\n\];",
        source, re.DOTALL)
    assert block, "connectorCatalog not found in designer/src/catalog.ts"
    chips = {}
    for name, label, events in ENTRY.findall(block.group(1)):
        chips[name] = {
            "label": label,
            "events": sorted(item.strip().strip('"') for item in events.split(",")
                             if item.strip()),
        }
    return chips


def test_the_mirror_carries_every_registered_chip():
    registered, mirrored = registry_chips(), mirrored_chips()
    missing = sorted(set(registered) - set(mirrored))
    extra = sorted(set(mirrored) - set(registered))
    assert not missing, (f"chips registered in Python but missing from the "
                         f"designer mirror (add them to connectorCatalog): "
                         f"{missing}")
    assert not extra, (f"mirror chips no Python module registers (drop them "
                       f"or register the connector): {extra}")


def test_mirrored_events_match_the_registry():
    registered, mirrored = registry_chips(), mirrored_chips()
    drifted = {
        name: (sorted(entry["events"]), mirrored[name]["events"])
        for name, entry in registered.items()
        if name in mirrored and sorted(entry["events"]) != mirrored[name]["events"]
    }
    assert not drifted, (f"event lists drifted between the registry and the "
                         f"designer mirror (registry, mirror): {drifted}")


def test_mirrored_labels_match_the_registry():
    registered, mirrored = registry_chips(), mirrored_chips()
    drifted = {
        name: (entry["label"], mirrored[name]["label"])
        for name, entry in registered.items()
        if name in mirrored and entry["label"] != mirrored[name]["label"]
    }
    assert not drifted, f"labels drifted between the registry and the mirror: {drifted}"


EVENT_INFO_LINE = re.compile(r'^\s*("[^"]+/[^"]+"):\s*(\[.*\]),$', re.MULTILINE)


def mirrored_event_info():
    """connectorEventInfo parsed out of the TS source: "conn/event" → [label, description]."""
    source = CATALOG_TS.read_text(encoding="utf-8")
    block = re.search(
        r"export const connectorEventInfo[^=]*=\s*\{(.*?)\n\};", source, re.DOTALL)
    assert block, "connectorEventInfo not found in designer/src/catalog.ts"
    return {json.loads(key): json.loads(value)
            for key, value in EVENT_INFO_LINE.findall(block.group(1))}


def test_every_registered_event_has_a_description():
    """The Event dropdown (designer) and `dapier catalog` explain each event."""
    undescribed = [
        f"{entry['name']}/{info['event']}"
        for entry in registry.catalog()["connectors"]
        for info in entry["event_info"]
        if not info["description"] or info["label"] == info["event"]
    ]
    assert not undescribed, f"events without a label/description in event_info: {undescribed}"


def test_mirrored_event_info_matches_the_registry():
    registered = {
        f"{entry['name']}/{info['event']}": [info["label"], info["description"]]
        for entry in registry.catalog()["connectors"]
        for info in entry["event_info"]
    }
    assert mirrored_event_info() == registered
