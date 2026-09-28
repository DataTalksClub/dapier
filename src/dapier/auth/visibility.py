"""Multi-user read filtering (G17 Phase 2): what a session may see.

Workflows are the owned items — ``owner`` is the subject stamped at publish
(G17 Phase 1), and a draft-only workflow is owned by its ``drafted_by``. The
rule, applied at the read surfaces (designer list, overview, runs, inbox,
usage):

1. Operators (and admins, via the same roles.py verdict the route gates use)
   see everything.
2. Any other subject sees the workflows it owns.
3. An item with no owner is visible to everyone — defensive, so a missing
   stamp never hides data.

Rows that only reference a workflow (runs, inbox matches, usage rollups) are
visible when the referenced workflow is; a row whose workflow no longer
exists resolves to no owner, so it stays visible — operators keep their
audit duty over history, and the defensive rule applies.

Read filtering only: nothing here gates a write (owner-or-operator write
checks are Phase 3), and the engine is untouched — matching, triggers, and
the worker read the same draft-blind loader as before.
"""

from ..triggers import published_workflows
from . import roles

__all__ = ["Visibility", "visible_to", "owner_of_item", "workflow_owners",
           "for_role", "for_session"]


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


def for_role(subject, effective_role):
    """The scope for an already-resolved effective role — the dispatchers
    compute it for their gate anyway (roles.effective_role), so this is the
    cheap path. Operator-or-admin sees everything; everyone else is
    owner-scoped to their subject."""
    return Visibility(subject, is_operator=roles.satisfies(effective_role, "operator"))


def for_session(payload, table_ref=None):
    """The scope of a console cookie / CLI bearer session payload: the
    subject it names, restricted exactly as roles.py restricts it."""
    payload = payload or {}
    subject = payload.get("subject") or payload.get("sub")
    return for_role(str(subject) if subject else "",
                    roles.effective_role(payload, table_ref))
