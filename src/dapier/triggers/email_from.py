"""Per-address sender policy for inbound email.

Each receiving address (the route / local-part) can allow a listed set of
senders or accept anyone. Addresses with no row of their own use the default
``from-allow`` policy. A missing default row is the seed. An empty allow-list
accepts nobody. ``mode=anyone`` still refuses a message with no recognizable
From — that is not a sender, it is a malformed event.

The workflow worker calls :func:`rejected` before any email flow matches.
"""

import os
import re
from email.utils import parseaddr

from .email_triggers import NAME_PATTERN


TABLE_ENV = "EMAIL_FROM_TABLE"
ITEM_ID = "from-allow"
ROUTE_PREFIX = "route:"
MODE_ALLOWLIST = "allowlist"
MODE_ANYONE = "anyone"
MODES = (MODE_ALLOWLIST, MODE_ANYONE)
SEED = (
    "alexey.s.grigoriev@gmail.com",
    "alexey@datatalks.club",
)


class FromError(ValueError):
    """A sender address or route policy cannot be stored."""


def bare_address(value):
    """Mailbox only, lowercased. A display name is ignored. ``*`` is never an address."""
    _, parsed = parseaddr(str(value or ""))
    address = parsed.strip().lower()
    if not address or "*" in address or address.count("@") != 1:
        return ""
    local, domain = address.split("@", 1)
    if not local or "." not in domain or re.search(r"\s", address):
        return ""
    return address


def _table(table_ref):
    if table_ref is not None:
        return table_ref
    name = os.environ.get(TABLE_ENV)
    if not name:
        raise FromError("the sender list is not configured")
    import boto3

    return boto3.resource("dynamodb").Table(name)


def _addresses(item):
    return [str(address).strip().lower() for address in (item or {}).get("addresses") or []]


def _mode(item):
    mode = str((item or {}).get("mode") or MODE_ALLOWLIST).strip().lower()
    return mode if mode in MODES else MODE_ALLOWLIST


def _policy(item):
    return {"mode": _mode(item), "addresses": _addresses(item)}


def _route_id(name):
    return f"{ROUTE_PREFIX}{name}"


def _route_name(value):
    name = str(value or "").strip().lower()
    if "@" in name:
        name = name.split("@", 1)[0].strip()
    if not NAME_PATTERN.fullmatch(name):
        raise FromError("route name must be 2-32 chars: lowercase letters, digits, hyphens")
    return name


def _scan_all(table):
    items, start = [], None
    while True:
        kwargs = {"ExclusiveStartKey": start} if start else {}
        page = table.scan(**kwargs)
        items.extend(page.get("Items") or [])
        start = page.get("LastEvaluatedKey")
        if not start:
            return items


def _read_default(table):
    item = table.get_item(Key={"id": ITEM_ID}).get("Item")
    if item is None:
        item = {"id": ITEM_ID, "mode": MODE_ALLOWLIST, "addresses": list(SEED)}
        table.put_item(Item=item)
    return _policy(item)


def _write_policy(table, item_id, policy):
    table.put_item(Item={
        "id": item_id,
        "mode": policy.get("mode") or MODE_ALLOWLIST,
        "addresses": list(policy.get("addresses") or []),
    })


def _read_route(table, name):
    item = table.get_item(Key={"id": _route_id(name)}).get("Item")
    if item is None:
        return None
    return _policy(item)


def _snapshot(table):
    default = _read_default(table)
    routes = {}
    for item in _scan_all(table):
        item_id = str(item.get("id") or "")
        if not item_id.startswith(ROUTE_PREFIX):
            continue
        name = item_id[len(ROUTE_PREFIX):]
        if not NAME_PATTERN.fullmatch(name):
            continue
        routes[name] = _policy(item)
    return {
        "default": default,
        "routes": dict(sorted(routes.items())),
        "addresses": default["addresses"],
    }


def _policy_for(table, name):
    if name:
        policy = _read_route(table, name)
        if policy is not None:
            return policy
    return _read_default(table)


def _new_route(table, name, mode):
    addresses = list(_read_default(table)["addresses"]) if mode == MODE_ALLOWLIST else []
    policy = {"mode": mode, "addresses": addresses}
    _write_policy(table, _route_id(name), policy)
    return policy


def _ensure_route(table, name, mode=None):
    """Create or update a route row. A new allow-list copies the default senders."""
    policy = _read_route(table, name)
    if policy is None:
        return _new_route(table, name, mode or MODE_ALLOWLIST)
    if mode is None or policy["mode"] == mode:
        return policy
    policy = dict(policy, mode=mode)
    if mode == MODE_ALLOWLIST and not policy["addresses"]:
        policy["addresses"] = list(_read_default(table)["addresses"])
    _write_policy(table, _route_id(name), policy)
    return policy


def api_list(table_ref=None):
    return 200, _snapshot(_table(table_ref))


def api_save(body, table_ref=None):
    body = body if isinstance(body, dict) else {}
    route_raw = body.get("route")
    mode_raw = body.get("mode")
    address_raw = body.get("address")
    route = str(route_raw).strip() if route_raw not in (None, "") else None
    mode = str(mode_raw).strip().lower() if mode_raw not in (None, "") else None
    address = address_raw if address_raw not in (None, "") else None
    if mode is not None and mode not in MODES:
        raise FromError("mode must be allowlist or anyone")
    if mode is not None and address is not None:
        raise FromError("anyone does not take a sender in the same call" if mode == MODE_ANYONE
                        else "set the mode and add a sender in separate calls")
    if route is not None:
        route = _route_name(route)
    table = _table(table_ref)
    if route is None:
        if mode is not None:
            raise FromError("set mode on a named address")
        return _add_address(table, None, address)
    if mode is not None:
        _ensure_route(table, route, mode=mode)
        return 200, _snapshot(table)
    return _add_address(table, route, address)


def api_add(address=None, route=None, mode=None, table_ref=None):
    body = {}
    if address is not None:
        body["address"] = address
    if route is not None:
        body["route"] = route
    if mode is not None:
        body["mode"] = mode
    return api_save(body, table_ref=table_ref)


def _add_address(table, route, address):
    bare = bare_address(address)
    if not bare:
        raise FromError("a sender must be one email address, without a wildcard")
    if route is None:
        policy = _read_default(table)
        added = bare not in policy["addresses"]
        if added:
            policy = dict(policy, addresses=policy["addresses"] + [bare])
            _write_policy(table, ITEM_ID, policy)
        payload = _snapshot(table)
        payload["added"] = added
        return 200, payload
    policy = _ensure_route(table, route)
    if policy["mode"] == MODE_ANYONE:
        raise FromError("this address accepts anyone; switch to allowlist first")
    added = bare not in policy["addresses"]
    if added:
        policy = dict(policy, addresses=policy["addresses"] + [bare])
        _write_policy(table, _route_id(route), policy)
    payload = _snapshot(table)
    payload["added"] = added
    return 200, payload


def api_remove(address, route=None, table_ref=None):
    bare = bare_address(address)
    if not bare:
        raise FromError("a sender must be one email address, without a wildcard")
    table = _table(table_ref)
    if route in (None, ""):
        policy = _read_default(table)
        removed = bare in policy["addresses"]
        if removed:
            policy = dict(policy, addresses=[item for item in policy["addresses"] if item != bare])
            _write_policy(table, ITEM_ID, policy)
        payload = _snapshot(table)
        payload["removed"] = removed
        return 200, payload
    name = _route_name(route)
    policy = _read_route(table, name)
    if policy is None:
        raise FromError("unknown route")
    if policy["mode"] == MODE_ANYONE:
        payload = _snapshot(table)
        payload["removed"] = False
        return 200, payload
    removed = bare in policy["addresses"]
    if removed:
        policy = dict(policy, addresses=[item for item in policy["addresses"] if item != bare])
        _write_policy(table, _route_id(name), policy)
    payload = _snapshot(table)
    payload["removed"] = removed
    return 200, payload


def _bare_candidates(raw):
    """Bare addresses from an SES From string or a Datamailer sender object.

    Datamailer inbound-email v1 stores ``sender`` as
    ``{"addresses": [...], "header": "Name <addr@host>"}``. SES stores a
    plain ``From`` string. Either form can carry a display name.
    """
    found = []

    def add(value):
        bare = bare_address(value)
        if bare and bare not in found:
            found.append(bare)

    if isinstance(raw, str):
        add(raw)
        return found
    if isinstance(raw, dict):
        header = raw.get("header")
        if isinstance(header, str):
            add(header)
        addresses = raw.get("addresses") or []
        if isinstance(addresses, str):
            addresses = [addresses]
        for item in addresses:
            if isinstance(item, str):
                add(item)
    return found


def sender_addresses(event):
    """Bare sender addresses on an email event, SES or Datamailer."""
    data = (event or {}).get("data") or {}
    raw = data.get("from")
    if raw in (None, ""):
        raw = data.get("sender")
    return _bare_candidates(raw)


def _local_part(value):
    text = str(value or "").strip()
    if not text:
        return ""
    _, parsed = parseaddr(text)
    address = (parsed or text).strip().lower()
    if "@" in address:
        return address.split("@", 1)[0].strip()
    return address.strip("<>").strip()


def _first_text(*values):
    for value in values:
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, str) and item.strip():
                    return item
    return ""


def route_name(event):
    """Inbound address local-part, or empty when the event has no route.

    Datamailer inbound-email v1 stores ``data.route``. SES-like events have
    no route field; the local-part of ``source``, ``data.to``, or
    ``data.recipients.matched[0]`` is used instead. Unknown or unparseable
    routes fall through to the default policy.
    """
    data = (event or {}).get("data") or {}
    recipients = data.get("recipients") if isinstance(data.get("recipients"), dict) else {}
    matched = recipients.get("matched")
    raw = _first_text(
        data.get("route"),
        (event or {}).get("source"),
        data.get("to"),
        matched,
    )
    return _local_part(raw)


def rejected(event):
    """True when this email must not run a flow.

    Other connectors are never rejected. With no sender table configured the
    check stays off, so deployments that have not created the table keep
    today's behavior. An email with no recognizable sender is rejected, even
    when the route accepts anyone.
    """
    if not isinstance(event, dict) or event.get("connector") != "email":
        return False
    if event.get("event", "message.received") != "message.received":
        # Authenticated SES delivery feedback describes outgoing mail; it has
        # no inbound sender and must reach its workflow watchers.
        return False
    if not os.environ.get(TABLE_ENV):
        return False
    senders = sender_addresses(event)
    if not senders:
        return True
    policy = _policy_for(_table(None), route_name(event))
    if policy["mode"] == MODE_ANYONE:
        return False
    allowed = set(policy["addresses"])
    return not any(address in allowed for address in senders)
