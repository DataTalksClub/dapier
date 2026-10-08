"""When a schedule fires: EventBridge cron/rate expressions, read for people.

The Schedules tab and ``dapier schedules`` answer "when do things run, and
did they?". This module owns the first half without calling AWS:

- :func:`next_fires` walks an expression forward from a moment and lists the
  fire times (UTC) EventBridge will use — the Upcoming list and the
  per-schedule "next" times;
- :func:`describe` turns the expression into plain language
  ("every weekday at 09:00 UTC") shown next to the raw string;
- :func:`typical_interval` is the usual gap between fires, which the health
  check uses to say how overdue a silent schedule is.

EventBridge cron has six fields — minute hour day-of-month month
day-of-week year — plus an optional IANA timezone, with ``?`` for whichever
of the two day fields is unused, day-of-week numbered 1=SUN..7=SAT, and the
``L`` / ``W`` / ``#`` specials. Rate schedules fire every N units counted
from when the rule was (re)programmed, so their times are approximate: the
caller passes the best anchor it has (the last fire, else the last save).
"""

import calendar
import re
from datetime import date, datetime, timedelta, timezone

MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
          "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
DAYS = ["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"]  # EventBridge 1..7
DAY_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July",
               "August", "September", "October", "November", "December"]
RATE_UNITS = {"minute": 60, "hour": 3600, "day": 86400}
# How far forward a cron walk looks for its next fire: a year-field cron
# (cron(0 9 1 1 ? 2030)) simply has no fire inside the horizon.
HORIZON_DAYS = 400

_CRON = re.compile(r"^cron\(\s*(.*?)\s*\)$")
_RATE = re.compile(r"^rate\(\s*(\d+)\s+(minute|hour|day)s?\s*\)$")


class ExpressionError(ValueError):
    """The expression cannot be read (EventBridge itself validates on save)."""


def _utc(moment):
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def parse_time(value):
    """An ISO timestamp as an aware UTC datetime, or None."""
    if isinstance(value, datetime):
        return _utc(value)
    try:
        return _utc(datetime.fromisoformat(str(value or "").replace("Z", "+00:00")))
    except ValueError:
        return None


# --- parsing ---------------------------------------------------------------

def _number(token, names=None, offset=0):
    token = token.strip().upper()
    if names and token in names:
        return names.index(token) + offset
    if not token.isdigit():
        raise ExpressionError(f"cannot read '{token}'")
    return int(token)


def _values(field, low, high, names=None, offset=0):
    """A cron list/range/step field as the set of values it allows."""
    allowed = set()
    for part in field.split(","):
        step = 1
        if "/" in part:
            part, step_text = part.split("/", 1)
            step = _number(step_text)
            if step < 1:
                raise ExpressionError("a step must be at least 1")
        if part in ("*", "?"):
            start, end = low, high
        elif "-" in part:
            first, last = part.split("-", 1)
            start, end = _number(first, names, offset), _number(last, names, offset)
        else:
            start = _number(part, names, offset)
            end = high if step > 1 else start
        if start < low or end > high:
            raise ExpressionError(f"'{part}' is outside {low}-{high}")
        if end < start:  # FRI-MON wraps
            allowed.update(range(start, high + 1, step))
            allowed.update(range(low, end + 1, step))
        else:
            allowed.update(range(start, end + 1, step))
    return allowed


def _eb_weekday(day):
    """EventBridge day-of-week (1=SUN..7=SAT) for a date."""
    return (day.weekday() + 1) % 7 + 1


def _dom_matcher(field):
    if field in ("?", "*"):
        return lambda day: True
    if field == "L":
        return lambda day: day.day == calendar.monthrange(day.year, day.month)[1]
    if field == "LW":
        def last_weekday(day):
            last = date(day.year, day.month, calendar.monthrange(day.year, day.month)[1])
            while last.weekday() > 4:
                last -= timedelta(days=1)
            return day == last
        return last_weekday
    match = re.fullmatch(r"(\d+)W", field)
    if match:
        target = int(match.group(1))

        def nearest_weekday(day):
            length = calendar.monthrange(day.year, day.month)[1]
            pick = date(day.year, day.month, min(target, length))
            if pick.weekday() == 5:  # Saturday -> Friday, unless that leaves the month
                pick = pick - timedelta(days=1) if pick.day > 1 else pick + timedelta(days=2)
            elif pick.weekday() == 6:  # Sunday -> Monday, unless that leaves the month
                pick = pick + timedelta(days=1) if pick.day < length else pick - timedelta(days=2)
            return day == pick
        return nearest_weekday
    allowed = _values(field, 1, 31)
    return lambda day: day.day in allowed


def _dow_matcher(field):
    if field in ("?", "*"):
        return lambda day: True
    match = re.fullmatch(r"([A-Za-z]{3}|\d)#(\d)", field)
    if match:
        weekday, nth = _number(match.group(1), DAYS, 1), int(match.group(2))
        return lambda day: _eb_weekday(day) == weekday and (day.day - 1) // 7 + 1 == nth
    match = re.fullmatch(r"([A-Za-z]{3}|\d)L", field)
    if match:
        weekday = _number(match.group(1), DAYS, 1)
        return lambda day: (_eb_weekday(day) == weekday
                            and day.day + 7 > calendar.monthrange(day.year, day.month)[1])
    if field == "L":
        return lambda day: _eb_weekday(day) == 7
    allowed = _values(field, 1, 7, DAYS, 1)
    return lambda day: _eb_weekday(day) in allowed


class Cron:
    """A parsed EventBridge cron expression."""

    def __init__(self, text):
        fields = text.split()
        if len(fields) not in (6, 7):
            raise ExpressionError("cron needs six fields (and an optional timezone)")
        self.fields = fields[:6]
        self.tz_name = fields[6] if len(fields) == 7 else "UTC"
        minute, hour, dom, month, dow, year = self.fields
        self.minutes = sorted(_values(minute, 0, 59))
        self.hours = sorted(_values(hour, 0, 23))
        self.months = _values(month, 1, 12, MONTHS, 1)
        self.years = _values(year, 1970, 2199)
        self.dom = _dom_matcher(dom.upper())
        self.dow = _dow_matcher(dow.upper())
        self.tz = timezone.utc
        self.tz_known = True
        if self.tz_name.upper() != "UTC":
            try:
                from zoneinfo import ZoneInfo

                self.tz = ZoneInfo(self.tz_name)
            except Exception:  # unknown/unavailable zone: read the times as UTC
                self.tz_known = False

    def day_matches(self, day):
        return (day.year in self.years and day.month in self.months
                and self.dom(day) and self.dow(day))

    def fires(self, after, until):
        """Fire times strictly after ``after`` up to ``until`` (UTC), in order."""
        local_after = after.astimezone(self.tz)
        day = local_after.date()
        last_day = until.astimezone(self.tz).date()
        while day <= last_day:
            if self.day_matches(day):
                for hour in self.hours:
                    for minute in self.minutes:
                        moment = datetime(day.year, day.month, day.day, hour, minute,
                                          tzinfo=self.tz).astimezone(timezone.utc)
                        if moment <= after:
                            continue
                        if moment > until:
                            return
                        yield moment
            day += timedelta(days=1)


def _parse(expression):
    expression = str(expression or "").strip()
    rate = _RATE.fullmatch(expression)
    if rate:
        count, unit = int(rate.group(1)), rate.group(2)
        if count < 1:
            raise ExpressionError("a rate must be at least 1")
        return "rate", count * RATE_UNITS[unit]
    cron = _CRON.fullmatch(expression)
    if cron:
        return "cron", Cron(cron.group(1))
    raise ExpressionError("not a cron(...) or rate(...) expression")


def is_approximate(expression):
    """Rate times hang off the rule's start, which only EventBridge knows."""
    try:
        kind, parsed = _parse(expression)
    except ExpressionError:
        return True
    return kind == "rate" or not parsed.tz_known


def next_fires(expression, after, *, count=5, until=None, anchor=None):
    """Up to ``count`` fire times (aware UTC datetimes) after ``after``.

    ``until`` bounds the walk (default: the horizon). ``anchor`` is where a
    rate schedule counts from — its last fire, else its last save; without
    one the rate counts from ``after``. An unreadable expression has no
    fires (``[]``) rather than an error: the list must not break on one row.
    """
    after = _utc(after)
    until = _utc(until) if until else after + timedelta(days=HORIZON_DAYS)
    try:
        kind, parsed = _parse(expression)
    except ExpressionError:
        return []
    out = []
    if kind == "rate":
        start = _utc(anchor) if anchor else after
        moment = start + timedelta(seconds=parsed)
        if moment <= after:  # skip whole periods already behind us
            missed = int((after - start).total_seconds() // parsed)
            moment = start + timedelta(seconds=parsed * (missed + 1))
        while moment <= until and len(out) < count:
            out.append(moment)
            moment += timedelta(seconds=parsed)
        return out
    for moment in parsed.fires(after, until):
        out.append(moment)
        if len(out) >= count:
            break
    return out


def typical_interval(expression, after=None):
    """The usual gap between fires, in seconds (None when it never repeats
    within the horizon). Taken as the median of the next few gaps, so a
    weekday schedule reads as daily, not as "every three days"."""
    try:
        kind, parsed = _parse(expression)
    except ExpressionError:
        return None
    if kind == "rate":
        return parsed
    after = _utc(after or datetime.now(timezone.utc))
    times = next_fires(expression, after, count=8)
    if len(times) < 2:
        return None
    gaps = sorted(int((b - a).total_seconds()) for a, b in zip(times, times[1:]))
    return gaps[len(gaps) // 2]


# --- plain language ----------------------------------------------------------

def _plural(count, unit):
    return f"every {unit}" if count == 1 else f"every {count} {unit}s"


def _clock(hour, minute):
    return f"{hour:02d}:{minute:02d}"


def _join(words):
    words = list(words)
    if len(words) <= 1:
        return "".join(words)
    return ", ".join(words[:-1]) + " and " + words[-1]


def _ordinal(n):
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _step(field):
    """The step of a '*/n' or '0/n' field, else None."""
    match = re.fullmatch(r"(?:\*|0)/(\d+)", field)
    return int(match.group(1)) if match else None


def _days_phrase(dom, dow):
    """(every-form, on-form) for the day fields, or None when unreadable."""
    dom, dow = dom.upper(), dow.upper()
    if dom in ("?", "*") and dow in ("?", "*"):
        return "every day", ""
    if dom in ("?", "*"):
        match = re.fullmatch(r"([A-Z]{3}|\d)#(\d)", dow)
        if match:
            name = DAY_NAMES[_number(match.group(1), DAYS, 1) - 1]
            text = f"on the {_ordinal(int(match.group(2)))} {name} of the month"
            return text, text
        match = re.fullmatch(r"([A-Z]{3}|\d)L", dow)
        if match:
            name = DAY_NAMES[_number(match.group(1), DAYS, 1) - 1]
            text = f"on the last {name} of the month"
            return text, text
        try:
            days = _values(dow, 1, 7, DAYS, 1)
        except ExpressionError:
            return None
        if days == {2, 3, 4, 5, 6}:
            return "every weekday", "on weekdays"
        if days == {1, 7}:
            return "every weekend day", "on weekends"
        names = [DAY_NAMES[day - 1] for day in sorted(days)]
        return f"every {_join(names)}", f"on {_join(name + 's' for name in names)}"
    if dom == "L":
        return "on the last day of the month", "on the last day of the month"
    if dom == "LW":
        text = "on the last weekday of the month"
        return text, text
    match = re.fullmatch(r"(\d+)W", dom)
    if match:
        text = f"on the weekday nearest the {_ordinal(int(match.group(1)))}"
        return text, text
    try:
        days = sorted(_values(dom, 1, 31))
    except ExpressionError:
        return None
    if len(days) > 4:
        text = f"on {len(days)} days of the month"
    else:
        text = f"on the {_join(_ordinal(day) for day in days)} of the month"
    return text, text


def _time_phrase(minute, hour):
    """('at', text) for fixed clock times, ('every', text) for repeats."""
    if minute == "*" and hour == "*":
        return "every", "every minute"
    step = _step(minute)
    if step and hour == "*":
        return "every", _plural(step, "minute")
    try:
        minutes = sorted(_values(minute, 0, 59))
    except ExpressionError:
        return None
    if step and hour != "*":
        try:
            hours = sorted(_values(hour, 0, 23))
        except ExpressionError:
            return None
        return "every", (f"{_plural(step, 'minute')} from {_clock(hours[0], 0)} "
                         f"to {_clock(hours[-1], 59)} {{tz}}")
    if len(minutes) != 1:
        return None
    mm = minutes[0]
    if hour == "*":
        return "every", f"every hour at :{mm:02d}"
    hour_step = _step(hour)
    if hour_step:
        return "every", f"{_plural(hour_step, 'hour')} at :{mm:02d}"
    try:
        hours = sorted(_values(hour, 0, 23))
    except ExpressionError:
        return None
    if "-" in hour and "," not in hour and len(hours) > 2:
        return "every", (f"every hour from {_clock(hours[0], mm)} "
                         f"to {_clock(hours[-1], mm)} {{tz}}")
    if len(hours) > 6:
        return "every", f"{len(hours)} times a day at :{mm:02d}"
    return "at", "at " + _join(_clock(h, mm) for h in hours)


def describe(expression):
    """Plain language for an expression, e.g. 'every weekday at 09:00 UTC'.

    Unusual shapes fall back to 'on a custom cron schedule' rather than a
    wrong reading; the raw expression always sits next to it.
    """
    try:
        kind, parsed = _parse(expression)
    except ExpressionError:
        return "unreadable schedule expression"
    if kind == "rate":
        for unit, seconds in (("day", 86400), ("hour", 3600), ("minute", 60)):
            if parsed % seconds == 0:
                return _plural(parsed // seconds, unit)
    minute, hour, dom, month, dow, year = parsed.fields
    days = _days_phrase(dom, dow)
    when = _time_phrase(minute, hour)
    if days is None or when is None:
        return "on a custom cron schedule"
    every_form, on_form = days
    style, time_text = when
    zone = "UTC" if parsed.tz_name.upper() == "UTC" else parsed.tz_name
    if style == "at":
        text = f"{every_form} {time_text} {zone}"
    else:
        if "{tz}" in time_text:
            time_text = time_text.replace("{tz}", zone)
        elif zone != "UTC":
            time_text += f" ({zone})"
        text = time_text + (f" {on_form}" if on_form else "")
    if month != "*":
        try:
            months = sorted(_values(month, 1, 12, MONTHS, 1))
            text += " in " + _join(MONTH_NAMES[m - 1] for m in months)
        except ExpressionError:
            return "on a custom cron schedule"
    if year != "*":
        text += f" {year}" if month != "*" else f" in {year}"
    return text


def describe_interval(seconds):
    """'15 minutes', 'hour', '3 days' — for "expected every …"."""
    if not seconds:
        return ""
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds % size == 0 or seconds >= size:
            count = round(seconds / size)
            return unit if count == 1 else f"{count} {unit}s"
    return f"{seconds} seconds"
