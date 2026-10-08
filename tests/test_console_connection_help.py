"""Every Connections register group explains itself behind a (?) tip."""
import re
from pathlib import Path

from src.dapier.connections.services import CATALOG

JS = (Path(__file__).resolve().parents[1] / "src" / "web" / "js" / "views" / "connections.js").read_text()


def _help_ids():
    block = re.search(r"const SERVICE_HELP = \{(.*?)\n\};", JS, re.S)
    assert block, "SERVICE_HELP is missing"
    return set(re.findall(r"^\s+([a-z]+):", block.group(1), re.M))


def test_every_catalog_service_has_help_text():
    # Zoom's two kinds carry their description from the API's services.
    missing = {spec["id"] for spec in CATALOG if spec["id"] != "zoom"} - _help_ids()
    assert not missing, f"register groups without (?) help: {sorted(missing)}"


def test_group_titles_render_the_help_tip():
    assert "helpTip(service?.description || SERVICE_HELP[serviceId])" in JS
