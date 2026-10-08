"""Human connection references: ``<service> <account>``.

A connection's ``connection_id`` is an internal key. One Google sign-in
backs several services, so an id like ``google-sheets`` names neither the
account nor everything it covers. People address connections the way the
console shows them instead: the service plus the signed-in account —
``drive alexey@datatalks.club``, or any unique fragment of the account
(``drive datatalks``). A bare service (``zoom``) works when only one
connection provides it, and an exact ``connection_id`` still resolves so
existing flows and scripts keep working.

Accepted spellings: ``drive datatalks``, ``drive:datatalks``,
``drive/datatalks``. Service names are catalog ids or labels
(``drive``, ``google drive``, ``sheets``, ``gmail``…).
"""

import re

from . import records
from . import services as connection_services


class RefError(ValueError):
    """The reference matches no connection, or more than one."""

    def __init__(self, message, candidates=()):
        super().__init__(message)
        self.candidates = list(candidates)


def account_of(connection):
    """The signed-in account label, or None before first consent."""
    return (connection.get("verified_account_id")
            or connection.get("account_title")
            or connection.get("expected_account_id")
            or None)


def refs_for(connection):
    """Every ``<service> <account>`` reference that addresses this
    connection, one per service it grants. Empty before sign-in: an
    unverified stub has no account to name."""
    account = account_of(connection)
    if not account:
        return []
    return [f"{service['id']} {account}"
            for service in connection_services.services_for(connection)]


def _service_ids(word):
    """Catalog service ids a word names (id, label, or label sans 'Google')."""
    word = word.strip().lower()
    if not word:
        return set()
    found = set()
    for spec in connection_services.CATALOG:
        label = spec["label"].lower()
        names = {spec["id"], label, label.removeprefix("google ").strip()}
        if word in names:
            found.add(spec["id"])
    if word == "google":
        found.add("google")
    return found


def _split(ref):
    """``(service_ids, account_fragment)`` for a reference string, or
    ``(set(), None)`` when no leading service name is recognised."""
    text = " ".join(str(ref or "").split())
    parts = re.split(r"[\s:/]+", text, maxsplit=1) if text else []
    if not parts:
        return set(), None
    # Two-word labels: "google drive datatalks".
    words = text.split(" ")
    if len(words) >= 2:
        two = _service_ids(" ".join(words[:2]))
        if two:
            return two, " ".join(words[2:]).strip() or None
    service_ids = _service_ids(parts[0])
    if not service_ids:
        return set(), None
    rest = parts[1].strip() if len(parts) > 1 else ""
    return service_ids, rest or None


def _describe(connection):
    account = account_of(connection) or "not signed in"
    labels = ", ".join(s["id"] for s in connection_services.services_for(connection))
    return f"{labels} {account} ({connection.get('status')})"


def resolve(connections, ref):
    """The single connection ``ref`` addresses among ``connections``.

    Raises RefError (with ``candidates``) when nothing or more than one
    connection matches. Among several matches a single connected one wins,
    so a lingering revoked or half-set-up duplicate never shadows the live
    sign-in.
    """
    text = " ".join(str(ref or "").split())
    if not text:
        raise RefError("Name a connection, e.g. 'drive alexey@example.com'")
    lowered = text.lower()
    for connection in connections:
        if str(connection.get("connection_id") or "") == lowered:
            return connection
    service_ids, fragment = _split(text)
    if not service_ids:
        raise RefError(
            f"No connection matches '{text}'. Use '<service> <account>', "
            "e.g. 'drive alexey@example.com'")
    matches = []
    for connection in connections:
        granted = {s["id"] for s in connection_services.services_for(connection)}
        if connection.get("provider") == "google":
            granted.add("google")
        if not granted & service_ids:
            continue
        if fragment:
            account = (account_of(connection) or "").lower()
            if fragment.lower() not in account:
                continue
        matches.append(connection)
    if not matches:
        raise RefError(f"No connection matches '{text}'")
    if len(matches) > 1:
        # An exact account beats a fragment that also hits a longer one.
        if fragment:
            exact = [c for c in matches
                     if (account_of(c) or "").lower() == fragment.lower()]
            if exact:
                matches = exact
        live = [c for c in matches if c.get("status") == records.STATUS_CONNECTED]
        if len(live) == 1:
            return live[0]
        if len(matches) > 1:
            raise RefError(
                f"'{text}' matches {len(matches)} connections; add more of the account",
                candidates=[_describe(c) for c in matches])
    return matches[0]
