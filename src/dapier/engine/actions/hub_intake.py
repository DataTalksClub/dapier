"""hub_intake action: create a Hub intake task (and optional assign/wake).

Workflows that handle per-bot mailboxes (``dev@``, ``tofu@``) or other dapier
events use this instead of inventing a second notification path. Token and
URL come from env / the DataOps team secrets store — never from the workflow YAML.
"""

from __future__ import annotations

from ...triggers.intake import developer_webhook
from .templating import render


def run_hub_intake(action, event, *, steps=None, transport=None):
    title = render(str(action.get("title") or "dapier event"), event, steps)
    text = render(str(action.get("text") or "{trigger}"), event, steps)
    if text == "{trigger}" or not str(action.get("text") or "").strip():
        # Default: compact JSON of the trigger event for Hub operators.
        import json
        text = json.dumps(event, ensure_ascii=False, sort_keys=True, default=str)[:8000]
    project = render(str(action.get("project") or "dapier"), event, steps)
    key = render(str(action.get("key") or ""), event, steps).strip()
    source_type = render(str(action.get("source_type") or "forwarded"), event, steps)
    source_reference = render(str(action.get("source_reference") or ""), event, steps)
    wake = action.get("wake", True)
    if isinstance(wake, str):
        wake = wake.strip().lower() not in ("0", "false", "no")
    message = {
        "title": title,
        "text": text,
        "project": project,
        "wake": bool(wake),
        "source_type": source_type,
    }
    if key:
        message["key"] = key
    if source_reference:
        message["source_reference"] = source_reference
    payload = developer_webhook.build_intake_payload(message, default_project=project)
    status, created = developer_webhook.create_hub_intake(payload, transport=transport)
    if status >= 300:
        raise RuntimeError(f"hub intake returned HTTP {status}: {created}")
    task = created.get("task") if isinstance(created, dict) else None
    task_id = (task or {}).get("id") if isinstance(task, dict) else None
    version = (task or {}).get("version") if isinstance(task, dict) else None
    assign = None
    if wake and task_id and version is not None:
        assign_status, assign_body = developer_webhook.assign_hub_task(
            task_id,
            expected_version=version,
            key=f"{payload['key']}-assign",
            transport=transport,
        )
        assign = {"hub_status": assign_status, "body": assign_body}
    return {
        "hub_status": status,
        "hub_task_id": task_id,
        "intake_key": payload["key"],
        "assign": assign,
        "body": created,
    }
