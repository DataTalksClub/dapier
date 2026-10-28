"""dakit account chrome (docs/shell.md §5.4, §6): one account row at the
foot of the sidebar opens an upward disclosure with identity, the Dark
mode switch, the region, and Sign out — no destinations in it."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from src.dapier.api.router import _static

ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "src/web/index.html").read_text()
CSS = (ROOT / "src/web/app.css").read_text()
ACCOUNT_JS = (ROOT / "src/web/js/account.js").read_text()
THEME_JS = (ROOT / "src/web/js/theme.js").read_text()
MAIN_JS = (ROOT / "src/web/js/main.js").read_text()


def _foot():
    return INDEX.split('<div class="dk-sidebar__foot">', 1)[1].split("</aside>", 1)[0]


def _menu():
    return INDEX.split('<div id="account-menu"', 1)[1].split('<div id="dk-live"', 1)[0]


def test_sidebar_foot_is_one_account_row_and_the_collapse_toggle():
    foot = _foot()
    buttons = re.findall(r'<button [^>]*id="([^"]+)"', foot)
    assert buttons == ["account-button", "sidebar-toggle"]
    assert 'class="dk-account-trigger"' in foot
    assert 'data-dk-popover-trigger="account-menu"' in foot
    assert 'aria-controls="account-menu"' in foot
    assert 'data-dk-sidebar-toggle' in foot
    # Region, refresh and "Updated" left the sidebar (shell.md §5.4, §13).
    for gone in ("eu-west-1", 'id="refresh"', 'id="last-updated"', "Sign out", "Dark mode"):
        assert gone not in foot


def test_account_menu_is_a_dakit_disclosure_without_destinations():
    menu = _menu()
    assert 'class="dk-menu dk-account-menu"' in INDEX
    assert "data-dk-popover" in INDEX.split('<div id="account-menu"', 1)[1].split(">", 1)[0]
    assert "dk-menu__identity" in menu
    assert 'role="switch"' in menu and "data-dk-theme-toggle" in menu
    assert "Dark mode" in menu and "dk-switch" in menu
    assert "dk-menu__meta" in menu and "eu-west-1" in menu
    assert "dk-menu__item--danger" in menu and "Sign out" in menu
    # A menu, not a dialog: no header band, no close button, no subtitles,
    # and no destinations (every page lives in the sidebar nav).
    assert 'role="dialog"' not in menu
    assert "account-menu-close" not in menu and "aria-label=\"Close" not in menu
    assert "<small>" not in menu and "Signed in as" not in menu
    assert "<a " not in menu


def test_theme_and_popover_behaviour_come_from_the_kit():
    assert INDEX.count('<script src="/assets/vendor/shell.js"></script>') == 1
    assert "dakit-theme" in THEME_JS
    assert "data-dk-theme-toggle" in THEME_JS
    assert "from './account.js'" in MAIN_JS
    assert "bindAccountChrome()" in MAIN_JS
    assert "applyAccountIdentity(me)" in MAIN_JS
    assert "/auth/logout" in ACCOUNT_JS
    assert "confirmDesignerLeave" in ACCOUNT_JS
    # The console no longer reimplements open/close, Esc or outside clicks.
    assert "addEventListener('keydown'" not in ACCOUNT_JS
    assert "Escape" not in ACCOUNT_JS
    response = _static("/assets/vendor/shell.js")
    assert response is not None and response["statusCode"] == 200


def test_old_account_and_sidebar_css_is_gone():
    for selector in (".account-menu", ".account-button", ".account-avatar", ".sidebar-foot",
                     ".app-shell", ".topbar", ".menu-toggle", ".nav-count", ".workspace-mark"):
        assert selector not in CSS, selector


def test_identity_helpers_derive_name_and_initials_from_email():
    if not shutil.which("node"):
        pytest.skip("node is needed to evaluate account identity helpers")
    source = "\n".join(line for line in ACCOUNT_JS.splitlines() if not line.startswith("import "))
    source = source.replace("export function", "function")
    script = source + """
const cases = [
  { username: 'grace@datatalks.club', name: displayNameFromUsername('grace@datatalks.club'), initials: accountInitials(displayNameFromUsername('grace@datatalks.club')) },
  { username: 'alexey.grin@datatalks.club', name: displayNameFromUsername('alexey.grin@datatalks.club'), initials: accountInitials(displayNameFromUsername('alexey.grin@datatalks.club')) },
  { username: '', name: displayNameFromUsername(''), initials: accountInitials(displayNameFromUsername('')) },
];
console.log(JSON.stringify(cases));
"""
    result = json.loads(subprocess.check_output(["node", "--input-type=module", "-e", script], text=True))
    assert result[0] == {"username": "grace@datatalks.club", "name": "Grace", "initials": "G"}
    assert result[1] == {"username": "alexey.grin@datatalks.club", "name": "Alexey Grin", "initials": "AG"}
    assert result[2] == {"username": "", "name": "Account", "initials": "A"}


def test_account_identity_prefers_the_name_and_never_shows_a_subject_id():
    if not shutil.which("node"):
        pytest.skip("node is needed to evaluate account identity helpers")
    source = "\n".join(line for line in ACCOUNT_JS.splitlines() if not line.startswith("import "))
    source = source.replace("export function", "function")
    script = source + """
console.log(JSON.stringify([
  accountIdentity({ username: 'alexey@datatalks.club', email: 'alexey@datatalks.club', name: 'Alexey Grigorev' }),
  accountIdentity({ username: 'grace@datatalks.club', email: 'grace@datatalks.club' }),
  accountIdentity({ username: '84b8d488-2001-70be-d465-f84bfd64ef96' }),
]));
"""
    result = json.loads(subprocess.check_output(["node", "--input-type=module", "-e", script], text=True))
    assert result[0] == {"name": "Alexey Grigorev", "email": "alexey@datatalks.club", "initials": "AG"}
    assert result[1] == {"name": "Grace", "email": "grace@datatalks.club", "initials": "G"}
    assert result[2] == {"name": "Operator", "email": "", "initials": "O"}
