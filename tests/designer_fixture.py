"""Synthetic designer API for browser tests and screenshots: one workflow
with a trigger, a branching condition (so a union join and branch edge
labels render) and steps on both branches."""
import json

SUMMARY = {"id": "invoice-triage", "enabled": True, "source": "invoice-triage.yaml",
           "connector": "email", "event": "message.received", "actionCount": 4,
           "published": True, "has_draft": False}

WORKFLOW = {
    "id": "invoice-triage",
    "name": "Invoice triage",
    "enabled": True,
    "trigger": {"connector": "email", "event": "message.received",
                "filters": {"route": {"equals": "invoice"}}},
    "actions": [
        {"id": "check-amount", "type": "condition",
         "when": {"trigger.subject": {"contains": "invoice"}},
         "then": [{"id": "notify-finance", "type": "slack", "channel": "#finance",
                   "text": "New invoice: {trigger.subject}"}],
         "else": [{"id": "file-it", "type": "dropbox_upload", "path": "/inbox"}]},
        {"id": "log-it", "type": "slack", "channel": "#ops", "text": "Handled"},
    ],
}


def payload(path):
    """The designer's /api/admin/designer/* answers, or None for other paths."""
    if path == "/api/admin/designer/workflows":
        return {"workflows": [SUMMARY]}
    if path.startswith("/api/admin/designer/workflows/"):
        return {"workflow": WORKFLOW, "name": WORKFLOW["name"], "name_source": "custom"}
    if path.startswith("/api/admin/designer/"):
        return {}
    if path == "/api/admin/me":
        return {"operator": True, "username": "grace@example.com", "email": "grace@example.com",
                "name": "Grace Hopper"}
    return None


def route(playwright_route):
    """page.route handler: answer the designer API, pass everything else on."""
    path = playwright_route.request.url.split("://", 1)[1].split("/", 1)[1]
    path = "/" + path.split("?", 1)[0]
    body = payload(path)
    if body is None:
        return playwright_route.continue_()
    return playwright_route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
