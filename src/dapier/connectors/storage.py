"""Storage connector: per-workflow key-value state that survives runs.

The catalog entries for the engine-side runners (see
``engine/actions/storage.py``) — a DynamoDB-backed table scoped to the
workflow, so a chain can remember things between events: dedupe ids,
counters, last-seen cursors. Values are template-rendered from the event
and earlier steps at run time.
"""
from ..engine.actions.storage import (
    run_storage_delete,
    run_storage_find,
    run_storage_get,
    run_storage_set,
)
from .registry import Action, register

register(Action(
    type="storage_get",
    label="Storage (get value)",
    icon="database",
    description="Read this workflow's stored value for a key. Output: "
                "{key, value, found}; a missing key is "
                "{found: false, value: \"\"}, not an error. Values are "
                "scoped to this workflow and survive runs.",
    run=lambda action, event, workflow_id, steps=None: run_storage_get(action, event, workflow_id, steps=steps),
    required=frozenset({"key"}),
    optional=frozenset(),
    fields=(
        {"key": "key", "label": "Key", "type": "text", "required": True,
         "placeholder": "last-seen-id",
         "help": "Reads this workflow's own key-value store — other workflows "
                 "cannot see its values"},
    ),
))

register(Action(
    type="storage_set",
    label="Storage (set value)",
    icon="database",
    description="Store a value under a key for this workflow (overwrites "
                "the previous one). Values are template-rendered from the "
                "event and earlier steps, e.g. {text} or "
                "{steps.lookup.output.row}. Output: {key, stored: true}, "
                "plus expires when ttl_seconds is set.",
    run=lambda action, event, workflow_id, steps=None: run_storage_set(action, event, workflow_id, steps=steps),
    required=frozenset({"key", "value"}),
    optional=frozenset({"ttl_seconds"}),
    fields=(
        {"key": "key", "label": "Key", "type": "text", "required": True,
         "placeholder": "last-seen-id",
         "help": "Writes this workflow's own key-value store — other workflows "
                 "cannot see its values"},
        {"key": "value", "label": "Value", "type": "textarea", "required": True,
         "placeholder": "{text}",
         "help": "Template-rendered at run time, e.g. {trigger.occurred_at} "
                 "or {steps.find.output.row}"},
        {"key": "ttl_seconds", "label": "Expire after (seconds)", "type": "number",
         "help": "Delete the value again after this many seconds (omit to "
                 "keep it until it is overwritten or deleted)"},
    ),
))

register(Action(
    type="storage_delete",
    label="Storage (delete value)",
    icon="database",
    description="Remove this workflow's stored value for a key. Output: "
                "{key, deleted}; deleting a missing key is fine "
                "(deleted: false).",
    run=lambda action, event, workflow_id, steps=None: run_storage_delete(action, event, workflow_id, steps=steps),
    required=frozenset({"key"}),
    optional=frozenset(),
    fields=(
        {"key": "key", "label": "Key", "type": "text", "required": True,
         "placeholder": "last-seen-id",
         "help": "Deletes from this workflow's own key-value store"},
    ),
))

register(Action(
    type="storage_find",
    label="Storage (find keys)",
    icon="database",
    description="List this workflow's stored keys under a prefix, ascending "
                "by key. Output: {items: [{key, value}...], count}, capped "
                "by limit (default 20, at most 50).",
    run=lambda action, event, workflow_id, steps=None: run_storage_find(action, event, workflow_id, steps=steps),
    required=frozenset({"prefix"}),
    optional=frozenset({"limit"}),
    fields=(
        {"key": "prefix", "label": "Key prefix", "type": "text", "required": True,
         "placeholder": "counter:",
         "help": "Lists the keys of this workflow's own store that start "
                 "with it — e.g. \"counter:\" finds counter:email, counter:sms"},
        {"key": "limit", "label": "Max keys", "type": "number", "default": "20",
         "help": "How many items the listing may carry (default 20, at most 50)"},
    ),
))
