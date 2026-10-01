"""Implementations of the `dapier audit` commands."""

import os
from urllib.parse import urlencode

from .. import api

__all__ = ["audit_export", "audit_list", "print_audit"]


def _audit_params(connection=None, action=None, actor=None, agent=None,
                  outcome=None, since=None, before=None, query=None,
                  next_token=None, limit=None):
    params = {}
    if limit:
        params["limit"] = int(limit)
    for key, value in (("connection", connection), ("action", action),
                       ("actor", actor), ("agent", agent), ("outcome", outcome),
                       ("since", since), ("before", before), ("q", query),
                       ("next", next_token)):
        if value:
            params[key] = value
    return params


def audit_list(api_url, limit=50, connection=None, action=None, actor=None,
               agent=None, outcome=None, since=None, before=None, query=None,
               next_token=None, debug=False):
    params = _audit_params(connection=connection, action=action, actor=actor,
                           agent=agent, outcome=outcome, since=since,
                           before=before, query=query, next_token=next_token,
                           limit=limit)
    data = api.call(api_url, "GET", f"/api/agent/audit?{urlencode(params)}", debug=debug)
    events = data.get("events", [])
    if not events:
        filtered = any((connection, action, actor, agent, outcome, since,
                        before, query, next_token))
        print("No audit events match these filters." if filtered else
              "No audit events yet. Actions appear as operators change connections, workflows, and triggers.")
        return 0
    print_audit(events)
    next_page = (data.get("paging") or {}).get("next")
    if next_page:
        print(f"\nnext page: {next_page}  (pass it to --next)")
    return 0


def audit_export(api_url, out=None, max_rows=None, connection=None, action=None,
                 actor=None, agent=None, outcome=None, since=None, before=None,
                 query=None, debug=False):
    params = _audit_params(connection=connection, action=action, actor=actor,
                           agent=agent, outcome=outcome, since=since,
                           before=before, query=query)
    if max_rows:
        params["max_rows"] = int(max_rows)
    data = api.call(api_url, "GET", f"/api/agent/audit/export?{urlencode(params)}",
                    debug=debug)
    # The server suggests the filename; basename keeps a hostile suggestion
    # from writing outside the caller's directory.
    path = out or os.path.basename(data.get("filename") or "") or "dapier-audit.csv"
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(data.get("csv") or "")
    note = " (capped; narrow the filters for the rest)" if data.get("truncated") else ""
    print(f"Wrote {data.get('count', 0)} audit events to {path}{note}")
    return 0


def print_audit(items):
    print(f"{'TIMESTAMP':20} {'ACTION':12} {'ACTOR':34} {'AGENT':20} "
          f"{'OUTCOME':22} CONNECTION")
    for item in items:
        timestamp = (item.get("timestamp") or "-")[:19]
        error = item.get("error")
        suffix = f"  ({error})" if error else ""
        print(f"{timestamp:20} {item.get('action', ''):12} "
              f"{item.get('actor_subject', ''):34} {item.get('agent') or '-':20} "
              f"{item.get('outcome', ''):22} {item.get('connection_id', '')}{suffix}")


