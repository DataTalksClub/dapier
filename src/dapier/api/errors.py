"""Error visibility: failed-run counts grouped by workflow over a window.

Reuses the runs module's grouped, bounded scans (api/runs.py) instead of a
second walk over the executions ledger — the same ``problems`` alias that
covers every failed-run shape (``failed`` and ``error``) on the runs list.
"""
from datetime import datetime, timedelta, timezone

DEFAULT_DAYS = 7
MAX_DAYS = 90


def api_summary(days=DEFAULT_DAYS, now=None):
    """Failed-run counts by workflow for the last ``days`` days.

    The window is the runs list's own filter (newest-first scan, bounded
    like every list call), so counts cover the scanned window rather than
    the whole ledger — the same trade the runs list makes.
    """
    from . import runs

    try:
        days = max(1, min(int(days), MAX_DAYS))
    except (TypeError, ValueError):
        days = DEFAULT_DAYS
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).isoformat()
    failed = runs.recent(runs.MAX_LIMIT, status="problems", since=since)
    counts = {}
    for run in failed:
        workflow_id = run.get("workflow_id") or "unknown"
        entry = counts.setdefault(workflow_id, {
            "workflow_id": workflow_id,
            "failed_runs": 0,
            "last_failed_at": None,
            "last_error": None,
        })
        entry["failed_runs"] += 1
        started = run.get("started_at") or ""
        if started >= (entry["last_failed_at"] or ""):
            entry["last_failed_at"] = started or None
            entry["last_error"] = run.get("error")
    workflows = sorted(
        counts.values(),
        key=lambda row: (-row["failed_runs"], row["workflow_id"]),
    )
    return 200, {
        "window_days": days,
        "since": since,
        "total_failed_runs": sum(row["failed_runs"] for row in workflows),
        "workflows": workflows,
    }
