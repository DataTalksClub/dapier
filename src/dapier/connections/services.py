"""Which product a connection's scopes actually grant.

A single Google OAuth connection can back Gmail, Calendar, Drive, Docs
and Sheets at once (YouTube may ride the same Google client). Surfaces
group by those services so Calendar is distinct from Drive even when they
share one refresh token. Standalone providers (Dropbox, Slack, Telegram,
Zoom) are one service each.
"""

USERINFO_EMAIL = "https://www.googleapis.com/auth/userinfo.email"

# Display order is the Connections register order. Google products first,
# then YouTube, then the standalone providers.
CATALOG = (
    {
        "id": "gmail",
        "label": "Gmail",
        "provider": "google",
        "connection_id": "gmail",
        "markers": ("/auth/gmail.",),
        "default_scopes": (
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.send",
            USERINFO_EMAIL,
        ),
    },
    {
        "id": "calendar",
        "label": "Google Calendar",
        "provider": "google",
        "connection_id": "google-calendar",
        "markers": ("/auth/calendar",),
        "default_scopes": (
            "https://www.googleapis.com/auth/calendar.freebusy",
            "https://www.googleapis.com/auth/calendar.events.owned",
            "https://www.googleapis.com/auth/calendar.readonly",
            USERINFO_EMAIL,
        ),
    },
    {
        "id": "drive",
        "label": "Google Drive",
        "provider": "google",
        "connection_id": "google-drive",
        "markers": ("/auth/drive",),
        "default_scopes": (
            "https://www.googleapis.com/auth/drive.readonly",
            USERINFO_EMAIL,
        ),
    },
    {
        "id": "docs",
        "label": "Google Docs",
        "provider": "google",
        "connection_id": "google-docs",
        "markers": ("/auth/documents",),
        "default_scopes": (
            "https://www.googleapis.com/auth/documents",
            USERINFO_EMAIL,
        ),
    },
    {
        "id": "sheets",
        "label": "Google Sheets",
        "provider": "google",
        "connection_id": "google-sheets",
        "markers": ("/auth/spreadsheets",),
        "default_scopes": (
            "https://www.googleapis.com/auth/spreadsheets",
            USERINFO_EMAIL,
        ),
    },
    {
        "id": "youtube",
        "label": "YouTube",
        "provider": "youtube",
        "connection_id": "youtube",
        "markers": ("/auth/youtube", "youtube.force-ssl", "yt-analytics"),
        "default_scopes": (
            "https://www.googleapis.com/auth/youtube.readonly",
        ),
    },
    {
        "id": "dropbox",
        "label": "Dropbox",
        "provider": "dropbox",
        "connection_id": "dropbox",
        "standalone": True,
        "default_scopes": (
            "account_info.read",
            "files.metadata.read",
            "files.content.read",
            "files.content.write",
        ),
    },
    {
        "id": "slack",
        "label": "Slack",
        "provider": "slack",
        "connection_id": "slack",
        "standalone": True,
        "default_scopes": (),
    },
    {
        "id": "telegram",
        "label": "Telegram",
        "provider": "telegram",
        "connection_id": "telegram-bot",
        "standalone": True,
        "default_scopes": (),
    },
    {
        "id": "zoom",
        "label": "Zoom",
        "provider": "zoom",
        "connection_id": "zoom",
        "standalone": True,
        "default_scopes": (),
    },
)

_BY_ID = {spec["id"]: spec for spec in CATALOG}
_STANDALONE_IDS = {spec["id"] for spec in CATALOG if spec.get("standalone")}


def catalog_entry(service_id):
    """Return the catalog spec for ``service_id``, or None."""
    return _BY_ID.get(service_id)


def _scopes_of(connection):
    granted = [str(scope) for scope in (connection.get("granted_scopes") or []) if scope]
    if granted:
        return granted
    return [str(scope) for scope in (connection.get("scopes") or []) if scope]


def _scopes_hit(scopes, markers):
    return any(marker in scope for scope in scopes for marker in markers)


def _entry(spec):
    return {"id": spec["id"], "label": spec["label"]}


def services_for(connection):
    """The services this connection grants, in catalog order.

    Google-family connections contribute every catalog product their
    granted (else requested) scopes hit. A YouTube *provider* connection
    always includes YouTube, even when the stored scope list is odd. A
    Google connection with no matching product scopes falls back to a
    single "Google" service so the row still has a home.
    """
    provider = str(connection.get("provider") or "").strip()
    scopes = _scopes_of(connection)
    found = []
    seen = set()
    for spec in CATALOG:
        service_id = spec["id"]
        if spec.get("standalone"):
            if provider == spec["provider"] and service_id not in seen:
                found.append(_entry(spec))
                seen.add(service_id)
            continue
        if service_id == "youtube" and provider == "youtube" and service_id not in seen:
            found.append(_entry(spec))
            seen.add(service_id)
            continue
        if provider in ("google", "youtube") and _scopes_hit(scopes, spec["markers"]):
            if service_id not in seen:
                found.append(_entry(spec))
                seen.add(service_id)
    if found:
        return found
    if provider == "google":
        return [{"id": "google", "label": "Google"}]
    label = (_BY_ID.get(provider) or {}).get("label") or provider or "connection"
    return [{"id": provider or "unknown", "label": label}]
