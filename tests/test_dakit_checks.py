"""dakit's checkers over the console (dakit docs/adoption.md): the vendored
copy matches its lock, no accent borders, no raw colours, and spacing/type on
the scales with a shrink-only baseline. Same commands as `make check-dakit`."""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENDOR = "src/web/vendor"


def _check_paths():
    makefile = (ROOT / "Makefile").read_text()
    block = re.search(r"^DAKIT_CHECK_PATHS = (.*?)\n\n", makefile, re.S | re.M).group(1)
    return block.replace("\\\n", " ").split()


def _run(*args):
    r = subprocess.run(["node", *args], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_vendored_dakit_matches_its_lock():
    _run(f"{VENDOR}/sync.mjs", "--check", "--dest", VENDOR)


def test_console_has_no_accent_borders():
    _run(f"{VENDOR}/check-borders.mjs", "src/web", "designer/src")


def test_console_has_no_raw_colours():
    _run(f"{VENDOR}/check-colors.mjs", *_check_paths())


def test_console_spacing_and_type_stay_on_scale():
    _run(f"{VENDOR}/check-scale.mjs", "--baseline", "dakit-scale-baseline.json", *_check_paths())


def test_app_element_rules_never_override_kit_components():
    """App CSS is unlayered, so a bare element rule (a, button, label,
    input, :focus-visible…) beats dakit's @layer dk.components and repaints
    kit nav items, buttons and focus rings. Base element rules must skip
    kit-classed elements or be scoped to an app class."""
    css = re.sub(r"/\*.*?\*/", "", (ROOT / "src/web/app.css").read_text(), flags=re.S)
    bare = []
    for selectors in re.findall(r"(?:^|})\s*([^{}@]+)\{", css):
        for selector in selectors.split(","):
            selector = selector.strip()
            head = re.split(r"[\s>+~]", selector)[0]
            if re.fullmatch(r"(a|button|label|input|select|textarea|summary)?(:[a-z-]+(\([^)]*\))?)*", head) \
                    and head and head != ":root" and 'not([class*="dk-"])' not in selector and "[type=" not in selector:
                bare.append(selector)
    assert bare == [], bare


def test_every_dk_token_the_app_reads_exists():
    """A var(--dk-…) that the kit does not define resolves to nothing — in
    SVG that is black fill on the dark theme (the Designer's old
    --dk-text-secondary / --dk-accent-text). Every --dk-* role the app or
    Designer reads must be defined by the vendored kit or set by the app."""
    kit = (ROOT / VENDOR / "dakit.css").read_text()
    sources = [p for p in (ROOT / "designer/src").rglob("*") if p.suffix in {".ts", ".tsx", ".css"}]
    sources += [ROOT / "src/web/app.css", ROOT / "src/web/public.css"]
    sources += list((ROOT / "src/web/js").rglob("*.js"))
    sources += list((ROOT / "src/web").glob("*.html"))
    text = "\n".join(p.read_text() for p in sources)
    defined = set(re.findall(r"(--dk-[a-z0-9-]+)\s*:", kit + "\n" + text))
    used = set(re.findall(r"var\(\s*(--dk-[a-z0-9-]+)", text))
    assert sorted(used - defined) == []
