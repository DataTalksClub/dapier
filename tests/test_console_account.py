"""Family account chrome: identity, theme, and sign-out live in the top bar."""
import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

from src.dapier.api.router import _static

ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "src/web/index.html").read_text()
CSS = (ROOT / "src/web/app.css").read_text()
ACCOUNT_JS = (ROOT / "src/web/js/account.js").read_text()
THEME_JS = (ROOT / "src/web/js/theme.js").read_text()
MAIN_JS = (ROOT / "src/web/js/main.js").read_text()


class TopActions(HTMLParser):
    def __init__(self):
        super().__init__()
        self.markup = ""
        self.account_label = None
        self.account_haspopup = None
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "").split()
        if "top-actions" in classes:
            self._depth = 1
        elif self._depth:
            self._depth += 1
        if attrs.get("id") == "account-button":
            self.account_label = attrs.get("aria-label")
            self.account_haspopup = attrs.get("aria-haspopup")
        if self._depth:
            self.markup += tag + " " + " ".join(f'{key}="{value}"' for key, value in attrs.items()) + "\n"

    def handle_endtag(self, tag):
        if self._depth:
            self._depth -= 1


def test_sidebar_foot_is_region_status_only():
    assert 'id="theme-toggle"' not in INDEX
    assert 'id="logout"' not in INDEX
    assert "sidebar-foot-actions" not in INDEX
    foot = INDEX.split('class="sidebar-foot">', 1)[1].split("</div>", 1)[0]
    assert "eu-west-1" in foot
    assert "theme-toggle" not in foot
    assert "Sign out" not in foot
    assert "data-lucide" not in foot


def test_account_button_lives_in_the_topbar():
    parser = TopActions()
    parser.feed(INDEX)
    assert parser.account_label == "Account"
    assert parser.account_haspopup == "true"
    assert 'id="account-button"' in parser.markup
    assert "account-avatar" in parser.markup
    assert "account-button-name" in parser.markup
    assert "account-chevron" in parser.markup
    assert 'id="refresh"' in parser.markup


def test_account_popover_has_family_sections_without_dataops_only_ones():
    menu = INDEX.split('id="account-menu"', 1)[1].split('id="user-confirm-dialog"', 1)[0]
    assert "Signed in as" in menu
    assert "Appearance" in menu
    assert "Dark mode" in menu
    assert 'id="account-theme-toggle"' in menu
    assert "account-toggle-track" in menu
    assert "Sign out" in menu
    assert "End this browser session." in menu
    assert "Show work for" not in menu
    assert "Workspace" not in menu
    assert "Version" not in menu
    assert "data-lucide" not in menu
    assert 'viewBox="0 0 24 24"' in menu
    assert 'stroke-width="1.8"' in menu


def test_theme_still_uses_dakit_key_and_account_module_is_wired():
    assert "dakit-theme" in THEME_JS
    assert "export function setTheme" in THEME_JS
    assert "export function toggleTheme" in THEME_JS
    assert "from './account.js'" in MAIN_JS
    assert "bindAccountChrome()" in MAIN_JS
    assert "applyAccountIdentity(me)" in MAIN_JS
    assert "$('#logout')" not in MAIN_JS
    assert "$('#theme-toggle')" not in MAIN_JS
    assert "toggleTheme()" in ACCOUNT_JS
    assert "/auth/logout" in ACCOUNT_JS
    response = _static("/assets/js/account.js")
    assert response is not None and response["statusCode"] == 200


def test_account_css_uses_family_tokens_and_mobile_avatar_only():
    account_css = CSS.split("/* Family account chrome", 1)[1].split(".loading {", 1)[0]
    account_css += CSS.split("@media (max-width: 820px)", 1)[1].split("@media", 1)[0]
    hex_colors = re.findall(r"#[0-9a-fA-F]{3,8}", account_css)
    assert hex_colors == [], hex_colors
    assert "--dk-radius-lg" in account_css
    assert "--dk-shadow-overlay" in account_css
    assert "--dk-danger-text" in account_css
    assert "lucide" not in account_css.lower()
    assert ".account-button-name" in account_css
    assert ".account-chevron" in account_css
    assert "display: none" in account_css


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

