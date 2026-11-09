"""Workflow write gate: who may change a workflow.

Reads are not scoped. Every signed-in user sees the same Home, workflows,
runs, failures and connections; there are no per-identity views.

Workflows are the owned items — ``owner`` is the subject stamped at publish,
and a draft-only workflow is owned by its ``drafted_by``. The write rule
(the owner-or-operator gate on the workflow WRITE routes): operators write
anything; a non-operator writes only what it owns. A write to an item that
exists with no owner stamp is DENIED for non-operators, while an id nothing
stored claims is a create and stays open.
"""

from ..triggers import published_workflows
from . import authz

__all__ = ["Visibility", "owner_of_item", "for_session", "owner_for_write",
           "ensure_can_write"]


def owner_of_item(item):
    """The owner of one stored workflow item.

    A draft-only row (``draft_of`` set) is owned by its ``drafted_by`` —
    drafts are never published, so they carry no owner stamp. A live item
    answers with its stamp: every live row carries one (the Oct-2026
    store-check found none without), so no published_by backfill.
    """
    if item.get("draft_of"):
        return str(item.get("drafted_by") or "")
    return str(item.get("owner") or "")


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
    """One caller's write scope over the owned workflows, built from the
    session by :func:`for_session`."""

    __slots__ = ("subject", "is_operator")

    def __init__(self, subject, *, is_operator=False):
        self.subject = str(subject or "")
        self.is_operator = bool(is_operator)

    def can_write(self, workflow_id):
        """The Phase 3 write gate for one workflow id (see
        :func:`ensure_can_write`): ``None`` when this scope may write it,
        else the ``(403, {"error"})`` denial — call sites holding a scope
        skip re-deriving subject and operator verdict."""
        return ensure_can_write(self.subject, workflow_id,
                                is_operator=self.is_operator)


def for_session(payload, table_ref=None):
    """The write scope of a console cookie / CLI bearer session payload: the
    subject it names, with the operator allowlist (authz) deciding the
    reach.

    An operator agent is owner-scoped to its DTC subject even when the
    allowlist would otherwise treat every DTC account as an operator.
    """
    from . import actors

    payload = payload or {}
    if actors.is_agent(payload):
        return Visibility(actors.stable_subject(payload), is_operator=False)
    subject = payload.get("subject") or payload.get("sub")
    return Visibility(str(subject) if subject else "",
                      is_operator=authz.is_operator(payload))
