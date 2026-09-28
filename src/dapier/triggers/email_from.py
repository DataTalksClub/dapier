"""One sender allow-list for every inbound email.

The list is not stored on each address. A missing row is the seed. An empty
row accepts nobody. The workflow worker calls :func:`rejected` before any
email flow matches.
"""

import os
import re
from email.utils import parseaddr


TABLE_ENV = "EMAIL_FROM_TABLE"
ITEM_ID = "from-allow"
SEED = (
    "alexey.s.grigoriev@gmail.com",
    "alexey@datatalks.club",
)


class FromError(ValueError):
    """A sender address cannot be stored."""


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


def _read(table):
    item = table.get_item(Key={"id": ITEM_ID}).get("Item")
    if item is None:
        item = {"id": ITEM_ID, "addresses": list(SEED)}
        table.put_item(Item=item)
    return [str(address).strip().lower() for address in item.get("addresses") or []]


def _write(table, addresses):
    table.put_item(Item={"id": ITEM_ID, "addresses": list(addresses)})


def api_list(table_ref=None):
    return 200, {"addresses": _read(_table(table_ref))}


def api_add(address, table_ref=None):
    bare = bare_address(address)
    if not bare:
        raise FromError("a sender must be one email address, without a wildcard")
    table = _table(table_ref)
    addresses = _read(table)
    added = bare not in addresses
    if added:
        addresses.append(bare)
        _write(table, addresses)
    return 200, {"addresses": addresses, "added": added}


def api_remove(address, table_ref=None):
    bare = bare_address(address)
    if not bare:
        raise FromError("a sender must be one email address, without a wildcard")
    table = _table(table_ref)
    addresses = _read(table)
    removed = bare in addresses
    if removed:
        addresses = [item for item in addresses if item != bare]
        _write(table, addresses)
    return 200, {"addresses": addresses, "removed": removed}


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


def rejected(event):
    """True when this email must not run a flow.

    Other connectors are never rejected. With no sender table configured the
    check stays off, so deployments that have not created the table keep
    today's behavior. An email with no recognizable sender is rejected.
    """
    if not isinstance(event, dict) or event.get("connector") != "email":
        return False
    if not os.environ.get(TABLE_ENV):
        return False
    allowed = set(_read(_table(None)))
    return not any(address in allowed for address in sender_addresses(event))
