"""The visibility gates the console wrappers share: read-side
denials raise 403 through the http layer; save-side checks merge the
payload's connection ids into the check."""
from .. import designer_store
from ... import http
from ...auth import session


def _write_denied(visible, workflow_id, action, operator):
    """G17 Phase 3: the owner-or-operator write gate for the workflow-scoped
    console routes — a 403 response when ``visible`` (the dispatcher's
    session scope) targets a workflow it does not own, else ``None``.
    ``visible`` is None for unrestricted callers (operator sessions resolve
    to an unrestricted scope; legacy direct calls pass nothing);
    visibility.ensure_can_write holds the rule, and denials are audited
    like the role gates'."""
    if visible is None:
        return None
    denied = visible.can_write(workflow_id)
    if denied is None:
        return None
    session._audit_event(str(workflow_id or "unknown"), action,
                         operator or "unknown", outcome="denied-not-owner")
    return http._json_response(*denied)


def _save_denied(visible, body, operator):
    """The save gate over every id a save body touches (the definition's id
    plus a renameFrom target)."""
    for workflow_id in designer_store.save_gate_ids(body):
        denied = _write_denied(visible, workflow_id, "workflow.save", operator)
        if denied is not None:
            return denied
    return None


