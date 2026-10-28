"""store_checks: the legacy-shaped-row counts behind /api/agent/store-check."""
from src.dapier import store_checks


class Table:
    def __init__(self, items):
        self.items = items

    def scan(self, **kwargs):
        return {"Items": list(self.items)}


def test_legacy_row_counts_each_shape():
    counts = store_checks.legacy_row_counts(
        executions=Table([
            {"execution_id": "wf:a:e", "run_id": "wf:e", "status": "completed",
             "lease_until": 10},
            # pre-run-grouping row: no run_id
            {"execution_id": "wf:b:e", "status": "completed"},
            # live processing step holds its lease
            {"execution_id": "wf:c:e", "run_id": "wf:e", "status": "processing",
             "lease_until": 10},
            # processing step a worker died in before leases existed
            {"execution_id": "wf:d:e", "run_id": "wf:e", "status": "processing"},
        ]),
        published=Table([
            {"workflow_id": "one", "owner": "subject-1"},
            {"workflow_id": "two", "owner": ""},
            {"workflow_id": "v:one", "version_of": "one", "owner": "subject-1"},
            {"workflow_id": "v:two", "version_of": "two"},
            {"workflow_id": "d:two", "draft_of": "two", "owner": "subject-1"},
        ]),
        connections=Table([
            {"connection_id": "a"},
            {"connection_id": "b", "display_name": "Legacy"},
            {"connection_id": "c", "display_name": "   "},
        ]),
        polls=Table([
            {"poll_id": "news"},
            {"poll_id": "old", "flow": "old-flow"},
        ]),
    )

    assert counts == {
        "executions_without_run_id": 1,
        "executions_processing_without_lease": 1,
        "published_live_without_owner": 1,
        "published_versions_without_owner": 1,
        "published_drafts_without_owner": 0,
        "connections_with_display_name": 1,
        "polls_with_flow": 1,
    }


def test_all_zero_means_every_fallback_is_dead():
    counts = store_checks.legacy_row_counts(
        executions=Table([
            {"execution_id": "wf:a:e", "run_id": "wf:e", "status": "completed",
             "lease_until": 10},
        ]),
        published=Table([{"workflow_id": "one", "owner": "subject-1"}]),
        connections=Table([{"connection_id": "a"}]),
        polls=Table([{"poll_id": "news"}]),
    )

    assert set(counts.values()) == {0}
