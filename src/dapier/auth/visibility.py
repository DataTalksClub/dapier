"""Multi-user read filtering (G17 Phase 2): what a session may see.

Workflows are the owned items — ``owner`` is the subject stamped at publish
(G17 Phase 1), and a draft-only workflow is owned by its ``drafted_by``. The
rule, applied at the read surfaces (designer list, overview, runs, inbox,
usage):

1. Operators (the allowlist verdict the route gates apply) see everything.
2. Any other subject sees the workflows it owns.
3. An item with no owner is visible to everyone — defensive, so a missing
   stamp never hides data.

Rows that only reference a workflow (runs, inbox matches, usage rollups) are
visible when the referenced workflow is; a row whose workflow no longer
exists resolves to no owner, so it stays visible — operators keep their
audit duty over history, and the defensive rule applies.

Phase 3 adds the write-side mirror: an owner-or-operator gate for the
workflow WRITE routes. The rule is the same verdict, but the defensive
default flips — a write to an item that exists with no owner stamp is
DENIED for non-operators (on writes a missing stamp must not open the
door, unlike reads), while an id nothing stored claims is a create and
stays open.

Read filtering never gated a write, and the engine is untouched —
matching, triggers, and the worker read the same draft-blind loader as
before.
"""

from ..triggers import published_workflows
from . import authz

__all__ = ["Visibility", "visible_to", "owner_of_item", "workflow_owners",
           "owners_for", "for_session", "owner_for_write",
           "ensure_can_write"]


def visible_to(subject, owner, *, is_operator=False):
    """The G17 rule for one owned item: ``True`` when ``subject`` may see it.

    Operators see everything; a subject sees its own items; an item with no
    owner (no stamp, no published_by to backfill) is visible to everyone.
    """
    if is_operator:
        return True
    owner = str(owner or "")
    return not owner or owner == str(subject or "")


def owner_of_item(item):
    """The owner of one stored workflow item.

    A draft-only row (``draft_of`` set) is owned by its ``drafted_by`` —
    drafts are never published, so they carry no owner stamp (Phase 1). A
    live item resolves like every Phase 1 read: the stamped ``owner``,
    backfilled from ``published_by`` (see published_workflows.resolve_owner).
    """
    if item.get("draft_of"):
        return str(item.get("drafted_by") or "")
    return published_workflows.resolve_owner(item)


def workflow_owners(table_ref=None):
    """``{workflow_id: owner}`` over the published store — the map the
    workflow_id-keyed surfaces (runs, inbox, usage) resolve their rows
    against. Includes draft-only rows (owned by ``drafted_by``); empty when
    the store is unconfigured, and store failures degrade to an empty map
    (everything visible) rather than failing the list."""
    if not published_workflows.configured():
        return {}
    try:
        items = published_workflows.load_items(include_drafts=True)
    except Exception:  # noqa: BLE001 — filtering must never fail a read
        return {}
    owners = {}
    for item in items:
        workflow_id = str(item.get("draft_of") or item.get("workflow_id") or "")
        if workflow_id:
            owners[workflow_id] = owner_of_item(item)
    return owners


def owner_for_write(workflow_id):
    """The owner a write to ``workflow_id`` answers to: the live item's
    resolved owner (the Phase 1 stamp, ``published_by`` backfilled), else a
    draft-only row's ``drafted_by`` — drafts are what a save really writes
    before any publish. ``None`` when nothing is stored under the id (an
    unclaimed id, i.e. a create) or the store is unconfigured or unreadable
    (the write itself will surface that; a lookup failure never fakes a
    denial)."""
    workflow_id = str(workflow_id or "")
    if not workflow_id or not published_workflows.configured():
        return None
    try:
        item = published_workflows.get_item(workflow_id)
        if item is None:
            item = published_workflows.get_draft(workflow_id)
    except Exception:  # noqa: BLE001 — the gate must not fail the route
        return None
    if not item:
        return None
    return owner_of_item(item)


def ensure_can_write(subject, workflow_id, *, is_operator):
    """The G17 Phase 3 write rule for one workflow: ``None`` when the write
    may proceed, else ``(403, {"error": ...})`` — the ``(status, payload)``
    shape the route layers already return.

    Operators write anything. A
    non-operator writes only what it owns; an id nothing stored claims is a
    create and stays open. An item that exists with no owner stamp is
    DENIED for non-operators — the write-side default is the safe
    direction: unlike reads (where a missing stamp must never hide data),
    a missing stamp on a write must never open the door."""
    if is_operator:
        return None
    owner = owner_for_write(workflow_id)
    if owner is None:  # unclaimed id: a create, or the store cannot answer
        return None
    if owner == str(subject or ""):
        return None
    return 403, {"error": f"workflow {workflow_id} belongs to another owner — "
                          "only its owner or an operator may modify it"}


class Visibility:
    """One caller's read scope over the owned workflows.

    Built from the session by :func:`for_session` (or :func:`for_role` when
    the dispatcher already resolved the effective role for its gate). Call
    sites short-circuit on ``is_operator`` — an unrestricted caller never
    needs the owners map.
    """

    __slots__ = ("subject", "is_operator")

    def __init__(self, subject, *, is_operator=False):
        self.subject = str(subject or "")
        self.is_operator = bool(is_operator)

    def owner_visible(self, owner):
        return visible_to(self.subject, owner, is_operator=self.is_operator)

    def workflow_visible(self, workflow_id, owners):
        """Whether a row tied to ``workflow_id`` is visible, its owner
        resolved from ``owners`` (a :func:`workflow_owners` map). A workflow
        the map does not know — deleted, or the store unreadable — resolves
        to no owner, so the row stays visible (the defensive rule)."""
        if self.is_operator:
            return True
        return self.owner_visible((owners or {}).get(str(workflow_id or "")))

    def can_write(self, workflow_id):
        """The Phase 3 write gate for one workflow id (see
        :func:`ensure_can_write`): ``None`` when this scope may write it,
        else the ``(403, {"error"})`` denial — call sites holding a scope
        skip re-deriving subject and operator verdict."""
        return ensure_can_write(self.subject, workflow_id,
                                is_operator=self.is_operator)


def owners_for(visible):
    """The workflow-owner map a filtered read resolves rows against.

    An unrestricted caller (no scope, or an operator) skips the store read.
    """
    if visible is None or visible.is_operator:
        return {}
    return workflow_owners()


def for_session(payload, table_ref=None):
    """The scope of a console cookie / CLI bearer session payload: the
    subject it names, with the operator allowlist (authz) deciding the
    reach."""
    payload = payload or {}
    subject = payload.get("subject") or payload.get("sub")
    return Visibility(str(subject) if subject else "",
                      is_operator=authz.is_operator(payload))
