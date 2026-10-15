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
