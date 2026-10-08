"""Every service explains itself: the Services list beside the accounts
shows each service's purpose under its name (no 18px (?) tips)."""
import re
from pathlib import Path

from src.dapier.connections.services import CATALOG

JS = (Path(__file__).resolve().parents[1] / "src" / "web" / "js" / "views" / "connections.js").read_text()


def _blurbs():
    block = re.search(r"const CONNECT_SERVICES = \{(.*?)\n\};", JS, re.S)
    assert block, "CONNECT_SERVICES is missing"
    return dict(re.findall(r"^  ([a-z]+): \{\n    label: '[^']*',\n    blurb: '([^']+)'", block.group(1), re.M)
                | {})


def test_every_catalog_service_has_help_text():
    blurbs = re.findall(r"^  ([a-z]+): \{\n    label: [^\n]*\n    blurb: ", JS, re.M)
    missing = {spec["id"] for spec in CATALOG} - set(blurbs)
    assert not missing, f"services without a blurb: {sorted(missing)}"


def test_services_list_renders_each_blurb():
    render = JS[JS.index('function renderConnectList('):JS.index('function bindConnectButtons(')]
    assert '<span class="connect-blurb">${meta.blurb}' in render
