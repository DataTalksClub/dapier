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


class RefError(LookupError):
    """The reference matches no connection, or more than one."""

    def __init__(self, message, candidates=()):
        super().__init__(message)
        self.candidates = list(candidates)


def account_of(connection):
    """The signed-in account as people know it (email, workspace, channel
    name — the provider's title before its opaque id), or None before
    first consent."""
    return (connection.get("account_title")
            or connection.get("verified_account_id")
            or connection.get("expected_account_id")
            or None)


def _nickname(connection):
    """Who the credential acts as inside its account (``Au-Tomator`` for a
    Slack app, ``Alexey Grigorev`` for a person's token), when it says more
    than the id or the account. Recorded at verification; see
    ``records.account_identity``."""
    identity = connection.get("account_identity")
    name = str((identity or {}).get("name") or "").strip() if isinstance(identity, dict) else ""
    plain = {str(connection.get(key) or "") for key in
             ("connection_id", "account_title", "verified_account_id")}
    return name if name and name not in plain else None


def _legacy_name(connection):
    """A retired display name still stored on older records. It never
    labels anything; it only keeps references written with it resolving."""
    name = str(connection.get("display_name") or "").strip()
    return name or None


def _service_ids_of(connection):
    return [service["id"] for service in connection_services.services_for(connection)]


def _account_label(connection, peers):
    """The account part of a reference. When another connection signs in to
    the same account for an overlapping service (two Slack bots in one
    workspace), who the token acts as is appended so each reference stays unique:
    ``DataTalks.Club / Au-Tomator``."""
    account = account_of(connection)
    nickname = _nickname(connection)
    if not peers or not nickname:
        return account
    services = set(_service_ids_of(connection))
    for other in peers:
        if other.get("connection_id") == connection.get("connection_id"):
            continue
        if account_of(other) == account and services & set(_service_ids_of(other)):
            return f"{account} / {nickname}"
    return account


def refs_for(connection, peers=None):
    """Every ``<service> <account>`` reference that addresses this
    connection, one per service it grants. ``peers`` (the other
    connections) lets duplicates on one account disambiguate. Empty before
    sign-in: an unverified stub has no account to name."""
    if not account_of(connection):
        return []
    label = _account_label(connection, peers)
    return [f"{service_id} {label}" for service_id in _service_ids_of(connection)]


def with_refs(views):
    """Re-stamp each view's ``refs`` knowing every other connection, so
    lists hand out references that resolve to exactly one connection."""
    for view in views:
        view["refs"] = refs_for(view, views)
    return views


def _names(connection):
    """Every spelling of the account a reference fragment may match."""
    account = account_of(connection) or ""
    names = [account]
    for nickname in (_nickname(connection), _legacy_name(connection)):
        if nickname:
            names += [nickname, f"{account} / {nickname}"]
    names += [str(connection.get(key) or "") for key in
              ("account_title", "verified_account_id", "expected_account_id")]
    return [name.lower() for name in names if name]


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


def _describe(connection, peers):
    refs = refs_for(connection, peers)
    if refs:
        return f"{refs[0]} ({connection.get('status')})"
    labels = ", ".join(_service_ids_of(connection))
    return f"{labels} not signed in ({connection.get('status')})"


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
        if fragment and not any(fragment.lower() in name for name in _names(connection)):
            continue
        matches.append(connection)
    if not matches:
        raise RefError(f"No connection matches '{text}'")
    if len(matches) > 1:
        # An exact account beats a fragment that also hits a longer one.
        if fragment:
            exact = [c for c in matches if fragment.lower() in _names(c)]
            if exact:
                matches = exact
        live = [c for c in matches if c.get("status") == records.STATUS_CONNECTED]
        if len(live) == 1:
            return live[0]
        if len(matches) > 1:
            raise RefError(
                f"'{text}' matches {len(matches)} connections; add more of the account",
                candidates=[_describe(c, connections) for c in matches])
    return matches[0]
