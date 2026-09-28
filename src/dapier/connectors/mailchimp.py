"""Mailchimp connector: audience/member discovery, the stored-API-key health
check, and the find/add-update member actions — all behind the stored
Mailchimp credential (Marketing API v3, basic auth with the key)."""

from ..engine.actions import mailchimp
from ..engine.actions.mailchimp import (
    DEFAULT_CREDENTIAL_ID,
    run_mailchimp_find_member,
    run_mailchimp_upsert_member,
)
from .registry import (
    Action,
    ConnectionTest,
    Discovery,
    register,
    register_connection_test,
    register_discovery,
)

register(Action(
    type="mailchimp_find_member",
    label="Mailchimp: find member",
    icon="mail",
    description="Find one audience member by email (Find Member). Output: "
                "{found: true, member: {email, status, merge_fields, ...}}; "
                "a miss is {found: false, member: null}.",
    run=lambda action, event, workflow_id, steps=None: run_mailchimp_find_member(
        action, event, steps=steps),
    required=frozenset({"list_id", "email"}),
    optional=frozenset({"credential_id", "connection_id"}),
    fields=(
        {"key": "credential_id", "label": "Credential ID", "placeholder": "mailchimp (default)"},
        {"key": "list_id", "label": "Audience", "required": True,
         "discover": {"resource": "mailchimp.audiences"}},
        {"key": "email", "label": "Email", "type": "email", "required": True,
         "placeholder": "{sender}",
         "discover": {"resource": "mailchimp.members", "params": {"list_id": "list_id"}}},
    ),
))

register(Action(
    type="mailchimp_upsert_member",
    label="Mailchimp",
    icon="mail",
    description="Add or update one audience member (Add/Update Member): the "
                "email's record is created with Status when missing, or "
                "updated when it exists. Merge fields is a JSON object.",
    run=lambda action, event, workflow_id, steps=None: run_mailchimp_upsert_member(
        action, event, steps=steps),
    required=frozenset({"list_id", "email"}),
    optional=frozenset({"status", "merge_fields", "credential_id", "connection_id"}),
    fields=(
        {"key": "credential_id", "label": "Credential ID", "placeholder": "mailchimp (default)"},
        {"key": "list_id", "label": "Audience", "required": True,
         "discover": {"resource": "mailchimp.audiences"}},
        {"key": "email", "label": "Email", "type": "email", "required": True,
         "placeholder": "{sender}",
         "discover": {"resource": "mailchimp.members", "params": {"list_id": "list_id"}}},
        {"key": "status", "label": "Status if new", "type": "select",
         "options": ["subscribed", "pending", "unsubscribed", "cleaned"],
         "default": "subscribed"},
        {"key": "merge_fields", "label": "Merge fields (JSON)", "type": "textarea",
         "placeholder": '{"FNAME": "{name}"}'},
    ),
))


def _stored_settings(connection):
    """The (api_key, server) pair behind the connection, falling back to the
    shared ``mailchimp`` credential — mirroring the s3 key resolution."""
    from ..connections import credentials

    for credential_id in (connection.get("credential_id"), DEFAULT_CREDENTIAL_ID):
        if not credential_id:
            continue
        try:
            secret = credentials.get_credential(credential_id)
        except KeyError:
            continue
        api_key = str(secret.get("apiKey") or secret.get("api_key") or "").strip()
        server = str(secret.get("server") or "").strip()
        if api_key and not server and "-" in api_key:
            server = api_key.rsplit("-", 1)[-1]
        if api_key and server:
            return api_key, server
    raise RuntimeError("no stored Mailchimp API key for this connection")


def _run_audiences(connection, params, *, transport=None):
    api_key, server = _stored_settings(connection)
    status, data = mailchimp.mailchimp_request(
        "GET", f"https://{server}.api.mailchimp.com/3.0/lists?count=100",
        api_key, transport=transport)
    if status >= 300:
        raise RuntimeError(f"Mailchimp lists returned HTTP {status}")
    return [
        {"id": entry.get("id"), "name": entry.get("name"),
         "members": (entry.get("stats") or {}).get("member_count")}
        for entry in data.get("lists") or []
        if entry.get("id")
    ]


register_discovery(Discovery(
    name="audiences",
    connector="mailchimp",
    label="Audiences",
    description="Mailchimp audiences (lists) on the account, with member counts",
    run=_run_audiences,
))


def _run_members(connection, params, *, transport=None):
    api_key, server = _stored_settings(connection)
    list_id = str(params.get("list_id") or "").strip()
    status, data = mailchimp.mailchimp_request(
        "GET",
        f"https://{server}.api.mailchimp.com/3.0/lists/{list_id}/members?count=100",
        api_key, transport=transport)
    if status >= 300:
        raise RuntimeError(f"Mailchimp members returned HTTP {status}")
    return [
        {"id": entry.get("email_address") or entry.get("id"),
         "name": entry.get("email_address"),
         "email": entry.get("email_address"),
         "status": entry.get("status")}
        for entry in data.get("members") or []
        if entry.get("email_address") or entry.get("id")
    ]


register_discovery(Discovery(
    name="members",
    connector="mailchimp",
    label="Audience members",
    description="Members of one audience, up to 100",
    params=(
        {"key": "list_id", "label": "Audience", "type": "text", "required": True,
         "help": "Audience ID from the audiences list"},
    ),
    run=_run_members,
))


def _run_test(connection):
    """Ping the Marketing API with the stored key (never raises)."""
    try:
        api_key, server = _stored_settings(connection)
        status, data = mailchimp.mailchimp_request(
            "GET", f"https://{server}.api.mailchimp.com/3.0/ping", api_key)
    except Exception as exc:
        return {"ok": False, "detail": f"Mailchimp key check failed: {exc}"}
    if status >= 300:
        detail = ""
        if isinstance(data, dict):
            detail = str(data.get("detail") or data.get("title") or "")
        return {"ok": False,
                "detail": f"Mailchimp ping failed: {detail or f'HTTP {status}'}"}
    return {
        "ok": True,
        "detail": f"Mailchimp API key verified (datacenter {server})",
        "identity": {"server": server},
    }


register_connection_test(ConnectionTest(connector="mailchimp", run=_run_test))
