"""Date/time formatting and processing clock, shared by all workflow surfaces."""

from ..engine.actions.date_time import run_date_time
from .registry import Action, register

register(
    Action(
        type="date_time",
        label="Date / time",
        icon="clock",
        run=lambda action, event, workflow_id, steps=None: run_date_time(
            action, event, steps=steps
        ),
        optional=frozenset({"value", "timezone", "format"}),
        description="Format a timestamp, or current processing time when value is omitted. Output: {iso, formatted, timezone}.",
        fields=(
            {
                "key": "value",
                "label": "Timestamp",
                "placeholder": "{date}",
                "help": "ISO or email Date with offset; omit for current processing time",
            },
            {
                "key": "timezone",
                "label": "Timezone",
                "placeholder": "America/Chicago",
                "default": "UTC",
            },
            {"key": "format", "label": "Format", "default": "%Y-%m-%d"},
        ),
    )
)
