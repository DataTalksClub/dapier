"""Google account selection shared by the gmail/gdrive agent ops.

One Google sign-in backs Gmail, Drive, Docs and Sheets at once, so the
``--account`` an operator passes names a *connection*: its verified email,
its connection id, or a unique fragment of either. The resolver here is the
single gate every ``/api/agent/gmail/*`` and ``/api/agent/gdrive/*``
operation goes through — it picks exactly one connection and never silently
switches accounts: an omitted ``--account`` is rejected, an ambiguous fragment is an error
that names the candidates, and every error lists the accounts that exist so
the operator can re-run with an explicit choice.
"""

from ...connections import records as connections
from ...connections import refs as connection_refs
from ...connections import services as connection_services

from .common import _LateBinding

# Resolved through the agent package at call time, so tests patch
# agent.<name> and this module sees the patch like every route module.
_tables = _LateBinding("_tables")


class GoogleAccountError(Exception):
    """Operator-facing account-selection failure; never carries a token."""

    def __init__(self, message, status=400, accounts=None):
        super().__init__(message)
        self.status = status
        self.accounts = accounts or []


def _account_view(connection):
    account = connection_refs.account_of(connection)
    return {
        "connection_id": connection.get("connection_id"),
        "email": connection.get("verified_account_id") or account,
        "title": connection.get("account_title") or None,
        "refs": connection_refs.refs_for(connection),
        "status": connection.get("status"),
    }


def _label(view):
    email = view.get("email") or "(not signed in)"
    return f"{view.get('connection_id')} ({email})"


def list_accounts(service_id):
    """Every Google connection granting ``service_id``, as account views."""
    table, _grants = _tables()
    found = []
    for connection in sorted(connections._scan_all(table),
                             key=lambda item: str(item.get("connection_id") or "")):
        if str(connection.get("provider") or "").lower() != "google":
            continue
        granted = {service["id"] for service in connection_services.services_for(connection)}
        granted.add("google")
        if service_id not in granted:
            continue
        found.append(_account_view(connection))
    return found


def resolve_account(service_id, account=None):
    """The one Google connection ``account`` names for ``service_id``.

    ``account`` (the CLI's ``--account``) matches a connection id exactly,
    a verified email exactly (case-insensitive), or a unique fragment of
    email/title. Without it, the request is rejected and the error names the
    available accounts. Raises GoogleAccountError on anything ambiguous.
    """
    accounts = list_accounts(service_id)
    connected = [view for view in accounts if view.get("status") == connections.STATUS_CONNECTED]
    candidates = connected or accounts
    if not candidates:
        raise GoogleAccountError(
            f"No Google connection grants {service_id}; connect one first "
            "(dapier connections create)", accounts=accounts)
    if account is None or not str(account).strip():
        raise GoogleAccountError(
            "--account is required: "
            + str(len(candidates)) + " Google accounts serve "
            + service_id + ": " + ", ".join(_label(v) for v in candidates)
            + ". Pass --account <email-or-connection-id>.",
            accounts=accounts)
    wanted = str(account).strip()
    lowered = wanted.lower()
    by_id = [view for view in candidates
             if str(view.get("connection_id") or "").lower() == lowered]
    if len(by_id) == 1:
        return by_id[0]["connection_id"]
    by_email = [view for view in candidates
                if str(view.get("email") or "").lower() == lowered]
    if len(by_email) == 1:
        return by_email[0]["connection_id"]
    fragments = [view for view in candidates
                 if lowered in str(view.get("email") or "").lower()
                 or lowered in str(view.get("title") or "").lower()]
    if len(fragments) == 1:
        return fragments[0]["connection_id"]
    if len(fragments) > 1:
        raise GoogleAccountError(
            f"Ambiguous account {wanted!r}: matches "
            + ", ".join(_label(v) for v in fragments)
            + ". Use the full email or connection id.",
            accounts=accounts)
    raise GoogleAccountError(
        f"No {service_id} account matches {wanted!r}. Available: "
        + (", ".join(_label(v) for v in accounts) or "none"),
        accounts=accounts)
