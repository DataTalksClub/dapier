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

# Trigger connectors that are their own provider: a workflow triggered on
# ``youtube.video.published`` rides the provider's connection even when its
# config carries no explicit connection_id (delivery resolves the connection
# by provider). Connectors on multi-connection providers (google) are not
# here — a provider match would be ambiguous with three Google rows.
CONNECTOR_PROVIDERS = {
    "youtube": "youtube",
    "dropbox": "dropbox",
    "slack": "slack",
    "telegram": "telegram",
    "zoom": "zoom",
}


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
    |"workflow"|"hook"}``. Provider-level trigger references (a trigger
    whose connector is its own provider but which binds no explicit
    connection_id) sit under ``"provider:<provider>"`` keys for attach()
    to place on that provider's single connection. Loading degrades to an
    empty map on a storage hiccup — usage labels must never break the list
    that carries them.
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
        # Provider-level trigger usage: an explicit connection_id is the
        # precise reference (recorded above); a trigger whose connector is
        # its own provider still depends on that provider's connection and
        # lands under "provider:<provider>" for attach() to place — only on
        # the row when it is that provider's single connection.
        trigger = workflow.get("trigger") or {}
        provider = CONNECTOR_PROVIDERS.get(str(trigger.get("connector") or ""))
        if provider and not found_connection_id(workflow.get("trigger")):
            record(f"provider:{provider}", {
                "ref": str(workflow["id"]),
                "kind": "workflow",
                "enabled": bool(workflow.get("enabled", True)),
                "where": "trigger",
            })

    if hooks is None:
        try:
            hooks = hook_triggers.load_items() if os.environ.get(
                hook_triggers.TABLE_ENV) else []
        except Exception:  # noqa: BLE001 — usage is best-effort metadata
            hooks = []
    for item in hooks:
        found = []
        _collect_ids(item, "hook", found)
        for connection_id, where in found:
            record(connection_id, {
                "ref": str(item.get("hook_id") or item.get("name") or "hook"),
                "kind": "hook",
                "enabled": bool(item.get("enabled", True)),
                "where": "hook",
            })
    return usage


def found_connection_id(value):
    found = []
    _collect_ids(value, "workflow", found)
    return bool(found)


def fold_refs(usage, connections):
    """Fold usage keyed by human references (``drive alexey@…``, which flows
    may write instead of an internal connection_id) onto the ids those
    references resolve to among ``connections``. Unresolvable keys stay as
    they are; ``provider:`` keys are untouched. Returns a new map."""
    ids = {str(row.get("connection_id")) for row in connections}
    if all(key in ids or key.startswith("provider:") for key in usage):
        return usage
    from ..connections import refs
    folded = {key: list(entries) for key, entries in usage.items()}
    for key, entries in usage.items():
        if key in ids or key.startswith("provider:"):
            continue
        try:
            target = refs.resolve(connections, key)["connection_id"]
        except refs.RefError:
            continue
        bucket = folded.setdefault(target, [])
        for entry in entries:
            if entry not in bucket:
                bucket.append(entry)
    return folded


def attach(rows, usage=None):
    """Stamp connection rows with ``used_in`` in place and return them.

    ``usage=None`` (the normal call) loads the map here; pass an explicit
    map (or ``[]`` to force every row empty) when the caller already has
    one. Rows always come back carrying the key, so the console never
    special-cases absence. A row nothing references directly inherits the
    provider-level trigger entries — but only when it is that provider's
    single connection; with several accounts the match would be ambiguous
    and the row stays honestly empty.
    """
    if usage is None:
        usage = collect()
    usage = fold_refs(usage, rows)
    provider_counts = {}
    for row in rows:
        provider_counts[row.get("provider")] = provider_counts.get(row.get("provider"), 0) + 1
    for row in rows:
        connection_id = row.get("connection_id")
        refs = usage.get(connection_id, [])
        if not refs:
            provider = row.get("provider")
            if provider_counts.get(provider) == 1:
                refs = list(usage.get(f"provider:{provider}", []))
        row["used_in"] = refs
    return rows
