"""Digest connector: collect events across runs, release them as one list.

The catalog entries for the engine-side runners (see
``engine/actions/digest.py``) — Zapier's Digest, built on the same
per-workflow table the storage actions use: a scheduled workflow adds items
as events arrive and a later run flushes them together.
"""
from ..engine.actions.digest import run_digest_add, run_digest_flush
from .registry import Action, register

register(Action(
    type="digest_add",
    label="Digest (add item)",
    icon="layers",
    description="Add an item to this workflow's digest so later runs can "
                "release the collected list together (Zapier's Digest). "
                "Output: {key, added, count}; dedupe: true turns a repeat "
                "item into added: false. Holds up to max_items pending "
                "(default 500) — a further add drops the oldest item.",
    run=lambda action, event, workflow_id, steps=None: run_digest_add(action, event, workflow_id, steps=steps),
    required=frozenset({"key"}),
    optional=frozenset({"item", "dedupe", "max_items", "ttl_seconds"}),
    fields=(
        {"key": "key", "label": "Digest key", "type": "text", "required": True,
         "placeholder": "todo-items",
         "help": "Named bucket this workflow collects into — add and flush "
                 "steps pair up through it"},
        {"key": "item", "label": "Item", "type": "textarea",
         "placeholder": "{text}",
         "help": "Template-rendered at run time, e.g. {text} or "
                 "{steps.lookup.output.row}; JSON objects keep their shape "
                 "for {item.field} access after the flush. Omit to collect "
                 "the whole event data as JSON"},
        {"key": "dedupe", "label": "Skip if already pending", "type": "boolean",
         "help": "An item identical to one already waiting is not added "
                 "again (added: false)"},
        {"key": "max_items", "label": "Hold at most", "type": "number",
         "default": "500",
         "help": "Pending items the digest may hold (default 500); a "
                 "further add drops the oldest item, so an unflushed "
                 "digest never blocks a run"},
        {"key": "ttl_seconds", "label": "Expire after (seconds)", "type": "number",
         "help": "Drop the item again after this many seconds even if no "
                 "flush happened"},
    ),
))

register(Action(
    type="digest_flush",
    label="Digest (flush)",
    icon="layers",
    description="Release this workflow's collected digest items as one "
                "list and empty it. Output: {items, count, empty} in "
                "arrival order — branch on empty (a filter step on "
                "{steps.<id>.output.count} > 0) when a scheduled run may "
                "have nothing to release. reset: false peeks without "
                "clearing.",
    run=lambda action, event, workflow_id, steps=None: run_digest_flush(action, event, workflow_id, steps=steps),
    required=frozenset({"key"}),
    optional=frozenset({"reset"}),
    fields=(
        {"key": "key", "label": "Digest key", "type": "text", "required": True,
         "placeholder": "todo-items",
         "help": "Releases and clears what digest_add collected under this "
                 "key"},
        {"key": "reset", "label": "Clear after reading", "type": "boolean",
         "default": "true",
         "help": "On (the default) the digest is emptied — the flush; off, "
                 "the items are only peeked at and stay pending"},
    ),
))
