"""Operator agents: a least-privilege account, and the read-only owner filter.

``orin@datatalks.club`` is the operator agent. The role can work with
workflows, runs, and agent tasks, and only with workflows it owns. It
cannot administer credentials, grants, tokens, OAuth clients, or other
accounts. No API token is issued for it. Orin signs in later through the
normal console or device-login flow; the first sign-in records the DTC
subject next to the email so ``--owner orin`` matches work stamped with
that subject.

A full operator (the allowlist, or any other DTC account while the
allowlist is empty) is unchanged and can narrow a list to one agent's
work. That narrowing is a read filter. It does not change who a write
runs as.
"""

import os

from . import authz

ROLE_AGENT = "agent"
ROLE_OPERATOR = "operator"

# Actions an agent may pass on the CLI bearer gate. Workflow writes are
# still owner-scoped by the G17 write gate. Anything not listed, and not
# a workflow.* action, stays operator-only (credentials, grants, tokens,
# quota, audit, sender policy, storage).
AGENT_ACTIONS = frozenset({
    "overview",
    "runs", "runs.export", "runs.replay", "runs.cancel", "runs.resolve",
    "runs.replay-failed",
    "agent-tasks",
    "workers",
    "triggers.sample",
    "connections.list", "connections.discover",
    "catalog",
    # Typefully agent proxy (write-only credential stays server-side).
    "typefully.social_sets", "typefully.drafts", "typefully.draft",
    "typefully.queue", "typefully.create_draft", "typefully.schedule_draft",
    "typefully.patch_draft", "typefully.upload_media", "typefully.media_status",
})

# The directory is the user record. Email is the stable key until the
# DTC subject is known; alias is what `--owner orin` and the console
# "View as" control use.
DIRECTORY = (
    {
        "alias": "orin",
        "email": "orin@datatalks.club",
        "role": ROLE_AGENT,
        "display_name": "Orin",
    },
)


def _fold(value):
    return str(value or "").strip().casefold()


def match_identity(value):
    """The directory row named by an alias, email, display name, or subject."""
    folded = _fold(value)
    if not folded:
        return None
    for actor in DIRECTORY:
        keys = (actor["alias"], actor["email"], actor["display_name"],
                actor.get("subject") or "")
        if folded in {_fold(key) for key in keys if key}:
            return actor
    for actor in DIRECTORY:
        if folded in bound_subjects(actor):
            return actor
    return None


def match_session(payload):
    """The directory row for a console cookie or CLI claims payload."""
    payload = payload or {}
    for key in ("email", "sub", "subject"):
        actor = match_identity(payload.get(key))
        if actor:
            return actor
    return None


def is_agent(payload):
    actor = match_session(payload)
    return bool(actor) and actor["role"] == ROLE_AGENT


def stable_subject(payload):
    """The DTC subject a session acts as. An email is not a subject."""
    payload = payload or {}
    for key in ("subject", "sub"):
        value = str(payload.get(key) or "").strip()
        if value and "@" not in value:
            return value
    return str(payload.get("subject") or payload.get("sub") or "")


def gate(payload):
    """``agent``, ``operator``, or None. A directory row narrows an
    allowlisted operator; with no row the allowlist decides as before."""
    if is_agent(payload):
        return ROLE_AGENT
    if authz.is_operator(payload):
        return ROLE_OPERATOR
    return None


def action_allowed(payload, action):
    """Whether this session may call a bearer-gated agent-API action."""
    kind = gate(payload)
    if kind == ROLE_OPERATOR:
        return True
    if kind != ROLE_AGENT:
        return False
    action = str(action or "")
    return action in AGENT_ACTIONS or action.startswith("workflow.")


def console_allowed(method, path):
    """Whether an agent may call this console route.

    Designer routes stay open and the owner write gate scopes them.
    Credentials, grants, tokens, quota, audit, and connection mutations
    stay closed.
    """
    method = str(method or "").upper()
    path = str(path or "")
    if path.startswith("/api/admin/designer/"):
        return True
    if method == "GET" and path in {
        "/api/admin/overview",
        "/api/admin/runs",
        "/api/admin/runs/export",
        "/api/admin/agent-tasks",
        "/api/admin/workers",
        "/api/admin/connections",
        "/api/admin/email-triggers",
        "/api/admin/triggers/sample",
        "/api/admin/triggers/inbox",
    }:
        return True
    if method == "GET" and path.startswith("/api/admin/runs/"):
        return True
    if method == "GET" and path.startswith("/api/admin/triggers/inbox/"):
        return True
    if method == "GET" and path.startswith("/api/admin/connections/") and "/discover" in path:
        return True
    if method == "POST" and path == "/api/admin/runs/replay-failed":
        return True
    if method == "POST" and path.startswith("/api/admin/runs/") and path.rsplit("/", 1)[-1] in {
        "replay", "cancel", "resolve",
    }:
        return True
    return False


def _bindings_table(table_ref):
    if table_ref is not None:
        return table_ref
    name = os.environ.get("DEVICE_SESSIONS_TABLE", "")
    if not name:
        return None
    try:
        import boto3

        return boto3.resource("dynamodb").Table(name)
    except Exception:  # noqa: BLE001 — binding must not break sign-in
        return None


def _binding_key(email):
    return f"actor#{_fold(email)}"


def remember(email, subject, table_ref=None):
    """Record the DTC subject for a directory email. Failures are ignored."""
    actor = match_identity(email)
    subject = str(subject or "").strip()
    if not actor or not subject or "@" in subject:
        return
    table = _bindings_table(table_ref)
    if table is None:
        return
    try:
        table.put_item(Item={
            "pk": _binding_key(actor["email"]),
            "kind": "actor",
            "email": actor["email"],
            "alias": actor["alias"],
            "subject": subject,
            "role": actor["role"],
        })
    except Exception:  # noqa: BLE001
        return


def bound_subjects(actor, table_ref=None):
    """Subjects recorded for this actor, lower-cased for comparison."""
    table = _bindings_table(table_ref)
    if table is None:
        return set()
    try:
        item = table.get_item(Key={"pk": _binding_key(actor["email"])}).get("Item") or {}
    except Exception:  # noqa: BLE001
        return set()
    subject = _fold(item.get("subject"))
    return {subject} if subject else set()


def identities(actor, table_ref=None):
    """Every string that should match this actor's rows."""
    keys = {_fold(actor.get("alias")), _fold(actor.get("email")),
            _fold(actor.get("display_name")), _fold(actor.get("subject"))}
    keys |= bound_subjects(actor, table_ref=table_ref)
    keys.discard("")
    return keys


def resolve_filter(owner=None, as_id=None, assignee=None, table_ref=None):
    """The match set for a list filter, or ``(None, error)``.

    ``None`` with no error means the caller did not ask to filter.
    ``--owner`` must name the directory. ``--as`` and ``--assignee``
    accept a directory identity or a raw subject id.
    """
    specs = []
    for kind, value in (("owner", owner), ("as", as_id), ("assignee", assignee)):
        text = str(value or "").strip()
        if text:
            specs.append((kind, text))
    if not specs:
        return None, None
    sets = []
    for kind, value in specs:
        actor = match_identity(value)
        if actor:
            sets.append(identities(actor, table_ref=table_ref))
            continue
        if kind == "owner":
            return None, f"Unknown owner {value}"
        sets.append({_fold(value)})
    match = set.intersection(*sets)
    if not match:
        return None, "Owner filters do not name the same person"
    return match, None


def row_visible(match, *, workflow_owner="", fields=()):
    """Whether one row survives an owner filter. ``match is None`` keeps it."""
    if match is None:
        return True
    values = {_fold(workflow_owner)}
    values.update(_fold(value) for value in fields)
    values.discard("")
    return bool(values & set(match))


def public_directory(table_ref=None):
    """What a full operator's "View as" control lists."""
    people = []
    for actor in DIRECTORY:
        people.append({
            "alias": actor["alias"],
            "email": actor["email"],
            "label": actor["display_name"],
            "role": actor["role"],
            "identities": sorted(identities(actor, table_ref=table_ref)),
        })
    return people
