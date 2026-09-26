"""Provider consent: the signed-state OAuth start/callback flow."""
import hashlib
import hmac
import os
import time
import urllib.parse

import boto3

from .. import audit as audit_log
from .. import http
from ..auth import session
from ..auth.session import OAUTH_COOKIE
from ..auth.session import OAUTH_COOKIE
from . import records as connection_model
from . import credentials
from .providers import oauth_clients, oauth_providers


def _connection(connection_id):
    return connection_model.get_connection(
        boto3.resource("dynamodb").Table(os.environ["CONNECTIONS_TABLE"]),
        connection_id,
    )


def oauth_callback_url():
    """The single registered provider redirect URI. Never derived from headers."""
    url = os.environ.get("OAUTH_CALLBACK_URL", "").strip()
    return url

def _claim_oauth_state(jti):
    """Consume an OAuth state ID exactly once. Returns False on replay."""
    from botocore.exceptions import ClientError

    table = boto3.resource("dynamodb").Table(os.environ["EXECUTIONS_TABLE"])
    try:
        table.put_item(
            Item={
                "execution_id": f"oauth-state:{jti}",
                "status": "consumed",
                "expires_at": int(time.time()) + 3600,
            },
            ConditionExpression="attribute_not_exists(execution_id)",
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise
    return True

def oauth_start(event, connection_id):
    connection = _connection(connection_id)
    if not connection:
        return http._json_response(404, {"error": "Connection not found"})
    if connection["provider"] in connection_model.TOKEN_PROVIDERS:
        return http._json_response(
            400,
            {"error": "This provider connects with a directly provided token; "
                      "paste a new token in the connection form instead"},
        )
    redirect_uri = oauth_callback_url()
    if not redirect_uri:
        return http._json_response(503, {"error": "OAuth callback URL is not configured"})
    provider_name = connection["provider"]
    try:
        scopes = oauth_providers.normalize_scopes(provider_name, connection.get("scopes"))
    except oauth_providers.ProviderError as exc:
        return http._json_response(400, {"error": str(exc)})
    try:
        client_id, _ = oauth_clients.get(provider_name)
    except oauth_clients.ClientConfigError as exc:
        return http._json_response(503, {"error": str(exc)})
    verifier = session._b64encode(os.urandom(48))
    challenge = session._b64encode(hashlib.sha256(verifier.encode()).digest())
    state = session._sign({
        "kind": "oauth",
        "connection_id": connection_id,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
        "jti": session._b64encode(os.urandom(16)),
        "operator_subject": session._session_subject(event),
        "exp": int(time.time()) + 600,
    })
    location = oauth_providers.authorization_url(
        provider_name,
        client_id=client_id,
        redirect_uri=redirect_uri,
        scopes=scopes,
        state=state,
        code_challenge=challenge,
    )
    return http._redirect(
        location,
        cookies=[f"{OAUTH_COOKIE}={state}; Path=/oauth/callback; Max-Age=600; HttpOnly; Secure; SameSite=Lax"],
    )

def oauth_callback(event):
    query = event.get("queryStringParameters") or {}
    state = query.get("state", "")
    payload = session._verify(state, "oauth")
    if not payload:
        return http._json_response(400, {"error": "Invalid or expired OAuth state"})
    cookie_state = session._cookie(event, OAUTH_COOKIE)
    if cookie_state:
        if not hmac.compare_digest(state, cookie_state):
            return http._json_response(400, {"error": "Invalid or expired OAuth state"})
    elif not payload.get("operator_subject") or not payload.get("jti"):
        # Cookieless callbacks only for CLI-initiated flows, where the signed
        # state binds the operator subject and is single-use. Browser flows
        # always carry the state cookie set by oauth_start.
        return http._json_response(400, {"error": "Invalid or expired OAuth state"})
    if query.get("error"):
        return http._redirect(f"/connections?oauth={urllib.parse.quote(query['error'])}")
    if not payload.get("jti") or not _claim_oauth_state(payload["jti"]):
        return http._json_response(400, {"error": "Invalid or expired OAuth state"})
    session_subject = session._session_subject(event)
    operator = payload.get("operator_subject") or session_subject or "unknown"
    if session_subject and payload.get("operator_subject") != session_subject:
        session._audit_event(payload.get("connection_id", "unknown"), audit_log.CALLBACK,
                     session_subject, outcome="denied-wrong-operator")
        return http._json_response(400, {"error": "OAuth session does not match the connection request"})
    connection = _connection(payload["connection_id"])
    if not connection or not query.get("code"):
        return http._json_response(400, {"error": "OAuth connection or code is missing"})

    try:
        client_id, client_secret = oauth_clients.get(connection["provider"])
    except oauth_clients.ClientConfigError as exc:
        return http._json_response(503, {"error": str(exc)})
    try:
        previous = credentials.get_credential(connection["credential_id"])
    except KeyError:
        # No credential record yet: the first consent creates it.
        previous = {}
    try:
        token_data = oauth_providers.exchange_code(
            connection["provider"],
            code=query["code"],
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=payload["redirect_uri"],
            code_verifier=payload.get("code_verifier"),
        )
    except oauth_providers.ProviderError as exc:
        session._audit_event(connection["connection_id"], audit_log.CALLBACK, operator,
                     outcome="error", error=str(exc))
        return http._json_response(400, {"error": str(exc)})
    requested = set(connection.get("scopes") or [])
    granted_raw = token_data.get("scope")
    if granted_raw:
        granted = set(str(granted_raw).split())
        missing = sorted(requested - granted)
        if missing:
            session._audit_event(connection["connection_id"], audit_log.CALLBACK, operator,
                         outcome="error", error="missing scopes")
            return http._json_response(400, {
                "error": "Provider did not grant the requested scopes",
                "missing_scopes": missing,
            })
    else:
        granted = requested
    try:
        account_id, account_title = oauth_providers.verify_account(
            connection["provider"], token_data["access_token"],
        )
    except oauth_providers.ProviderError as exc:
        session._audit_event(connection["connection_id"], audit_log.CALLBACK, operator,
                     outcome="error", error=str(exc))
        return http._json_response(400, {"error": f"Could not verify the provider account: {exc}"})
    try:
        connection_model.check_binding(connection, account_id)
    except connection_model.BindingError as exc:
        session._audit_event(connection["connection_id"], audit_log.CALLBACK, operator,
                     outcome="denied-account-mismatch", error=str(exc))
        return http._json_response(409, {"error": str(exc)})
    stored = oauth_providers.normalize_token_data(
        token_data, previous_refresh_token=previous.get("refresh_token"),
    )
    credentials.put_credential(connection["credential_id"], stored, provider=connection["provider"])
    try:
        updated = connection_model.mark_connected(
            connection,
            verified_account_id=account_id,
            account_title=account_title,
            granted_scopes=sorted(granted),
            connected_by=operator,
        )
    except connection_model.BindingError as exc:
        session._audit_event(connection["connection_id"], audit_log.CALLBACK, operator,
                     outcome="error", error=str(exc))
        return http._json_response(409, {"error": str(exc)})
    boto3.resource("dynamodb").Table(os.environ["CONNECTIONS_TABLE"]).put_item(Item=updated)
    session._audit_event(connection["connection_id"], audit_log.CALLBACK, operator, outcome="ok")
    return http._redirect(
        "/connections?oauth=connected",
        cookies=[f"{OAUTH_COOKIE}=; Path=/oauth/callback; Max-Age=0; HttpOnly; Secure; SameSite=Lax"],
    )
