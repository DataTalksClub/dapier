"""Where each connection earns its keep: the workflows and hook triggers
that reference it.

The connections list stamps every row with ``used_in`` so the console and
the CLI can show what a connection is for and what would stop working if it
went away — and the delete path uses the same map to refuse removing a
connection that live definitions still point at. Two sources count:

- published workflow definitions — any ``connection_id`` anywhere in the
  stored workflow (trigger config, actions, designer flow nodes);
- hook triggers (Telegram bots, Mailchimp) that bind a connection id.

Drafts and version records never count: only live definitions do, the same
set the engine merges.
"""

import os

from . import hook_triggers, published_workflows


def _collect_ids(value, where, found):
    """Depth-first walk collecting every string under a ``connection_id``
    key, tagging each with the part of the definition it sat in."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "connection_id":
                if isinstance(item, str) and item.strip():
                    found.append((item.strip(), where))
            else:
                child = where
                if where == "workflow" and key in ("trigger", "actions", "flow"):
                    child = "trigger" if key == "trigger" else "step"
                _collect_ids(item, child, found)
    elif isinstance(value, list):
        for item in value:
            _collect_ids(item, where, found)


def collect(workflows=None, hooks=None):
    """The usage map: ``{connection_id: [entry, …]}``, entries deduplicated.

    Each entry is ``{"ref": <workflow id or hook name>, "kind":
    "workflow"|"hook", "enabled": <bool>, "where": "trigger"|"step"
    |"workflow"|"hook"}``. Loading degrades to an empty map on a storage
    hiccup — usage labels must never break the list that carries them.
    """
    usage = {}

    def record(connection_id, entry):
        refs = usage.setdefault(connection_id, [])
        if entry not in refs:
            refs.append(entry)

    if workflows is None:
        try:
            workflows = (published_workflows.load_items()
                         if published_workflows.configured() else [])
        except Exception:  # noqa: BLE001 — usage is best-effort metadata
            workflows = []
    for item in workflows:
        workflow = item.get("workflow")
        if not isinstance(workflow, dict) or not workflow.get("id"):
            continue
        found = []
        _collect_ids(workflow, "workflow", found)
        for connection_id, where in found:
            record(connection_id, {
                "ref": str(workflow["id"]),
                "kind": "workflow",
                "enabled": bool(workflow.get("enabled", True)),
                "where": where,
            })

    if hooks is None:
        try:
            hooks = hook_triggers.load_items() if os.environ.get(
                hook_triggers.TABLE_ENV) else []
        except Exception:  # noqa: BLE001 — usage is best-effort metadata
            hooks = []
    for item in hooks:
        connection_id = str(item.get("connection_id") or "").strip()
        if not connection_id:
            continue
        record(connection_id, {
            "ref": str(item.get("hook_id") or item.get("name") or "hook"),
            "kind": "hook",
            "enabled": bool(item.get("enabled", True)),
            "where": "hook",
        })
    return usage


def attach(rows, usage=None):
    """Stamp connection rows with ``used_in`` in place and return them.

    ``usage=None`` (the normal call) loads the map here; pass an explicit
    map (or ``[]`` to force every row empty) when the caller already has
    one. Rows always come back carrying the key, so the console never
    special-cases absence.
    """
    if usage is None:
        usage = collect()
    for row in rows:
        connection_id = row.get("connection_id")
        row["used_in"] = usage.get(connection_id, [])
    return rows
