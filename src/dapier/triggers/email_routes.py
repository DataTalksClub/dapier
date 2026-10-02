"""Email inventory and ownership, projected from workflow definitions.

Addresses are entry points into workflows, never another action store: the
inventory is exactly the email triggers claimed by published (and draft)
workflows, and every publish path checks address ownership.
"""
import copy

from . import email_triggers, published_workflows


class RouteConflict(ValueError):
    pass


def email_specs(workflow, event="message.received"):
    from ..engine.matching import workflow_triggers
    return [t for t in workflow_triggers(workflow)
            if t.get("connector") == "email" and t.get("event") == event]


def finite_routes(trigger):
    """Finite address claims, or None for a broad subscription."""
    rule = (trigger.get("filters") or {}).get("route")
    if isinstance(rule, str):
        return [rule]
    if isinstance(rule, dict):
        if isinstance(rule.get("equals"), str):
            return [rule["equals"]]
        if isinstance(rule.get("in"), list) and all(isinstance(n, str) for n in rule["in"]):
            return rule["in"]
    return None


def status_of(workflow, published=True):
    if not published:
        return "draft"
    if workflow.get("auto_paused"):
        return "auto-paused"
    return "enabled" if workflow.get("enabled", True) else "disabled"


def inventory():
    """One row per address, all handlers visible; broad rules and feedback apart."""
    items = published_workflows.load_items(include_drafts=True) if published_workflows.configured() else []
    live = {i["workflow_id"]: i for i in items if not i.get("draft_of")}
    drafts = {i["draft_of"]: i for i in items if i.get("draft_of")}
    sources = [(i["workflow"], i, True) for i in live.values() if isinstance(i.get("workflow"), dict)]
    sources += [(i["workflow"], i, False) for name, i in drafts.items()
                if name not in live and isinstance(i.get("workflow"), dict)]
    addresses, subscriptions, watchers = {}, [], []
    for workflow, item, published in sources:
        from ..engine.matching import workflow_triggers
        for trigger in workflow_triggers(workflow):
            if trigger.get("connector") != "email":
                continue
            handler = {"workflow": workflow["id"], "event": trigger.get("event"),
                       "status": status_of(workflow, published),
                       "description": workflow.get("description", ""),
                       "action_types": [a.get("type", "unknown") for a in workflow.get("actions", [])],
                       "filters": trigger.get("filters") or {},
                       "updated_at": item.get("updated_at", ""),
                       "has_draft": workflow["id"] in drafts}
            if trigger.get("event") != "message.received":
                watchers.append(handler)
                continue
            routes = finite_routes(trigger)
            if routes is None:
                subscriptions.append(handler)
                continue
            for name in routes:
                # A multi-operator rule may exclude some of its finite candidates.
                if not _route_matches(trigger, name):
                    continue
                row = addresses.setdefault(name, {"name": name,
                    "address": email_triggers.address_for(name), "handlers": []})
                existing = next((h for h in row["handlers"] if h["workflow"] == handler["workflow"]), None)
                if existing:
                    if handler["filters"] not in existing["matching_filters"]:
                        existing["matching_filters"].append(copy.deepcopy(handler["filters"]))
                else:
                    entry = copy.deepcopy(handler)
                    entry["matching_filters"] = [copy.deepcopy(handler["filters"])]
                    row["handlers"].append(entry)
    return {"domain": email_triggers.trigger_domain(),
            "addresses": [addresses[k] for k in sorted(addresses)],
            "subscriptions": subscriptions, "watchers": watchers}


def _route_matches(trigger, name):
    from ..engine.matching import _matches_filter
    rule = (trigger.get("filters") or {}).get("route")
    return rule is None or _matches_filter(name, rule)


def _overlaps(left, right):
    a, b = finite_routes(left), finite_routes(right)
    if a is not None:
        return any(_route_matches(left, n) and _route_matches(right, n) for n in a)
    if b is not None:
        return any(_route_matches(left, n) and _route_matches(right, n) for n in b)
    # Broad subscriptions cannot generally be proved disjoint. They must opt in.
    return True


def validate_ownership(workflow, *, rename_from=None):
    """Every publish path checks address ownership; deliberate fan-out is explicit."""
    specs = email_specs(workflow)
    if not specs or workflow.get("allow_email_overlap") is True:
        return
    from ..engine.matching import workflows
    excluded = {workflow["id"], str(rename_from or "").removesuffix(".yaml")}
    others = [w for w in workflows() if w.get("id") not in excluded]
    conflicts = sorted({w["id"] for w in others
                        if any(_overlaps(a, b) for a in specs for b in email_specs(w))})
    if conflicts:
        raise RouteConflict("Email routes overlap with " + ", ".join(conflicts)
                            + ". Choose another address, or explicitly set allow_email_overlap: true for fan-out.")
