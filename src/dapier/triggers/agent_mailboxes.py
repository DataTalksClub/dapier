"""The agent mailbox and the one shared sender list.

Mail to ``agent@`` the trigger domain is not a workflow. One mailbox row
holds the engine, workspace, instructions, and extra AND rules. The sender
allow-list is a single item of its own: every mailbox consults it, and an
empty list accepts nobody.
"""

import os
import re
import uuid
from datetime import datetime, timezone
from email.utils import parseaddr

from .email_triggers import address_for, trigger_domain, validate_name


MAILBOX_NAME = "agent"
TABLE_ENV = "AGENT_MAILBOXES_TABLE"
FROM_TABLE_ENV = "AGENT_FROM_TABLE"
TASKS_TABLE_ENV = "AGENT_TASKS_TABLE"
FROM_ID = "from-allow"
SEED_SENDERS = (
    "alexey.s.grigoriev@gmail.com",
    "alexey@datatalks.club",
)
RULE_FIELDS = frozenset({"subject", "body"})
INSTRUCTIONS_LIMIT = 2_000
DEFAULT_WORKSPACE = "/home/alexey/git/dapier"
DEFAULT_ENGINE = "claude"
DEFAULT_TAG_PREFIX = "mail"


class MailboxError(ValueError):
    """Invalid mailbox, sender, or rule."""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _table(env, table_ref, label):
    if table_ref is not None:
        return table_ref
    name = os.environ.get(env)
    if not name:
        raise MailboxError(f"{label} is not configured")
    import boto3

    return boto3.resource("dynamodb").Table(name)


def mailbox_table(table_ref=None):
    return _table(TABLE_ENV, table_ref, "agent mailboxes")


def from_table(table_ref=None):
    return _table(FROM_TABLE_ENV, table_ref, "the sender list")


def tasks_table(table_ref=None):
    return _table(TASKS_TABLE_ENV, table_ref, "agent tasks")


def peek_mailbox(name, table_ref=None):
    """The stored row, or None. Does not create the default mailbox."""
    try:
        table = mailbox_table(table_ref)
    except MailboxError:
        return None
    return table.get_item(Key={"name": str(name or "").strip().lower()}).get("Item")


def bare_address(value):
    """The mailbox of a From header or a typed address, lowercased.

    A display name is ignored. ``*`` is never an address: wildcards are not
    a sender rule. Unparseable text yields ``""``.
    """
    _, parsed = parseaddr(str(value or ""))
    address = parsed.strip().lower()
    if not address or "*" in address or address.count("@") != 1:
        return ""
    local, domain = address.split("@", 1)
    if not local or not domain or "." not in domain:
        return ""
    if re.search(r"\s", address):
        return ""
    return address


def _default_mailbox(operator):
    now = _now()
    return {
        "name": MAILBOX_NAME,
        "address": address_for(MAILBOX_NAME),
        "engine": DEFAULT_ENGINE,
        "profile": None,
        "workspace": DEFAULT_WORKSPACE,
        "tag_prefix": DEFAULT_TAG_PREFIX,
        "instructions": "",
        "rules": [],
        "enabled": True,
        "created_by": str(operator or ""),
        "created_at": now,
        "updated_at": now,
    }


def public_mailbox(item):
    return {key: item.get(key) for key in (
        "name", "address", "engine", "profile", "workspace", "tag_prefix",
        "instructions", "rules", "enabled", "created_by", "created_at", "updated_at",
    )}


def _put_mailbox(table, item):
    stored = dict(item)
    if stored.get("profile") is None:
        stored["profile"] = ""
    table.put_item(Item=stored)
    return item


def api_get_mailbox(table_ref=None, operator=""):
    table = mailbox_table(table_ref)
    item = table.get_item(Key={"name": MAILBOX_NAME}).get("Item")
    if item is None:
        from .addresses import claim_error

        error = claim_error(MAILBOX_NAME, "agent-mailbox")
        if error:
            raise MailboxError(error)
        item = _default_mailbox(operator)
        _put_mailbox(table, item)
    return 200, {"domain": trigger_domain(), "mailbox": public_mailbox(item)}


def _tag_prefix(value, previous):
    prefix = str(value if value is not None else (previous or DEFAULT_TAG_PREFIX)).strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,18}[a-z0-9]", prefix):
        raise MailboxError("tag_prefix must be 2-20 chars: lowercase letters, digits, hyphens")
    return prefix


def api_save_mailbox(body, operator, table_ref=None):
    """Update engine, workspace, instructions, and enabled.

    Rules and the sender list are not on this body. Keys for them are ignored
    so a settings save cannot wipe either.
    """
    if not isinstance(body, dict):
        raise MailboxError("request body must be an object")
    name = str(body.get("name") or MAILBOX_NAME).strip().lower()
    if name != MAILBOX_NAME:
        raise MailboxError(f"the only agent mailbox is '{MAILBOX_NAME}'")
    # validate_name rejects reserved infrastructure names; agent is not one.
    validate_name(name)
    table = mailbox_table(table_ref)
    previous = table.get_item(Key={"name": MAILBOX_NAME}).get("Item")
    if previous is None:
        from .addresses import claim_error

        error = claim_error(MAILBOX_NAME, "agent-mailbox")
        if error:
            raise MailboxError(error)
        previous = _default_mailbox(operator)
        created = True
    else:
        created = False
    instructions = body["instructions"] if "instructions" in body else previous.get("instructions") or ""
    instructions = str(instructions or "")
    if len(instructions) > INSTRUCTIONS_LIMIT:
        raise MailboxError(f"instructions are limited to {INSTRUCTIONS_LIMIT} characters")
    engine = str(body["engine"] if "engine" in body else previous.get("engine") or "").strip()
    workspace = str(body["workspace"] if "workspace" in body else previous.get("workspace") or "").strip()
    if not workspace:
        raise MailboxError("workspace is required")
    profile = body["profile"] if "profile" in body else previous.get("profile")
    profile = str(profile).strip() if profile else ""
    enabled = previous.get("enabled", True) if "enabled" not in body else bool(body.get("enabled"))
    item = {
        "name": MAILBOX_NAME,
        "address": address_for(MAILBOX_NAME),
        "engine": engine,
        "profile": profile or None,
        "workspace": workspace,
        "tag_prefix": _tag_prefix(body.get("tag_prefix"), previous.get("tag_prefix")),
        "instructions": instructions,
        "rules": list(previous.get("rules") or []),
        "enabled": enabled,
        "created_by": previous.get("created_by") or str(operator or ""),
        "created_at": previous.get("created_at") or _now(),
        "updated_at": _now(),
    }
    _put_mailbox(table, item)
    return 200, {"created": created, "mailbox": public_mailbox(item)}


def _read_senders(table):
    item = table.get_item(Key={"id": FROM_ID}).get("Item")
    if item is None:
        item = {"id": FROM_ID, "addresses": list(SEED_SENDERS)}
        table.put_item(Item=item)
    return [str(address).strip().lower() for address in item.get("addresses") or []]


def _write_senders(table, addresses):
    table.put_item(Item={"id": FROM_ID, "addresses": list(addresses)})


def api_from_list(table_ref=None):
    addresses = _read_senders(from_table(table_ref))
    return 200, {"addresses": addresses}


def api_from_add(address, table_ref=None):
    bare = bare_address(address)
    if not bare:
        raise MailboxError("a sender must be one email address, without a wildcard")
    table = from_table(table_ref)
    addresses = _read_senders(table)
    added = bare not in addresses
    if added:
        addresses.append(bare)
        _write_senders(table, addresses)
    return 200, {"addresses": addresses, "added": added}


def api_from_remove(address, table_ref=None):
    bare = bare_address(address)
    if not bare:
        raise MailboxError("a sender must be one email address, without a wildcard")
    table = from_table(table_ref)
    addresses = _read_senders(table)
    if bare in addresses:
        addresses = [item for item in addresses if item != bare]
        _write_senders(table, addresses)
        removed = True
    else:
        removed = False
    return 200, {"addresses": addresses, "removed": removed}


def _rule_operators():
    from ..engine.matching import _matches_filter

    return _matches_filter


def validate_rule(body):
    if not isinstance(body, dict):
        raise MailboxError("a rule must be an object")
    field = str(body.get("field") or "").strip()
    operator = str(body.get("operator") or "").strip()
    if field not in RULE_FIELDS:
        raise MailboxError("a rule field must be subject or body")
    if not operator or operator == "from":
        raise MailboxError(f"unknown operator '{operator}'")
    value = body.get("value")
    if value is None:
        value = ""
    try:
        _rule_operators()("", {operator: value})
    except KeyError:
        raise MailboxError(f"unknown operator '{operator}'") from None
    except (TypeError, ValueError) as exc:
        raise MailboxError(f"invalid value for operator '{operator}'") from exc
    return {
        "id": uuid.uuid4().hex[:12],
        "field": field,
        "operator": operator,
        "value": value,
    }


def api_rule_add(body, table_ref=None):
    rule = validate_rule(body)
    table = mailbox_table(table_ref)
    item = table.get_item(Key={"name": MAILBOX_NAME}).get("Item")
    if item is None:
        raise MailboxError("the agent mailbox does not exist yet")
    rules = list(item.get("rules") or [])
    rules.append(rule)
    item = dict(item)
    item["rules"] = rules
    item["updated_at"] = _now()
    _put_mailbox(table, item)
    return 200, {"rule": rule, "rules": rules}


def api_rule_delete(rule_id, table_ref=None):
    rule_id = str(rule_id or "").strip()
    if not rule_id:
        raise MailboxError("a rule id is required")
    table = mailbox_table(table_ref)
    item = table.get_item(Key={"name": MAILBOX_NAME}).get("Item")
    if item is None:
        raise MailboxError("the agent mailbox does not exist yet")
    rules = [rule for rule in item.get("rules") or [] if rule.get("id") != rule_id]
    if len(rules) == len(item.get("rules") or []):
        raise MailboxError(f"no rule '{rule_id}'")
    item = dict(item)
    item["rules"] = rules
    item["updated_at"] = _now()
    _put_mailbox(table, item)
    return 200, {"rules": rules, "deleted": rule_id}


def api_tasks(table_ref=None, limit=50):
    table = tasks_table(table_ref)
    items = table.scan(Limit=200).get("Items", [])
    items.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    public = []
    for item in items[:limit]:
        public.append({key: item.get(key) for key in (
            "task_id", "mailbox", "status", "from", "subject", "s3",
            "session_id", "tag", "workspace", "error", "created_at",
            "started_at", "sent_at",
        )})
    return 200, {"tasks": public}
