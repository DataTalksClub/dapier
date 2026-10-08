"""The Schedules tab drives the shared schedule endpoints (activity + health)."""
from pathlib import Path


def test_console_schedules_view_drives_the_shared_endpoints():
    root = Path(__file__).resolve().parents[1] / "src" / "web"
    view = (root / "js" / "views" / "schedules.js").read_text()
    html = (root / "index.html").read_text()
    for route in ("/api/admin/schedule-triggers/upcoming?hours=",
                  "/api/admin/schedule-triggers/${name}/run",
                  "/api/admin/schedule-triggers/${name}/${action}",
                  "/api/admin/schedule-triggers?name="):
        assert route in view
    # Every console action has its CLI twin.
    cli = (Path(__file__).resolve().parents[1] / "dapier_cli" / "cli" / "schedules.py").read_text()
    for command in ('"upcoming"', '"run"', '"pause"', '"resume"', '"show"'):
        assert command in cli
    for anchor in ('id="sched-workspace"', 'id="sched-upcoming"', 'id="sched-list"',
                   'id="sched-detail"', 'id="new-schedule"', 'data-sched-filter="attention"'):
        assert anchor in html
