"""The iterating steps: for_each loops over a list
from the event data (or a digest flush), and digest accumulates
items across runs to flush in a later one."""
from .controls import _predicate_scope
from .core import (DIGEST_MODES, MAX_LOOP_ITERATIONS, RunSuspended, _lookup,
                   render_value)


def _run_for_each(workflow_id, action_id, step, event, run_action, *,
                  before_action, after_action, on_action_error, scope=None,
                  step_outputs=None, start=0):
    label = step.get("id", "")
    body = step.get("actions")
    if not isinstance(body, list) or not body:
        raise ValueError(f"for_each '{label}' needs at least one step in actions")
    path = str(step.get("list") or "").strip().strip("{}").strip()
    if not path:
        raise ValueError(f"for_each '{label}': needs a list field")
    base = {**_predicate_scope(event, scope), "steps": step_outputs or {}}
    items, found = _lookup(base, path)
    if not found:
        raise ValueError(f"for_each '{label}': list field '{path}' not found in the event data")
    if not isinstance(items, list):
        raise ValueError(f"for_each '{label}': field '{path}' is not a list")
    item_var = str(step.get("item") or "item").strip() or "item"
    cap = max(0, min(int(step.get("max_iterations") or MAX_LOOP_ITERATIONS),
                     MAX_LOOP_ITERATIONS))

    from .chains import run_chain  # lazy: chains imports this module at its top

    iterated = skipped = 0
    for position, item in enumerate(items[:cap]):
        if position < start:
            # A resumed run continues the loop where the suspension paused it.
            continue
        child_scope = {**base, item_var: item, f"{item_var}_index": position}
        try:
            stop = run_chain(
                workflow_id, render_value(body, child_scope), event, run_action,
                before_action=before_action, after_action=after_action,
                on_action_error=on_action_error, prefix=f"{action_id}[{position}]",
                scope=child_scope, step_outputs=step_outputs,
            )
        except RunSuspended as susp:
            # The loop's remaining iterations resume after the paused
            # iteration's own remainder (already appended by the body's
            # run_chain frame).
            susp.segments.append({"loop": {
                "action_id": action_id, "step": step, "from": position + 1,
                "scope": scope, "item_var": item_var, "cap": cap,
            }})
            raise
        iterated += 1
        # A filter inside the body skips just this iteration; the loop goes on.
        if stop == "filtered":
            skipped += 1
    output = {"iterated": iterated, "items": len(items), "skipped": skipped,
              "truncated": len(items) > cap}
    if start:
        output["resumed_from"] = start
    return None, output, "completed"


def _run_digest(workflow_id, step, event, context):
    """Zapier-Digest batching: accumulate items across runs, flush them later.

    ``accumulate`` appends the step's rendered item(s) — ``item`` (one
    template) and/or ``items`` (a list of templates, appended in order,
    ``item`` first) — to the digest under the required ``key``
    (a literal name: the accumulate and flush runs are different events, so
    the key must not depend on either). Output: ``{"key", "digested"}``
    with the new total count.

    ``shared: true`` moves the digest to a partition every workflow can
    reach — the canonical pattern accumulates in the event's workflow and
    flushes from a schedule-triggered one, which are two workflow ids
    sharing one key. The default scopes the digest to this workflow alone.

    ``flush`` claims and clears the batch in one atomic operation
    (``digests.digests_claim``), so concurrent flushes never double-send,
    and exposes the claimed items to the rest of the run: they ride the
    event data as ``digest`` (``{digest.items}``, ``{digest.count}`` in any
    template — and ``list: digest.items`` for a following for_each). An
    empty digest records ``skipped`` (nothing to send; output
    ``{empty: true, count: 0, items: []}``) and the chain runs on. Either
    way ``digest`` lands on the event with the step's result, so a
    following step can gate on ``{digest.count}``.
    """
    from ..actions import digests

    label = step.get("id", "")
    mode = str(step.get("mode") or "accumulate").strip().lower() or "accumulate"
    if mode not in DIGEST_MODES:
        raise ValueError(f"digest '{label}': mode must be one of {', '.join(DIGEST_MODES)}")
    key = str(step.get("key") or "").strip()
    if not key:
        raise ValueError(f"digest '{label}' requires a key")
    scope = digests.SHARED_SCOPE if step.get("shared") else workflow_id

    if mode == "accumulate":
        batch = _digest_batch(step, context, label)
        count = digests.digests_append(scope, key, batch)
        return None, {"key": key, "digested": count}, "completed"

    items = digests.digests_claim(scope, key)
    data = event.get("data")
    if not isinstance(data, dict):
        data = {}
        event["data"] = data
    data["digest"] = {"items": items, "count": len(items)}
    output = {"key": key, "items": items, "count": len(items)}
    if not items:
        output["empty"] = True
        return None, output, "skipped"
    return None, output, "completed"


def _digest_batch(step, context, label):
    """The items one accumulate run appends: ``item`` (one rendered string)
    and/or ``items`` (each template rendered), in that order. At least one
    is required — an accumulate run with nothing to append is a config
    error, not a silent no-op."""
    batch = []
    item = step.get("item")
    if item is not None:
        batch.append(render_value(item, context))
    items = step.get("items")
    if items is not None:
        if not isinstance(items, list) or not items:
            raise ValueError(f"digest '{label}': items must be a non-empty list of templates")
        batch.extend(render_value(entry, context) for entry in items)
    if not batch:
        raise ValueError(
            f"digest '{label}': accumulate needs an item or a non-empty items list")
    return batch
