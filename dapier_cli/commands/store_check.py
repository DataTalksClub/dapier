"""Implementation of the `dapier store-check` command."""

from .. import api

__all__ = ["store_check"]

LABELS = (
    ("executions_without_run_id", "execution rows without run_id"),
    ("executions_processing_without_lease", "processing steps without a lease"),
    ("published_live_without_owner", "published workflows without owner"),
    ("published_versions_without_owner", "published versions without owner"),
    ("published_drafts_without_owner", "published drafts without owner"),
    ("connections_with_display_name", "connections carrying display_name"),
    ("polls_with_flow", "poll rows bound by flow"),
)


def store_check(api_url, debug=False):
    """The legacy-shaped stored rows the compat fallbacks serve (operator)."""
    data = api.call(api_url, "GET", "/api/agent/store-check", debug=debug)
    width = max(len(label) for _, label in LABELS)
    for key, label in LABELS:
        print(f"{label:{width}}  {data.get(key, 0)}")
    return 0
