"""The plugin loader: manifest validation, load semantics, and the real
tree's smoke check (every plugin in plugins/ loads and registers what its
manifest declares)."""
import sys
from pathlib import Path

import pytest

from src.dapier import plugins
from src.dapier.connectors import registry

MANIFEST = """\
name: {name}
version: 0.1.0
core_compat: ">=1.0"
provides:
{provides}
"""

PLUGIN_PY = """\
from src.dapier.connectors.registry import Action, register

register(Action(
    type="{action_type}",
    label="Probe",
    icon="sparkles",
    run=lambda action, event, workflow_id, steps=None: {{"ok": True}},
    required=frozenset(),
    optional=frozenset(),
))
"""


def write_plugin(root, name, *, provides="  {}", action_type=None, plugin_py=None):
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "plugin.yaml").write_text(
        MANIFEST.format(name=name, provides=provides))
    (folder / "plugin.py").write_text(
        plugin_py if plugin_py is not None
        else PLUGIN_PY.format(action_type=action_type or f"{name}_probe"))
    return folder


@pytest.fixture(autouse=True)
def clean_loader_state():
    plugins.reset()
    yield
    plugins.reset()


@pytest.fixture
def plugin_root(tmp_path, monkeypatch):
    monkeypatch.setenv("DAPIER_PLUGINS_DIR", str(tmp_path))
    return tmp_path


def test_missing_root_loads_nothing(plugin_root):
    plugin_root.rmdir()
    assert plugins.load_all() == {}


def test_load_imports_plugin_and_registers_action(plugin_root):
    write_plugin(plugin_root, "probe", action_type="probe_action")
    manifests = plugins.load_all()
    assert set(manifests) == {"probe"}
    try:
        assert "probe_action" in registry.ACTIONS
        # Idempotent: a second call (re-entry from any entry point) neither
        # re-imports nor re-registers.
        assert plugins.load_all() is manifests
        assert registry.ACTIONS["probe_action"].label == "Probe"
    finally:
        registry.ACTIONS.pop("probe_action", None)


def test_manifest_validation(plugin_root):
    write_plugin(plugin_root, "good")
    bad = plugin_root / "good" / "plugin.yaml"

    text = bad.read_text().replace('version: 0.1.0\n', '')
    bad.write_text(text)
    with pytest.raises(plugins.PluginError, match="missing 'version'"):
        plugins.load_all()

    plugins.reset()
    bad.write_text(MANIFEST.format(name="other", provides="  {}"))
    with pytest.raises(plugins.PluginError, match="does not match folder"):
        plugins.load_all()

    plugins.reset()
    bad.write_text(MANIFEST.format(name="good", provides="  rockets: []\n"))
    with pytest.raises(plugins.PluginError, match="unknown provides keys: rockets"):
        plugins.load_all()

    plugins.reset()
    bad.write_text(MANIFEST.format(name="good", provides="  {}"))
    empty = plugin_root / "empty"
    empty.mkdir()
    (empty / "plugin.yaml").write_text(MANIFEST.format(name="empty", provides="  {}"))
    with pytest.raises(plugins.PluginError, match="missing plugin.py"):
        plugins.load_all()


def test_duplicate_providers_rejected(plugin_root):
    # A manifest name is its folder, so names cannot collide — connection
    # providers can: the same provider key from two plugins is the loud one.
    write_plugin(plugin_root, "first", provides="  providers: [slack]\n")
    write_plugin(plugin_root, "second", provides="  providers: [slack]\n")
    with pytest.raises(plugins.PluginError, match="both provide"):
        plugins.load_all()


def test_provides_feed_registry_provider_mappings(plugin_root):
    write_plugin(plugin_root, "probe",
                 provides="  discovery_sources: {probe: [probe-source]}\n"
                          "  test_aliases: {probe2: probe}\n")
    plugins.load_all()
    try:
        assert registry.PROVIDER_DISCOVERY_SOURCES["probe"] == ("probe-source",)
        assert registry.CONNECTION_TEST_ALIASES["probe2"] == "probe"
    finally:
        registry.PROVIDER_DISCOVERY_SOURCES.pop("probe", None)
        registry.CONNECTION_TEST_ALIASES.pop("probe2", None)


def test_core_compat_mismatch_warns_but_loads(plugin_root):
    write_plugin(plugin_root, "probe")
    manifest_path = plugin_root / "probe" / "plugin.yaml"
    manifest_path.write_text(
        manifest_path.read_text().replace('core_compat: ">=1.0"',
                                          'core_compat: ">=99.0"'))
    plugins.load_all()
    assert plugins.compat_warnings() == [
        "plugin probe 0.1.0 declares core_compat >=99.0 but core is "
        f"{plugins.CORE_VERSION}"]
