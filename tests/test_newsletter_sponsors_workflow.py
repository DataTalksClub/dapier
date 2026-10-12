"""Guard the shipped newsletter-sponsors workflow file.

The canonical `workflows/newsletter-sponsors.yaml` must satisfy the same
save-time validation the API enforces, stay disabled until sender-policy
lands, and keep the extract-sponsors prompt pinned to the gold paid-only
rules (contract 2026-10-10). Substrings only — the private labeled corpus
is not in git.
"""
from pathlib import Path

import yaml

from src.dapier.api.designer_store.validation import parse_workflow
from src.dapier.connectors.registry import validate_action_chain

WORKFLOW_FILE = Path(__file__).resolve().parents[1] / "workflows" / "newsletter-sponsors.yaml"
TEMPLATE_FILE = Path(__file__).resolve().parents[1] / "template.yaml"


def shipped_workflow():
    return yaml.safe_load(WORKFLOW_FILE.read_text())


def extract_action():
    for action in shipped_workflow()["actions"]:
        if action.get("id") == "extract-sponsors":
            return action
    raise AssertionError("extract-sponsors step missing")


def test_the_shipped_newsletter_sponsors_workflow_validates():
    workflow = parse_workflow(WORKFLOW_FILE.read_text())
    assert workflow["id"] == "newsletter-sponsors"
    assert workflow["enabled"] is False
    assert workflow["trigger"]["connector"] == "email"
    assert workflow["trigger"]["event"] == "message.received"
    assert workflow["trigger"]["filters"]["route"]["equals"] == "newsletter"
    assert [action["id"] for action in workflow["actions"]] == [
        "extract-sponsors", "forward-leads"]
    forward = workflow["actions"][1]
    assert forward["type"] == "dataops"
    assert forward["connection_id"] == "dataops"
    assert "url" not in forward
    assert "url_env" not in forward
    assert forward["auth_secret_id"] == "dapier/dataops"
    assert forward["extraction_step"] == "extract-sponsors"
    assert forward["on_fail"] == "continue"
    validate_action_chain(workflow["actions"])


def test_extract_sponsors_schema_keeps_category_optional():
    action = extract_action()
    assert action["type"] == "ai_complete"
    assert action["model"] == "claude-sonnet-5-5"
    assert "temperature" not in action
    schema = action["json_schema"]
    assert schema["required"] == ["newsletter", "sponsors"]
    item = schema["properties"]["sponsors"]["items"]
    assert item["required"] == ["name"]
    assert "category" not in item["required"]
    assert item["properties"]["category"]["enum"] == [
        "tool", "product", "service", "event", "course", "book",
        "job", "community", "other"]


def test_extract_sponsors_prompt_encodes_gold_paid_only_rules():
    action = extract_action()
    prompt = action["prompt"]
    system = action["system"]
    lowered = prompt.lower()

    assert "paid placements only" in lowered or "paid sponsor placements" in lowered
    assert "self-promo" in lowered
    assert "own job board" in lowered
    assert "https" in lowered and "sponsor's own site" in lowered
    assert "when in doubt, omit" in lowered
    assert "sponsor this newsletter" in lowered
    assert "substack.com/redirect" in lowered

    # Body templating covers SES string html/text and Datamailer's
    # {html: {value: ...}} / body.text.value so the model sees the issue.
    assert "From: {sender}" in prompt
    assert "Subject: {subject}" in prompt
    assert "{text}" in prompt
    assert "{body.text.value}" in prompt
    assert "{html.value}" in prompt
    assert "{html}" in prompt
    assert "Body:\n    {body}" not in prompt

    assert "paid newsletter sponsor placements only" in system.lower()
    assert "empty sponsors is valid" in system.lower()


def test_template_leaves_intake_url_to_the_connection():
    text = TEMPLATE_FILE.read_text(encoding="utf-8")
    deploy = (Path(__file__).resolve().parents[1] / "scripts" / "deploy.sh").read_text(
        encoding="utf-8")
    assert "DataOpsLeadsUrl:" not in text
    assert "DataOpsIntakeUrl:" not in text
    assert "DATAOPS_LEADS_URL" not in text
    assert "DATAOPS_INTAKE_URL" not in text
    assert "DataOpsIntakeUrl" not in deploy
