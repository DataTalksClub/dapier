"""Shared vocabulary for email triggers: names, addresses, and routes.

Email flows are authored only as managed workflows (the designer store);
there is no separate email-trigger store. This module keeps the pieces every
email surface shares — address naming and the reserved-route list,
action-chain validation in the TriggerError dialect (used by the other
trigger adapters too), and the route-claim registry that publish paths check
against shadowing. The receiving domain is accepted by upstream
Datamailer/SES.
"""

import os
import re

DOMAIN_ENV = "TRIGGER_EMAIL_DOMAIN"
DEFAULT_DOMAIN = "dtcdev.click"

NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}[a-z0-9]$")
# Only infrastructure addresses are reserved here. Routes claimed by managed
# workflows are guarded separately: the publish paths refuse to shadow them
# (yaml_email_routes), so a route becomes reusable as soon as the workflow
# stops claiming it.
RESERVED_NAMES = {
    "abuse", "admin", "api", "auth", "billing", "dmarc", "datamailer", "e2e",
    "hostmaster", "imap", "mail", "no-reply", "noreply", "pop", "postmaster",
    "relay", "root", "security", "smtp", "support", "webmaster", "webmail",
    "www",
}


class TriggerError(ValueError):
    """Invalid or conflicting trigger definition."""


def trigger_domain():
    return os.environ.get(DOMAIN_ENV, DEFAULT_DOMAIN).strip().lstrip("@").lower()


def address_for(name):
    return f"{name}@{trigger_domain()}"


def validate_name(name):
    name = str(name or "").strip().lower()
    if not NAME_PATTERN.fullmatch(name):
        raise TriggerError("trigger name must be 2-32 chars: lowercase letters, digits, hyphens")
    if name in RESERVED_NAMES:
        raise TriggerError(f"the address {address_for(name)} is reserved")
    return name


def validate_actions(actions):
    """Validation lives in the connector registry (one spec set for the
    engine, the API and the designer); TriggerError is this module's dialect."""
    from ..connectors import registry

    try:
        return registry.validate_action_chain(actions)
    except registry.ActionError as exc:
        raise TriggerError(str(exc)) from None


def resolve_actions(body):
    """The trigger's inline action chain, validated against the registry.

    Shared flow references are retired: a ``flow`` key fails the save with
    the retirement error instead of resolving to anything.
    """
    if body.get("flow"):
        raise TriggerError("shared flows are retired; give the trigger inline actions")
    return validate_actions(body.get("actions"))


def managed_routes():
    """Address routes claimed by published workflows, each with its owner.

    One entry per (route, workflow) pair: a workflow may claim several
    routes, and a route claimed by two workflows lists both owners. These
    are the rows of the console and CLI email list — every address the
    domain answers is one entry of the same list, never a footnote.
    ``status`` mirrors the owning workflow's run state.
    """
    from .. import engine

    routes = {}
    for workflow in engine.workflows():
        for trigger in engine.workflow_triggers(workflow):
            if trigger.get("connector") != "email":
                continue
            rule = (trigger.get("filters") or {}).get("route") or {}
            if not (isinstance(rule, dict) and rule.get("equals")):
                continue
            key = (str(rule["equals"]).lower(), str(workflow.get("id") or ""))
            if workflow.get("auto_paused") is True:
                status = "auto-paused"
            elif workflow.get("enabled", True):
                status = "enabled"
            else:
                status = "disabled"
            routes.setdefault(key, {"name": key[0], "workflow": key[1], "status": status})
    return [routes[key] for key in sorted(routes)]


def yaml_email_routes():
    """Routes already claimed by published workflows, so nothing else can shadow them."""
    return {route["name"] for route in managed_routes()}
