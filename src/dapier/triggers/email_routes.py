"""Email inventory and ownership, projected from workflow definitions.

Addresses are entry points into workflows, never another action store. Legacy
records are read only during the migration; publishing their exact generated
workflow id takes precedence at runtime before the old record is removed.
"""
import copy
import os

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


def legacy_items():
    return email_triggers.load_items() if os.environ.get(email_triggers.TABLE_ENV) else []


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
    sources = [(i["workflow"], i, True, False) for i in live.values() if isinstance(i.get("workflow"), dict)]
    sources += [(i["workflow"], i, False, False) for name, i in drafts.items()
                if name not in live and isinstance(i.get("workflow"), dict)]
    for item in legacy_items():
        workflow = email_triggers.workflow_for(item)
        if workflow and workflow["id"] not in live:
            workflow = {**workflow, "enabled": item.get("enabled", True),
                        "description": item.get("description", "")}
            sources.append((workflow, item, True, True))
    addresses, subscriptions, watchers = {}, [], []
    for workflow, item, published, legacy in sources:
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
                       "has_draft": workflow["id"] in drafts, "legacy": legacy}
            if legacy:
                handler["legacy_name"] = item["name"]
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
    others += [w for item in legacy_items()
               if (w := email_triggers.workflow_for(item)) and w["id"] not in excluded]
    conflicts = sorted({w["id"] for w in others
                        if any(_overlaps(a, b) for a in specs for b in email_specs(w))})
    if conflicts:
        raise RouteConflict("Email routes overlap with " + ", ".join(conflicts)
                            + ". Choose another address, or explicitly set allow_email_overlap: true for fan-out.")


def migrate(name, operator):
    """Explicitly publish an unchanged legacy definition, preserving its run identity."""
    from ..api import designer_store
    name = email_triggers.validate_name(name)
    table = email_triggers.get_table()
    stored_item = table.get_item(Key={"name": name}, ConsistentRead=True).get("Item")
    if not stored_item:
        return 404, {"error": f"no legacy email trigger named '{name}'"}
    item = email_triggers._decode_numbers(stored_item)
    workflow = email_triggers.workflow_for(item)
    if workflow is None:
        return 409, {"error": "the legacy trigger has an unresolved flow"}
    workflow = copy.deepcopy(workflow)
    workflow["enabled"] = bool(item.get("enabled", True))
    workflow["description"] = item.get("description") or ""
    for index, action in enumerate(workflow["actions"]):
        action.setdefault("id", str(index))
    previous = published_workflows.get_item(workflow["id"])
    if previous:
        if previous.get("workflow") != workflow:
            return 409, {"error": "a different workflow already uses this legacy trigger's id"}
    else:
        if published_workflows.get_draft(workflow["id"]):
            return 409, {"error": "this workflow has a draft; publish or discard it before migration"}
        # Validate through the same workflow API. Existing behaviour is published
        # explicitly by this migration verb; ordinary saves still only draft.
        status, payload = designer_store.api_save(
            {"yaml": designer_store.workflow_yaml_text(workflow)},
            operator=operator, cause="email-migration", live=True, only_if_absent=True)
        if status != 200:
            return status, payload
    # Writes to the legacy API are retired before migration. Recheck before
    # removing the source in case an older deployment edited it concurrently.
    current = table.get_item(Key={"name": name}, ConsistentRead=True).get("Item")
    if email_triggers._decode_numbers(current) != item:
        return 409, {"error": "legacy trigger changed during migration; source retained"}
    keys = [key for key in item if key != "name"]
    table.delete_item(
        Key={"name": name},
        ConditionExpression=" AND ".join(f"#f{i} = :v{i}" for i in range(len(keys))),
        ExpressionAttributeNames={f"#f{i}": key for i, key in enumerate(keys)},
        ExpressionAttributeValues={f":v{i}": stored_item[key] for i, key in enumerate(keys)})
    return 200, {"migrated": True, "workflow": workflow["id"], "published": True,
                 "address": item.get("address", ""), "name": name}
