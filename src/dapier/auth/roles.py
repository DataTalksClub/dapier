"""Named user roles over the operator gate: viewer < editor < operator < admin.

Roles v1 close the "no user/role model" gap from the Zapier gap analysis
while leaving every existing deployment byte-for-byte compatible:

1. The stored assignments (``RoleAssignmentsTable``, keyed by DTC subject or
   email) decide a session's role: ``viewer`` reaches the read-only
   surfaces, ``editor`` additionally workflow editing and testing,
   ``operator`` the full console minus user management, and ``admin``
   additionally manages users. A ``disabled`` assignment is denied
   everywhere.
2. With no stored row the operator allowlist (``authz``) stays authoritative
   exactly as before: an allowlisted session resolves to ``admin`` while the
   table is empty — today's operators keep full power and can bootstrap the
   first assignments — and to ``operator`` once any row exists. A
   non-allowlisted session with no row resolves to None, the pre-roles
   verdict, so an empty table reproduces the historical gate bit for bit.

Mutations go through :func:`api_set_role` / :func:`api_remove_role`, which
guard the last active admin: demoting, disabling, or removing them is
refused, so a deployment can never lock itself out of user management.

Table discipline: like every table here, attribute names never appear bare
in expressions — this module only reads and writes whole items via
get/put/delete/scan, so no expression grammar is used at all.
"""

import os
import re
from datetime import datetime, timezone

from .. import audit as audit_log
from . import authz

ROLES = ("viewer", "editor", "operator", "admin")

# "admin" implies every role, "operator" implies editor+viewer; matches how
# the grants table implies operations. An unknown role (None, "disabled")
# implies nothing.
_IMPLIED = {
    "viewer": frozenset({"viewer"}),
    "editor": frozenset({"viewer", "editor"}),
    "operator": frozenset({"viewer", "editor", "operator"}),
    "admin": frozenset(ROLES),
}


def roles_table():
    import boto3

    return boto3.resource("dynamodb").Table(os.environ["ROLE_ASSIGNMENTS_TABLE"])


def _table(table_ref=None):
    """The caller-supplied table, else the configured one; None when the
    store is unconfigured (every caller then degrades to legacy behavior)."""
    if table_ref is not None:
        return table_ref
    if not os.environ.get("ROLE_ASSIGNMENTS_TABLE", ""):
        return None
    try:
        return roles_table()
    except KeyError:
        return None


def normalize_identity(identity):
    """Accept a DTC subject or an email; emails are matched case-insensitively."""
    cleaned = str(identity or "").strip()
    if not cleaned:
        raise ValueError("Subject (DTC subject or email) is required")
    if "@" in cleaned:
        cleaned = cleaned.lower()
    return cleaned


def validate_role(role):
    cleaned = str(role or "").strip().lower()
    if cleaned not in ROLES:
        raise ValueError(f"Role must be one of: {', '.join(ROLES)}")
    return cleaned


def satisfies(role, minimum):
    """True when ``role`` carries at least ``minimum``'s powers."""
    return bool(role) and minimum in _IMPLIED.get(role, frozenset())


def get_role(subject, table_ref=None):
    """The stored assignment row for ``subject``, or None. Store failures
    read as "no row", which degrades to the allowlist verdict."""
    table = _table(table_ref)
    if table is None:
        return None
    try:
        identity = normalize_identity(subject)
    except ValueError:
        return None
    try:
        return table.get_item(Key={"identity": identity}).get("Item")
    except Exception:  # noqa: BLE001 — reads never fail the request path
        return None


def stored_assignment(table, session_payload):
    """The stored assignment for this session's subject, else its email."""
    payload = session_payload or {}
    subject = str(payload.get("subject") or "").strip()
    if subject:
        item = table.get_item(Key={"identity": subject}).get("Item")
        if item and item.get("role"):
            return item
    email = str(payload.get("sub") or "").strip().lower()
    if email and email != subject:
        item = table.get_item(Key={"identity": email}).get("Item")
        if item and item.get("role"):
            return item
    return None


def list_rows(table):
    try:
        return table.scan().get("Items", [])
    except Exception:  # noqa: BLE001
        return []


def _store_is_empty(table):
    try:
        return not table.scan(Limit=1).get("Items")
    except Exception:  # noqa: BLE001
        return True


def effective_role(session_payload, table_ref=None):
    """The session's effective role, or None (the pre-roles verdict).

    A stored assignment wins: ``disabled`` denies everywhere, otherwise the
    stored role applies — narrowing an allowlisted operator to viewer, or
    widening a non-allowlisted identity into a band. With no stored row the
    allowlist fallback applies (see the module docstring). Table problems —
    not provisioned yet, permissions — never break a request: they leave the
    allowlist verdict standing.
    """
    if not session_payload:
        return None
    table = _table(table_ref)
    row = None
    if table is not None:
        try:
            row = stored_assignment(table, session_payload)
        except Exception:  # noqa: BLE001
            row = None
    if row is not None:
        if row.get("disabled"):
            return "disabled"
        role = str(row.get("role") or "").strip().lower()
        return role if role in ROLES else None
    if not authz.is_operator(session_payload):
        return None
    if table is None or _store_is_empty(table):
        return "admin"  # legacy: the allowlist is the whole authority
    return "operator"


# --- API helpers (shared by the console and CLI layers) ----------------------

USER_PUBLIC_FIELDS = ("subject", "role", "display_name", "disabled",
                      "assigned_by", "assigned_at", "updated_at", "updated_by")


def public_user(row):
    """One assignment as the users API returns it: the ``identity`` key is
    exposed as ``subject``."""
    view = {key: row.get(key) for key in USER_PUBLIC_FIELDS if row.get(key) is not None}
    if "subject" not in view:
        view["subject"] = row.get("identity")
    return view


def _active_admins(table):
    return [row for row in list_rows(table)
            if row.get("role") == "admin" and not row.get("disabled")]


def _last_admin_guard(table, identity, *, losing):
    """409 when ``identity`` is the only active admin and the change would
    remove them (a demote, a disable, or a delete)."""
    if not losing:
        return None
    remaining = [row for row in _active_admins(table)
                 if str(row.get("identity")) != str(identity)]
    if not remaining:
        return 409, {"error": "Refusing to remove the last admin: make another user an admin first"}
    return None


def api_list_users(table_ref=None):
    """Every stored user, subject-ordered: ``(status, payload)``."""
    table = _table(table_ref)
    if table is None:
        return 200, {"users": []}
    rows = sorted(list_rows(table), key=lambda row: str(row.get("identity") or ""))
    return 200, {"users": [public_user(row) for row in rows]}


def api_set_role(body, *, operator, table_ref=None):
    """Create or update one user's role. Returns ``(status, payload)``.

    Body: ``subject`` (required; an email or DTC subject), ``role``
    (admin | operator | editor | viewer), optional ``display_name`` and
    ``disabled``. Shared by the console (cookie) and CLI (bearer) layers;
    the audit row is written here so both surfaces record
    ``users.set-role``.
    """
    if not isinstance(body, dict):
        return 400, {"error": "Invalid request"}
    try:
        identity = normalize_identity(body.get("subject") or body.get("identity"))
        role = validate_role(body.get("role"))
    except ValueError as exc:
        return 400, {"error": str(exc)}
    table = _table(table_ref)
    if table is None:
        return 503, {"error": "The users table is not configured"}
    try:
        previous = table.get_item(Key={"identity": identity}).get("Item")
    except Exception:  # noqa: BLE001
        previous = None
    losing = bool(
        previous and previous.get("role") == "admin" and not previous.get("disabled")
        and (role != "admin" or body.get("disabled"))
    )
    refusal = _last_admin_guard(table, identity, losing=losing)
    if refusal is not None:
        return refusal
    now = datetime.now(timezone.utc).isoformat()
    item = {
        "identity": identity,
        "role": role,
        "assigned_by": str(operator or "unknown"),
        "assigned_at": (previous or {}).get("assigned_at") or now,
        "updated_at": now,
        "updated_by": str(operator or "unknown"),
    }
    display_name = str(body.get("display_name") or "").strip()
    if display_name:
        item["display_name"] = display_name
    elif previous:
        item["display_name"] = previous.get("display_name")
    if "disabled" in body:
        if body.get("disabled"):
            item["disabled"] = True
    elif previous:
        item["disabled"] = previous.get("disabled")
    table.put_item(Item=item)
    audit_log.emit(identity, "users.set-role", item["updated_by"], outcome=role)
    return 200, public_user(item)


def api_remove_role(subject, *, operator, table_ref=None):
    """Delete one user's stored role; the allowlist fallback applies again.
    Returns ``(status, payload)``. Refuses to remove the last admin."""
    try:
        identity = normalize_identity(subject)
    except ValueError as exc:
        return 400, {"error": str(exc)}
    table = _table(table_ref)
    if table is None:
        return 503, {"error": "The users table is not configured"}
    try:
        previous = table.get_item(Key={"identity": identity}).get("Item")
    except Exception:  # noqa: BLE001
        previous = None
    if previous is None:
        return 404, {"error": f"No stored role for {identity}"}
    refusal = _last_admin_guard(
        table, identity,
        losing=previous.get("role") == "admin" and not previous.get("disabled"),
    )
    if refusal is not None:
        return refusal
    table.delete_item(Key={"identity": identity})
    audit_log.emit(identity, "users.remove", str(operator or "unknown"), outcome="ok")
    return 200, {"ok": True, "subject": identity}


# --- Route classification ----------------------------------------------------
#
# Both API surfaces gate everything at operator today. Roles v1 lowers the
# bar for two well-defined bands, adds an admin band for user management,
# and leaves everything else — connections, credentials, grants, tokens,
# trigger definitions — at operator. Anything unclassified stays operator,
# so a new route can never accidentally become readable by a viewer.

# (pattern, methods) pairs a ``viewer`` may reach: read-only surfaces.
_VIEWER_ROUTES = (
    (r"/api/admin/overview", ("GET",)),
    (r"/api/admin/audit(?:/export)?", ("GET",)),
    (r"/api/admin/runs(?:/export)?", ("GET",)),
    (r"/api/admin/runs/[^/]+", ("GET",)),
    (r"/api/admin/usage", ("GET",)),
    (r"/api/admin/errors/summary", ("GET",)),
    (r"/api/admin/triggers/inbox(?:/[^/]+)?", ("GET",)),
    (r"/api/admin/triggers/sample", ("GET",)),
    (r"/api/admin/designer/catalog", ("GET",)),
    (r"/api/admin/designer/workflows", ("GET",)),
    (r"/api/admin/designer/templates", ("GET",)),
    (r"/api/admin/designer/export(?:-all)?", ("GET",)),
    (r"/api/admin/designer/workflows/[^/]+(?:/versions(?:/diff)?)?", ("GET",)),
    (r"/api/admin/designer/workflows/[^/]+/draft(?:/diff)?", ("GET",)),
    (r"/api/admin/grants", ("GET",)),
    (r"/api/admin/tokens", ("GET",)),
    (r"/api/admin/(?:email|hook|schedule|poll)-triggers", ("GET",)),
    (r"/api/admin/oauth-clients", ("GET",)),
    (r"/api/admin/storage/[^/]+", ("GET",)),
    (r"/api/admin/connections/[^/]+/discover(?:/[^/]+)?", ("GET",)),
)

# Pairs an ``editor`` may reach: workflow editing/testing and operational
# nudges (replays, cancels, storage values, connection health tests).
_EDITOR_ROUTES = (
    (r"/api/admin/runs/[^/]+/(?:replay|cancel)", ("POST",)),
    (r"/api/admin/runs/replay-failed", ("POST",)),
    (r"/api/admin/triggers/inbox/[^/]+/replay", ("POST",)),
    (r"/api/admin/designer/workflows", ("PUT",)),
    (r"/api/admin/designer/workflows/test(?:-step)?", ("POST",)),
    (r"/api/admin/designer/workflows/bulk", ("POST",)),
    (r"/api/admin/designer/workflows/[^/]+", ("PUT", "DELETE")),
    (r"/api/admin/designer/workflows/[^/]+/(?:tags|folder)", ("PUT",)),
    (r"/api/admin/designer/workflows/[^/]+/publish", ("POST",)),
    (r"/api/admin/designer/workflows/[^/]+/draft", ("DELETE",)),
    (r"/api/admin/designer/workflows/[^/]+/(?:test|test-step|duplicate|rollback)", ("POST",)),
    (r"/api/admin/designer/workflows/[^/]+/template", ("PUT",)),
    (r"/api/admin/designer/templates/[^/]+/apply", ("POST",)),
    (r"/api/admin/copilot/draft", ("POST",)),
    (r"/api/admin/discover", ("POST",)),
    (r"/api/admin/connections/[^/]+/test", ("POST",)),
    (r"/api/admin/errors/digest", ("POST",)),
    (r"/api/admin/storage/[^/]+", ("POST", "DELETE")),
)

# User management: stored admins only (allowlist operators count as admins
# while the store is empty, so the first assignment can always be made).
_ADMIN_ROUTES = (
    (r"/api/admin/users(?:/[^/]+)?", ("GET", "POST", "DELETE")),
)


def minimum_for_route(method, path):
    """The least role that may hit this console route; ``operator`` by default."""
    method = str(method or "").upper()
    for pattern, methods in _ADMIN_ROUTES:
        if method in methods and re.fullmatch(pattern, path or ""):
            return "admin"
    for pattern, methods in _VIEWER_ROUTES:
        if method in methods and re.fullmatch(pattern, path or ""):
            return "viewer"
    for pattern, methods in _EDITOR_ROUTES:
        if method in methods and re.fullmatch(pattern, path or ""):
            return "editor"
    return "operator"


# The CLI surface gates by audit action instead of route; the split mirrors
# minimum_for_route. ``runs``/``triggers.inbox``/``errors`` stayed shared by
# reads and mutations once, so the mutating handlers now pass their precise
# action names (matching what they already write to the audit trail).
_VIEWER_ACTIONS = frozenset({
    "overview", "usage", "runs", "runs.export", "audit", "audit.export",
    "errors", "workflow.versions", "workflow.export", "workflow.export-all",
    "triggers.sample", "triggers.inbox", "storage.read", "connections.discover",
})

_EDITOR_ACTIONS = frozenset({
    "workflow.save", "workflow.toggle", "workflow.delete", "workflow.duplicate",
    "workflow.tags", "workflow.folder", "workflow.bulk-toggle",
    "workflow.rollback", "workflow.test", "workflow.test-step", "workflow.draft",
    "workflow.publish", "workflow.discard",
    "runs.replay", "runs.cancel", "runs.replay-failed",
    "triggers.inbox-replay", "storage.write", "connections.test",
    "errors.send-digest",
})

_ADMIN_ACTIONS = frozenset({"users"})


def minimum_for_action(action):
    """The least role that may perform this audited action; ``operator`` by default."""
    action = str(action or "").strip()
    if action in _ADMIN_ACTIONS:
        return "admin"
    if action in _VIEWER_ACTIONS:
        return "viewer"
    if action in _EDITOR_ACTIONS:
        return "editor"
    return "operator"
