"""Workflow reuse stays available through Duplicate after removing the gallery."""
from pathlib import Path


WEB = Path(__file__).resolve().parents[1] / "src" / "web"


def test_workflows_list_duplicate_calls_the_api():
    js = (WEB / "js/views/overview.js").read_text()
    assert 'class="dk-button dk-button--secondary workflow-duplicate"' in js
    assert "designer/workflows/${encodeURIComponent(button.dataset.file)}/duplicate" in js
    assert ".workflow-duplicate" in js
