"""Anchors styled as dakit buttons keep their label color on hover."""
from pathlib import Path

CSS = (Path(__file__).resolve().parents[1] / "src" / "web" / "app.css").read_text()


def test_link_buttons_reassert_text_color_on_hover():
    for variant in ("primary", "secondary", "danger"):
        assert f"a.dk-button--{variant}:hover {{ color:" in CSS
