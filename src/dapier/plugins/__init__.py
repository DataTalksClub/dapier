"""Plugin loader: integrations live in ``plugins/<name>/`` with a manifest.

Each plugin folder carries ``plugin.yaml`` (identity and what it provides)
and ``plugin.py`` whose import registers everything it owns — actions,
trigger chips, connection-scoped discovery, health checks, trigger-sample
discovery, poll sources — the same import == registration idiom the core
connectors use (see connectors/registry.py for the contract). Core
(src/dapier) never imports plugin code: this loader is the only bridge, and
the engine, the API, and the CLI keep reading the registry they always have.

Entry points call :func:`load_all` once (the connectors package does it at
import; poll sources do it lazily so a scheduled fire never depends on the
connectors package being loaded first). It is idempotent and reentrant-safe;
failures are loud: a bad manifest or a missing module raises PluginError at
load time rather than dropping a connector silently.
"""
import importlib.util
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

#: The core's version, checked against each manifest's ``core_compat``.
CORE_VERSION = "1.0.0"

#: What a manifest may declare under ``provides``. Discovery sources and
#: test aliases are applied into the registry's provider mappings; the rest
#: document the plugin's surface (and are validated for shape only — the
#: registrations themselves happen from ``plugin.py``).
PROVIDES_KEYS = ("providers", "provider_aliases", "discovery_sources",
                 "test_aliases", "trigger_chips", "poll_sources",
                 "ingress", "intake_handlers")


class PluginError(Exception):
    """A plugin manifest or module violates the plugin contract."""


@dataclass
class Manifest:
    name: str
    version: str
    core_compat: str
    provides: dict


def plugins_root():
    """The folder holding the plugin directories.

    The default is ``<repo/bundle root>/plugins`` — three parents above this
    file (``src/dapier/plugins/__init__.py``), matching the bundle-root rule
    the workflows dir uses, so ``sam build`` copies (``CodeUri: .``) and the
    Lambda bundle both resolve to the same tree. ``DAPIER_PLUGINS_DIR``
    overrides it.
    """
    override = os.environ.get("DAPIER_PLUGINS_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[3] / "plugins"


def load_manifest(path):
    """Parse and validate one ``plugin.yaml``."""
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise PluginError(f"{path}: manifest must be a mapping")
    name = str(data.get("name") or "").strip()
    if not name:
        raise PluginError(f"{path}: manifest is missing 'name'")
    if path.parent.name != name:
        raise PluginError(
            f"{path}: manifest name {name!r} does not match folder "
            f"{path.parent.name!r}")
    version = str(data.get("version") or "").strip()
    if not version:
        raise PluginError(f"{path}: manifest is missing 'version'")
    compat = str(data.get("core_compat") or "").strip()
    if not compat:
        raise PluginError(f"{path}: manifest is missing 'core_compat'")
    provides = data.get("provides") or {}
    if not isinstance(provides, dict):
        raise PluginError(f"{path}: 'provides' must be a mapping")
    unknown = sorted(str(key) for key in provides if key not in PROVIDES_KEYS)
    if unknown:
        raise PluginError(f"{path}: unknown provides keys: {', '.join(unknown)}")
    for key in ("discovery_sources", "test_aliases", "provider_aliases"):
        if not isinstance(provides.get(key) or {}, dict):
            raise PluginError(f"{path}: provides.{key} must be a mapping")
    return Manifest(name=name, version=version, core_compat=compat,
                    provides=provides)


def _version_tuple(value):
    parts = []
    for piece in str(value).strip().lstrip(">").lstrip("=").strip().split("."):
        try:
            parts.append(int(piece))
        except ValueError:
            break
    return tuple(parts)


def core_compat_ok(manifest):
    """Whether this core satisfies the manifest's ``core_compat`` range.

    Only ``>=X.Y[.Z]`` (a bare ``X.Y[.Z]`` reads the same) — the range we
    need while plugins and core ride the same mono-repo deploy.
    """
    want, have = _version_tuple(manifest.core_compat), _version_tuple(CORE_VERSION)
    width = max(len(want), len(have))
    want += (0,) * (width - len(want))
    have += (0,) * (width - len(have))
    return have >= want


_state = {"loaded": False, "loading": False, "manifests": {}, "warnings": []}


def load_all(root=None):
    """Import every plugin once (import == registration) and apply manifests.

    Safe to call from any entry point: the first call loads, reentrant calls
    made while a load is in flight are no-ops (a plugin importing the
    connectors package re-enters here), later calls return the manifests.
    A missing plugins root is not an error — a bare core with no plugins is
    a valid deployment.
    """
    if _state["loaded"] or _state["loading"]:
        return _state["manifests"]
    base = Path(root) if root else plugins_root()
    if not base.is_dir():
        _state["loaded"] = True
        return _state["manifests"]
    _state["loading"] = True
    try:
        _root_on_path(base)
        manifests = {}
        for manifest_path in sorted(base.glob("*/plugin.yaml")):
            manifest = load_manifest(manifest_path)
            if manifest.name in manifests:
                raise PluginError(f"duplicate plugin name {manifest.name!r}")
            manifests[manifest.name] = manifest
        _check_providers_unique(manifests)
        for name, manifest in manifests.items():
            _import_plugin(base, name)
            _apply_provides(manifest)
            if not core_compat_ok(manifest):
                _state["warnings"].append(
                    f"plugin {name} {manifest.version} declares core_compat "
                    f"{manifest.core_compat} but core is {CORE_VERSION}")
        _state["manifests"] = manifests
    except Exception:
        _state["loading"] = False
        raise
    _state["loading"] = False
    _state["loaded"] = True
    return _state["manifests"]


def loaded_manifests():
    """``{name: Manifest}`` for the plugins this process has loaded."""
    return dict(_state["manifests"])


def compat_warnings():
    """core_compat mismatches seen at load time (load_all still succeeded)."""
    return list(_state["warnings"])


def reset():
    """Test seam: forget the load. Registered registry entries are the
    caller's teardown responsibility (the registry's dicts are the plugins'
    output, not the loader's state)."""
    _state.update(loaded=False, loading=False, manifests={}, warnings=[])


def _root_on_path(base):
    # plugin.py imports its runners as `plugins.<name>...` and core as
    # `src.dapier...`; both need the repo/bundle root importable. The Lambda
    # bundle root is already on sys.path (handlers are src.dapier.*); this
    # covers CLI and test contexts.
    root = str(base.parent)
    if root not in sys.path:
        sys.path.insert(0, root)


def _check_providers_unique(manifests):
    seen = {}
    for name, manifest in manifests.items():
        for provider in manifest.provides.get("providers") or []:
            provider = str(provider)
            if provider in seen:
                raise PluginError(
                    f"plugins {seen[provider]!r} and {name!r} both provide "
                    f"connection provider {provider!r}")
            seen[provider] = name


def _import_plugin(base, name):
    plugin_py = base / name / "plugin.py"
    if not plugin_py.is_file():
        raise PluginError(f"plugin {name!r}: missing plugin.py")
    # Load under the module name tests and runners import —
    # ``plugins.<name>.plugin`` — so there is exactly one module object per
    # plugin: whichever side imports first wins sys.modules, and the
    # registry's registered closures and a test's patch targets always
    # belong to the same module. spec_from_file_location bypasses sys.path,
    # so a loader-test plugin root without a plugins/ package on it works.
    module_name = f"plugins.{name}.plugin"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, plugin_py)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


def _apply_provides(manifest):
    from src.dapier.connectors import registry

    for provider, sources in (manifest.provides.get("discovery_sources") or {}).items():
        registry.PROVIDER_DISCOVERY_SOURCES[str(provider)] = tuple(sources)
    registry.CONNECTION_TEST_ALIASES.update({
        str(alias): str(target)
        for alias, target in (manifest.provides.get("test_aliases") or {}).items()
    })
