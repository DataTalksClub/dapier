"""Guard the shipped invoice-body-completion workflow file."""
from pathlib import Path

import pytest
import yaml

WORKFLOW_FILE = Path(__file__).resolve().parents[1] / "workflows" / "invoice-body-completion.yaml"


def shipped_workflow():
    return yaml.safe_load(WORKFLOW_FILE.read_text())


def test_the_shipped_completion_workflow_validates():
    from src.dapier.api.designer_store.validation import parse_workflow

    workflow = parse_workflow(WORKFLOW_FILE.read_text())
    assert workflow["trigger"]["connector"] == "renderer"
    assert workflow["trigger"]["filters"]["route"]["equals"] == "invoice"


def test_archive_identity_and_duplicate_skip():
    from src.dapier.engine.actions.code import run_code
    from src.dapier.engine.actions.templating import render
    from src.dapier.engine.code_tests import run_code_tests

    workflow = shipped_workflow()
    ids = [action["id"] for action in workflow["actions"]]
    assert ids == [
        "email-date", "clean-subject", "archive-rendered", "skip-duplicate",
        "intake-rendered",
    ]
    clean_subject = workflow["actions"][1]
    archive = workflow["actions"][2]
    skip = workflow["actions"][3]
    assert archive.get("skip_existing") is True
    assert archive.get("strict_conflict") is True
    assert "steps.email-date" in archive["filename"]
    assert "steps.clean-subject" in archive["filename"]
    assert skip["when"]["steps.archive-rendered.output.already_exists"] == {
        "not_equals": True,
    }

    report = run_code_tests(clean_subject)
    assert report["failed"] == 0, report

    event = {"data": {
        "checksum": "839f39e8a3ba40d9ab870cc949a2340c93c22a2b6a5f652c9e9828196a1d1ed0",
        "source_event": {"data": {
            "subject": "Fwd: Your Google Play Order Receipt from Oct 2, 2026",
            "date": "2026-10-04T01:15:47+00:00",
        }},
    }}
    output = run_code(clean_subject, event)
    steps = {
        "email-date": {"output": {"formatted": "2026-10-04"}},
        "clean-subject": {"output": output},
    }
    name = render(archive["filename"], event, steps)
    assert name == (
        "2026-10-04-Your Google Play Order Receipt from Oct 2, 2026-839f39e8a3ba.pdf"
    )

    with pytest.raises(RuntimeError, match="checksum is required"):
        run_code(clean_subject, {"data": {
            "source_event": {"data": {"subject": "Invoice"}},
        }})
