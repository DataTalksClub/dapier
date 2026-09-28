"""Who already owns a local-part on the shared trigger domain.

Email triggers, YAML email routes, and the agent mailbox share one SES
catch-all. Each save asks here so the two stores cannot claim the same name.
"""

import os


def claim_error(name, caller):
    """Why ``name`` cannot be claimed by ``caller``, or None when it is free.

    ``caller`` is ``email-trigger`` or ``agent-mailbox``: a store does not
    conflict with its own row.
    """
    name = str(name or "").strip().lower()
    if not name:
        return "a local-part is required"
    from .email_triggers import yaml_email_routes

    if name in yaml_email_routes():
        return f"the route '{name}' is already handled by a YAML workflow"
    if caller != "email-trigger" and os.environ.get("EMAIL_TRIGGERS_TABLE"):
        from .email_triggers import get_table

        if get_table().get_item(Key={"name": name}).get("Item"):
            return f"the route '{name}' is already an email trigger"
    if caller != "agent-mailbox" and os.environ.get("AGENT_MAILBOXES_TABLE"):
        from .agent_mailboxes import peek_mailbox

        if peek_mailbox(name):
            return f"the route '{name}' is the agent mailbox"
    return None
