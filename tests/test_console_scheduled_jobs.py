"""Scheduled jobs live under the Schedules tab, not the Workflows list.

The family tab strip puts Schedules second; the Schedules tab renders every
schedule trigger as a jobs-table row (schedule, starts, latest fire, state
toggle); the Workflows list hides schedule-only flows and points at the
Schedules tab instead. Purely presentational: the row toggle reuses the
shared pause/resume endpoints the CLI drives, so no parity gap opens.
"""

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
INDEX = (ROOT / "src" / "web" / "index.html").read_text()
SCHEDULES = (ROOT / "src" / "web" / "js" / "views" / "schedules.js").read_text()
OVERVIEW = (ROOT / "src" / "web" / "js" / "views" / "overview.js").read_text()
CSS = (ROOT / "src" / "web" / "app.css").read_text()


def test_schedules_jobs_table_markup():
    for column in ("<th>Schedule</th>", "<th>Starts</th>",
                   "<th>Latest fire</th>", "<th>State</th>"):
        assert column in INDEX
    assert '<tbody id="sched-list">' in INDEX
    assert '<ul id="sched-list"' not in INDEX


def test_schedule_rows_toggle_through_the_shared_endpoints():
    assert "data-sched-toggle" in SCHEDULES
    assert "sched-switch" in SCHEDULES
    assert "/api/admin/schedule-triggers/${encodeURIComponent(name)}/${action}" in SCHEDULES
    assert ".sched-switch {" in CSS
    assert ".sched-switch-track" in CSS


def test_workflows_list_hides_schedule_only_flows():
    assert "workflowIsScheduleOnly" in OVERVIEW
    assert "trigger.connector === 'schedule'" in OVERVIEW
    assert "Scheduled jobs live under Schedules" in OVERVIEW
    assert "live under Schedules" in OVERVIEW
