"""Format a source timestamp or the current processing time in an IANA zone."""

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

from .templating import render


def run_date_time(action, event, *, steps=None):
    zone_name = render(str(action.get("timezone") or "UTC"), event, steps).strip()
    zone = ZoneInfo(zone_name)
    raw = render(str(action.get("value") or ""), event, steps).strip()
    if "value" in action and not raw:
        raise ValueError(
            "date_time value rendered empty; omit value for processing time"
        )
    if raw:
        try:
            moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            moment = parsedate_to_datetime(raw)
        if moment.tzinfo is None:
            raise ValueError("date_time value must include a timezone offset")
    else:
        moment = datetime.now(timezone.utc)
    moment = moment.astimezone(zone)
    fmt = render(str(action.get("format") or "%Y-%m-%d"), event, steps)
    return {
        "iso": moment.isoformat(timespec="seconds"),
        "formatted": moment.strftime(fmt),
        "timezone": zone_name,
    }
