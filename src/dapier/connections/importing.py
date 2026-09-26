"""Operator imports and pasted-token verification for provider connections."""
from .. import audit
from . import credentials, zoom
from . import records as connections
from .providers import oauth_clients, oauth_providers, slack_tokens, telegram_api


def verify_token_provider(provider, token):
    """Verify a pasted token against its provider. Returns ``(account_id, title)``.

    Slack workspaces verify via ``auth.test``; Telegram bots via ``getMe``.
    Both fail closed before anything is stored.
    """
    from .providers import telegram_api

    if provider == "slack":
        token = slack_tokens.validate_token(token)
        return slack_tokens.verify_account(token)
    token = telegram_api.validate_token(token)
    return telegram_api.get_me(token)

def _import_token_connection(body, *, operator_subject, connections_table):
    """Operator import for pasted-token providers (Slack, Telegram).

    Mirrors the console's token-connection path over the CLI's bearer
    authentication: the token is verified against its provider before it is
    stored, so an imported connection always carries a checked identity.
    """
    from .providers import slack_tokens, telegram_api
    try:
        fields = connections.validate_new_connection(body)
    except connections.ConnectionError as exc:
        return 400, {"error": str(exc)}
    token = str(body.get("token") or "").strip()
    if not token:
        return 400, {"error": "This provider imports with a token, not an authorized-user file"}
    try:
        account_id, account_title = verify_token_provider(fields["provider"], token)
    except (slack_tokens.SlackTokenError, telegram_api.TelegramApiError) as exc:
        audit.emit(fields["connection_id"], audit.IMPORT, operator_subject,
                   outcome="error", error=str(exc))
        return 400, {"error": str(exc)}
    previous = connections.get_connection(connections_table, fields["connection_id"])
    try:
        item = connections.build_item(fields, owner_subject=operator_subject, previous=previous)
        connections.check_binding(item, account_id)
        item = connections.mark_connected(
            item, verified_account_id=account_id, account_title=account_title,
            granted_scopes=fields["scopes"], connected_by=operator_subject,
        )
    except connections.ConnectionError as exc:
        audit.emit(fields["connection_id"], audit.IMPORT, operator_subject,
                   outcome="denied-account-mismatch", error=str(exc))
        status = 409 if isinstance(exc, connections.BindingError) else 400
        return status, {"error": str(exc)}
    credentials.put_credential(item["credential_id"], {"token": token}, provider=item["provider"])
    connections.put_connection(connections_table, item)
    audit.emit(item["connection_id"], audit.IMPORT, operator_subject, outcome="ok")
    return 200, connections.public_view(item)

def import_core(body, *, operator_subject, connections_table):
    """Shared operator import. Returns ``(status_code, payload)``.

    Token providers (Slack, Telegram) import a verified pasted token; OAuth
    providers transfer the supplied refresh credential without logging it,
    verify refresh + provider account before storing, and bind the
    connection. Existing backups are never touched.
    """
    if str(body.get("provider", "")).strip().lower() == "zoom":
        status, payload = zoom.save(body, operator_subject=operator_subject,
                                    connections_table=connections_table)
        audit.emit(str(body.get("connection_id", "zoom")), audit.IMPORT, operator_subject,
                   outcome="ok" if status == 200 else "error")
        return status, payload
    if str(body.get("provider", "")).strip().lower() in connections.TOKEN_PROVIDERS:
        return _import_token_connection(
            body, operator_subject=operator_subject, connections_table=connections_table)
    if not isinstance(body.get("authorized_user"), dict):
        return 400, {"error": "An authorized-user credential object is required"}
    try:
        fields = connections.validate_new_connection(body)
    except connections.ConnectionError as exc:
        return 400, {"error": str(exc)}
    refresh_token = body["authorized_user"].get("refresh_token")
    if not refresh_token:
        return 400, {"error": "The authorized-user object has no refresh token"}

    try:
        client_id, client_secret = oauth_clients.get(
            fields["provider"],
            client_id=fields.get("client_id"),
            client_secret=fields.get("client_secret"),
        )
    except oauth_clients.ClientConfigError as exc:
        return 400, {"error": str(exc)}

    try:
        token_data = oauth_providers.refresh_access_token(
            fields["provider"],
            refresh_token=refresh_token,
            client_id=client_id,
            client_secret=client_secret,
        )
    except oauth_providers.ProviderError as exc:
        audit.emit(fields["connection_id"], audit.IMPORT, operator_subject,
                   outcome="error", error=str(exc))
        return 400, {"error": f"Refresh check failed: {exc}"}
    try:
        account_id, account_title = oauth_providers.verify_account(
            fields["provider"], token_data["access_token"],
        )
    except oauth_providers.ProviderError as exc:
        audit.emit(fields["connection_id"], audit.IMPORT, operator_subject,
                   outcome="error", error=str(exc))
        return 400, {"error": f"Could not verify the provider account: {exc}"}

    previous = connections.get_connection(connections_table, fields["connection_id"])
    try:
        item = connections.build_item(fields, owner_subject=operator_subject, previous=previous)
        connections.check_binding(item, account_id)
        item = connections.mark_connected(
            item, verified_account_id=account_id, account_title=account_title,
            granted_scopes=fields["scopes"], connected_by=operator_subject,
        )
    except connections.ConnectionError as exc:
        audit.emit(fields["connection_id"], audit.IMPORT, operator_subject,
                   outcome="denied-account-mismatch", error=str(exc))
        status = 409 if isinstance(exc, connections.BindingError) else 400
        return status, {"error": str(exc)}
    # Refresh tokens issued by a client other than the shared one stay bound
    # to it: keep the explicit client credentials with the record so refresh
    # continues to work (tokens._client_override).
    stored = {
        key: fields[key] for key in ("client_id", "client_secret") if fields.get(key)
    }
    stored.update(oauth_providers.normalize_token_data(
        token_data, previous_refresh_token=refresh_token,
    ))
    credentials.put_credential(item["credential_id"], stored, provider=item["provider"])
    connections.put_connection(connections_table, item)
    audit.emit(item["connection_id"], audit.IMPORT, operator_subject, outcome="ok")
    return 200, connections.public_view(item)
