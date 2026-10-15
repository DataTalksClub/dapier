"""The Designer canvas, rendered: no box draws an accent perimeter in any
state (dakit "No accent borders"), and no SVG paint resolves to an
undefined token (black text on the dark theme)."""
import pytest

from tests import designer_fixture
from tests.test_console_boot import _chromium, console_server

PROBE = r"""() => {
  const svg = [...document.querySelectorAll('svg')].find((el) => el.querySelector('pattern'));
  const resolve = (value) => {
    const probe = document.createElement('span');
    probe.style.color = value;
    document.body.append(probe);
    const color = getComputedStyle(probe).color;
    probe.remove();
    return color;
  };
  const neutral = new Set(['--dk-border-default', '--dk-border-strong', '--dk-border-control-hover']
    .map((name) => resolve(`var(${name})`)));
  const boxes = [...svg.querySelectorAll('rect')].filter((rect) =>
    !rect.closest('defs') && !(rect.getAttribute('fill') || '').startsWith('url('));
  const perimeters = boxes.map((rect) => {
    const style = getComputedStyle(rect);
    return { stroke: style.stroke, width: parseFloat(style.strokeWidth) || 0 };
  }).filter((box) => box.stroke !== 'none' && box.width > 0);
  const bad = perimeters.filter((box) => !neutral.has(box.stroke) || box.width > 1);
  const texts = [...svg.querySelectorAll('text')].map((text) => getComputedStyle(text).fill);
  return { boxes: boxes.length, perimeters: perimeters.length, bad, texts, black: texts.filter((fill) => fill === 'rgb(0, 0, 0)') };
}"""


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_canvas_boxes_keep_neutral_1px_perimeters_and_real_tokens(theme):
    pytest.importorskip("playwright.sync_api")
    from playwright.sync_api import sync_playwright

    with console_server() as origin, sync_playwright() as playwright:
        browser = _chromium(playwright)
        try:
            page = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme=theme,
                                       bypass_csp=True).new_page()
            page.route("**/api/**", designer_fixture.route)
            page.goto(origin + f"/designer/app?embed=1&theme={theme}&workflow=invoice-triage.yaml",
                      wait_until="domcontentloaded")
            page.locator("svg[viewBox] text", has_text="Union").first.wait_for(timeout=15_000)
            unselected = page.evaluate(PROBE)
            # Select a step: selection is a fill, never an outline.
            page.locator("svg[viewBox] text", has_text="#finance").first.click(force=True)
            page.wait_for_timeout(300)
            selected = page.evaluate(PROBE)
            selected["fills"] = page.evaluate("""() => [...document.querySelectorAll('svg[viewBox] rect')]
              .map((rect) => getComputedStyle(rect).fill)""")
            selected["soft"] = page.evaluate("""() => { const p = document.createElement('span');
              p.style.color = 'var(--dk-accent-soft)'; document.body.append(p);
              const c = getComputedStyle(p).color; p.remove(); return c; }""")
        finally:
            browser.close()
    for state in (unselected, selected):
        assert state["boxes"] >= 5
        assert state["perimeters"] >= 4
        assert state["bad"] == [], state["bad"]
        assert state["black"] == [], state["texts"]
    # The selected step reads as selected through its fill alone.
    assert selected["soft"] in selected["fills"]


def test_standalone_designer_loads_the_kit_once_with_no_inline_script():
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1] / "src/web/designer.html").read_text()
    assert "vendor/dakit.css" not in html          # designer.css inlines the bundle
    assert html.count('href="/assets/designer.css"') == 1
    assert '<script src="/assets/vendor/shell.js"></script>' in html
    assert '<script src="/assets/js/theme-init.js"></script>' in html
    assert "<script>" not in html                    # CSP: script-src 'self'


def test_standalone_designer_sits_in_the_kit_topbar_shell():
    pytest.importorskip("playwright.sync_api")
    from playwright.sync_api import sync_playwright

    with console_server() as origin, sync_playwright() as playwright:
        browser = _chromium(playwright)
        try:
            page = browser.new_context(viewport={"width": 1280, "height": 800}, bypass_csp=True).new_page()
            page.route("**/api/**", designer_fixture.route)
            page.goto(origin + "/designer/app?workflow=invoice-triage.yaml", wait_until="domcontentloaded")
            bar = page.locator(".dk-shell--topbar .dk-shell__bar")
            bar.wait_for(timeout=15_000)
            assert page.locator('.dk-shell__nav [aria-current="page"]').inner_text() == "Designer"
            assert page.locator(".workspace-mark").count() == 0   # no accent letter block
            trigger = page.locator("#account-button")
            page.wait_for_function("document.querySelector('#account-button').getAttribute('aria-label') === 'Account, Grace Hopper'")
            trigger.focus()
            page.keyboard.press("Enter")
            menu = page.locator("#account-menu")
            assert menu.is_visible()
            assert "Sign out" in menu.inner_text() and "Dark mode" in menu.inner_text()
            page.keyboard.press("Escape")
            assert menu.is_hidden()
            assert page.evaluate("document.activeElement.id") == "account-button"
            # Framed by the console, the console's shell is the chrome.
            page.goto(origin + "/designer/app?embed=1&workflow=invoice-triage.yaml", wait_until="domcontentloaded")
            page.locator("svg text", has_text="Union").first.wait_for(timeout=15_000)
            assert page.locator(".dk-shell").count() == 0
        finally:
            browser.close()
