"""The designer's hardcoded catalog mirror vs the connector registry.

The designer (designer/src/catalog.ts) carries a TypeScript copy of the
action catalog, including the per-field ``discover`` picker hints, because
it does not fetch ``/api/catalog``. Two hardcoded copies drift; this test
pins them together: every registry field hint must appear in the designer
mirror and vice versa, every hinted resource must be a registered
discovery, and hints naming a resource with required params must map those
params to sibling fields.
"""
import re
from pathlib import Path

import pytest

from src.dapier.connectors import registry

CATALOG_TS = Path(__file__).resolve().parents[1] / "designer" / "src" / "catalog.ts"

# One designer field entry: its key up to the discover tag, without
# crossing into the next field object.
FIELD_HINT_RE = re.compile(
    r'key:\s*"([a-z0-9_]+)"(?:(?!key:).)*?discover:\s*\{(?:(?!discover:).)*?\}',
    re.S)
RESOURCE_RE = re.compile(r'resource:\s*"([a-z0-9_]+)"')
ACTION_RE = re.compile(r'type:\s*"([a-z0-9_]+)"')

# Field-level `type:` values (text/number/select/…) are not action types;
# only `type:` markers outside that set attribute hints to an action.
FIELD_TYPES = {"text", "number", "textarea", "boolean", "select", "yaml"}


def registry_hints():
    """``(action_type, field_key, bare_resource)`` for every registry hint."""
    hints = set()
    for entry in registry.ACTIONS.values():
        for field in entry.fields:
            discover = field.get("discover")
            if discover:
                resource = str(discover.get("resource", "")).rpartition(".")[2]
                hints.add((entry.type, field["key"], resource))
    return hints


def designer_hints():
    """``(action_type, field_key, resource)`` parsed from catalog.ts."""
    content = CATALOG_TS.read_text()
    actions = [(match.start(), match.group(1))
               for match in ACTION_RE.finditer(content)
               if match.group(1) not in FIELD_TYPES]
    hints = set()
    for match in FIELD_HINT_RE.finditer(content):
        resource = RESOURCE_RE.search(match.group(0))
        if not resource:
            continue
        action = max(
            ((position, name) for position, name in actions if position < match.start()),
            default=(0, ""), key=lambda item: item[0])[1]
        assert action, f"discover hint for {resource.group(1)} outside any action"
        hints.add((action, match.group(1), resource.group(1)))
    return hints


def registered_names():
    return {entry.name for entry in registry.discoveries()}


def test_every_designer_hint_exists_in_the_registry():
    missing = designer_hints() - registry_hints()
    assert not missing, f"designer hints unknown to the registry: {sorted(missing)}"


def test_every_registry_hint_reaches_the_designer():
    missing = registry_hints() - designer_hints()
    assert not missing, f"registry hints missing from catalog.ts: {sorted(missing)}"


def designer_action_types():
    """Action ``type`` values parsed from catalog.ts."""
    content = CATALOG_TS.read_text()
    return {match.group(1) for match in ACTION_RE.finditer(content)
            if match.group(1) not in FIELD_TYPES}


def test_every_registry_action_reaches_the_designer():
    missing = set(registry.ACTIONS) - designer_action_types()
    assert not missing, f"registry actions missing from catalog.ts: {sorted(missing)}"


def test_hinted_resources_are_registered_discoveries():
    unknown = {resource for _, _, resource in designer_hints()} - registered_names()
    assert not unknown, f"hints naming unregistered resources: {sorted(unknown)}"


@pytest.mark.parametrize("resource,required", [
    (entry.name, param.get("key"))
    for entry in registry.discoveries()
    for param in entry.params
    if param.get("required")
])
def test_hints_map_required_params_to_sibling_fields(resource, required):
    content = CATALOG_TS.read_text()
    for match in FIELD_HINT_RE.finditer(content):
        found = RESOURCE_RE.search(match.group(0))
        if found and found.group(1) == resource:
            assert f'"{required}"' in match.group(0), (
                f"the {resource} hint must map the required param "
                f"'{required}' to a sibling field")
