"""Webhook-only Zoom connection setup shared by console and CLI APIs."""

from . import credentials
from . import records


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
