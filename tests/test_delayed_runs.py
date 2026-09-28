"""runs.delayed_runs — the delete gate: which runs of a workflow are still
parked on a delay, deduped per run, with the wake-up moment each parked step
recorded."""

import boto3
import pytest

from src.dapier.api import runs


class PagedExecTable:
    """A scan that honors the FilterExpression and pages like DynamoDB.

    boto3 hands the FilterExpression over as a condition object (the wire
    serialization happens deeper in botocore), so the ``=`` leaves are read
    here: every attribute named in a leaf must be present with the compared
    value on the item — AND semantics, like the real filter.
    """

    def __init__(self, items, page_size=2):
        self.items = items
        self.page_size = page_size

    @staticmethod
    def _equalities(node):
        """(attribute name, compared value) pairs of every = leaf, recursing
        through combinators (And). A leaf's values are (Attr, constant)."""
        pairs = []
        parts = node.get_expression()["values"]
        if parts and hasattr(parts[0], "name"):
            return [(parts[0].name, parts[1])]
        for part in parts:
            pairs.extend(PagedExecTable._equalities(part))
        return pairs

    def scan(self, **kwargs):
        cond = kwargs.get("FilterExpression")
        wanted = dict(PagedExecTable._equalities(cond)) if cond is not None else {}
        matched = [
            item for item in self.items
            if all(item.get(name) == value for name, value in wanted.items())
        ]
        start = kwargs.get("ExclusiveStartKey")
        start_index = start["offset"] if start else 0
        page = matched[start_index:start_index + self.page_size]
        last = start_index + self.page_size
        result = {"Items": page}
        if page and last < len(matched):
            result["LastEvaluatedKey"] = {"offset": last}
        return result


def _configure(monkeypatch, items):
    monkeypatch.setenv("EXECUTIONS_TABLE", "executions")

    class Dynamo:
        def Table(self, _name):
            return PagedExecTable(items)

    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())


def _step(workflow_id, event_id, *, status="completed", run_id=None, resume_at=None):
    item = {
        "execution_id": f"{workflow_id}:wait:{event_id}",
        "workflow_id": workflow_id,
        "action_id": "wait",
        "status": status,
    }
    if run_id:
        item["run_id"] = run_id
    if resume_at is not None:
        item["output"] = {"resume_at": resume_at}
    return item


def test_finds_only_parked_steps_of_the_named_workflow(monkeypatch):
    _configure(monkeypatch, [
        _step("wf-a", "e1", status="delayed", run_id="wf-a:e1", resume_at=1234.5),
        _step("wf-a", "e2", status="completed", run_id="wf-a:e2"),
        _step("wf-b", "e3", status="delayed", run_id="wf-b:e3", resume_at=99.0),
    ])

    found = runs.delayed_runs("wf-a")

    assert found == [{"run_id": "wf-a:e1", "delayed_until": 1234.5}]


def test_one_parked_run_with_several_delayed_steps_is_one_entry(monkeypatch):
    _configure(monkeypatch, [
        _step("wf-a", "e1", status="delayed", run_id="wf-a:e1", resume_at=10.0),
        _step("wf-a", "e1b", status="delayed", run_id="wf-a:e1", resume_at=20.0),
    ])

    assert runs.delayed_runs("wf-a") == [{"run_id": "wf-a:e1", "delayed_until": 10.0}]


def test_run_id_falls_back_to_the_execution_id_shape(monkeypatch):
    _configure(monkeypatch, [
        _step("wf-a", "e9", status="delayed"),  # pre-grouping record: no run_id
    ])

    assert runs.delayed_runs("wf-a") == [{"run_id": "wf-a:e9", "delayed_until": None}]


def test_walks_scan_pages_and_stops_at_the_limit(monkeypatch):
    items = [_step("wf-a", f"e{i}", status="delayed", run_id=f"wf-a:e{i}")
             for i in range(5)]
    _configure(monkeypatch, items)  # page size 2: the walk needs all 3 pages

    everything = runs.delayed_runs("wf-a")
    assert [run["run_id"] for run in everything] == [f"wf-a:e{i}" for i in range(5)]

    capped = runs.delayed_runs("wf-a", limit=3)
    assert len(capped) == 3


def test_no_parked_runs_is_an_empty_list(monkeypatch):
    _configure(monkeypatch, [_step("wf-a", "e1", status="completed", run_id="wf-a:e1")])

    assert runs.delayed_runs("wf-a") == []
