"""Zoom connection setup shared by the console and CLI APIs.

A Zoom connection is one of two kinds. An **API** connection is a regular
OAuth grant with scopes (consent through ``oauth_flow``); a **webhook**
connection holds an event signing secret and no scopes (``save``). The
stored record tells them apart by its scopes (``records.is_zoom_webhook``);
requests name the kind explicitly with ``kind``, and :func:`resolve_kind`
is the one rule both API surfaces apply.
"""

from . import credentials
from . import records

KIND_API = "api"
KIND_WEBHOOK = "webhook"
KINDS = (KIND_API, KIND_WEBHOOK)

# What a new Zoom API connection requests when the caller names no scopes:
# the identity scope consent verification needs (GET /users/me) plus the
# read scopes the Zoom plugin's find/list actions, discovery pickers and the
# recordings poll use. The OAuth callback rejects a grant missing any
# requested scope, so these stay within what the shared Zoom app grants;
# write and webinar scopes are opt-in (Manage → Advanced, or
# ``dapier connections edit --scopes``).
API_DEFAULT_SCOPES = (
    "cloud_recording:read:recording",
    "meeting:read:list_meetings",
    "meeting:read:meeting",
    "user:read:user",
)


class KindConflict(records.ConnectionError):
    """The request names the other Zoom kind than the stored connection."""


def resolve_kind(fields, kind=None, previous=None):
    """The Zoom connection kind a create/edit request means, or None for
    other providers. Fills a new API connection's default scopes in
    ``fields``; raises ConnectionError for contradictory requests.

    An explicit ``kind`` wins. Without one, requested scopes mean API, an
    existing Zoom record keeps its kind, and a new record is a webhook
    (what scope-less Zoom saves always created).
    """
    raw = "" if kind is None else str(kind).strip().lower()
    if fields["provider"] != "zoom":
        if raw:
            raise records.ConnectionError("kind applies only to Zoom connections")
        return None
    if raw and raw not in KINDS:
        raise records.ConnectionError("Zoom connection kind must be api or webhook")
    previous = previous if previous and previous.get("provider") == "zoom" else None
    previous_kind = None
    if previous:
        previous_kind = KIND_WEBHOOK if records.is_zoom_webhook(previous) else KIND_API
    resolved = raw or (KIND_API if fields["scopes"] else previous_kind or KIND_WEBHOOK)
    if previous_kind and resolved != previous_kind:
        raise KindConflict(
            f"This is a Zoom {'webhook' if previous_kind == KIND_WEBHOOK else 'API'} "
            "connection; add a separate connection for the other kind")
    if resolved == KIND_WEBHOOK:
        if fields["scopes"]:
            raise records.ConnectionError("Zoom webhook connections carry no OAuth scopes")
    elif not fields["scopes"]:
        if previous:
            raise records.ConnectionError("A Zoom API connection needs at least one scope")
        fields["scopes"] = list(API_DEFAULT_SCOPES)
    return resolved


def save(body, *, operator_subject, connections_table):
    """Store a write-only Zoom webhook signing token and connection metadata.

    Zoom cannot verify this token via an API call. The connection stays ready
    until Zoom signs its first endpoint-validation challenge or recording event.
    """
    try:
        fields = records.validate_new_connection(body)
    except records.ConnectionError as exc:
        return 400, {"error": str(exc)}
    if fields["provider"] != "zoom":
        return 400, {"error": "This setup is for Zoom only"}
    connection_id = fields["connection_id"]
    previous = records.get_connection(connections_table, connection_id)
    if previous and previous.get("provider") != "zoom":
        return 409, {"error": "Connection belongs to another provider"}
    supplied = body.get("token")
    if supplied is not None and not isinstance(supplied, str):
        return 400, {"error": "Zoom secret token must be text"}
    try:
        previous_secret = credentials.get_credential(
            records.credential_id_for(connection_id)).get("webhook_secret", "") if previous else ""
    except KeyError:
        previous_secret = ""
    token = (supplied or "").strip() or previous_secret
    if not token:
        return 400, {"error": "Paste the Zoom webhook Secret Token"}
    if not 16 <= len(token) <= 256:
        return 400, {"error": "Zoom webhook Secret Token must be 16–256 characters"}
    try:
        item = records.build_item(fields, owner_subject=operator_subject, previous=previous)
    except records.ConnectionError as exc:
        return 409, {"error": str(exc)}
    if not previous or token != previous_secret:
        item["status"] = records.STATUS_READY
    credentials.put_credential(item["credential_id"], {"webhook_secret": token}, provider="zoom")
    records.put_connection(connections_table, item)
    return 200, records.public_view(item)
