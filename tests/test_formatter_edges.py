"""Formatter edges beyond the base set: timezone-aware ``date_format``,
relative ``time_until``, ``number_format``'s currency mode, their chained
composition, and their surfacing through the registry catalog."""
import time as time_module
import unittest
from datetime import datetime, timedelta, timezone

import pytest

from src.dapier.connectors import registry
from src.dapier.engine.actions.templating import (
    TemplateError,
    render,
    validate_template,
)


def now_utc():
    return datetime.now(timezone.utc)


class DateFormatZoneTests(unittest.TestCase):
    """``date_format`` renders in an IANA zone appended to the spec
    (``date_format:<strftime>@<zone>``); invalid zones render empty like
    any other bad formatter input."""

    def render_one(self, template, value):
        return render(template, {"data": {"v": value}})

    def test_utc_input_renders_in_the_target_zone(self):
        self.assertEqual(
            self.render_one("{v | date_format:%Y-%m-%d %H:%M@Europe/Berlin}",
                            "2026-09-24T15:00:00+00:00"),
            "2026-09-24 17:00")

    def test_offset_input_converts_back_to_utc(self):
        self.assertEqual(
            self.render_one("{v | date_format:%H:%M@UTC}",
                            "2026-09-24T17:00:00+02:00"),
            "15:00")

    def test_half_hour_zone(self):
        self.assertEqual(
            self.render_one("{v | date_format:%H:%M@Asia/Kolkata}",
                            "2026-09-24T15:00:00Z"),
            "20:30")

    def test_naive_value_reads_as_utc(self):
        self.assertEqual(
            self.render_one("{v | date_format:%H:%M@Europe/Berlin}",
                            "2026-09-24 15:05:00"),
            "17:05")

    def test_unknown_zone_renders_empty_not_a_crash(self):
        self.assertEqual(
            self.render_one("{v | date_format:%Y@Mars/Olympus}",
                            "2026-09-24T15:00:00Z"), "")

    def test_chains_after_date_offset(self):
        self.assertEqual(
            self.render_one("{v | date_offset:1d | date_format:%Y-%m-%d %H:%M@Europe/Berlin}",
                            "2026-09-24T15:00:00+00:00"),
            "2026-09-25 17:00")


class TimeUntilTests(unittest.TestCase):
    """``time_until`` renders the human gap to now: ``in 2d`` / ``5d ago`` /
    ``just now`` (inside a minute); ISO 8601 or bare epoch seconds in."""

    def render_one(self, template, value):
        return render(template, {"data": {"v": value}})

    def test_future_renders_in(self):
        self.assertEqual(
            self.render_one("{v | time_until}",
                            (now_utc() + timedelta(hours=50)).isoformat()),
            "in 2d")

    def test_past_renders_ago(self):
        self.assertEqual(
            self.render_one("{v | time_until}",
                            (now_utc() - timedelta(days=5, hours=2)).isoformat()),
            "5d ago")

    def test_weeks_win_over_days(self):
        self.assertEqual(
            self.render_one("{v | time_until}",
                            (now_utc() + timedelta(days=15)).isoformat()),
            "in 2w")

    def test_hours_for_sub_day_gaps(self):
        self.assertEqual(
            self.render_one("{v | time_until}",
                            (now_utc() - timedelta(hours=3)).isoformat()),
            "3h ago")

    def test_inside_a_minute_is_just_now(self):
        self.assertEqual(
            self.render_one("{v | time_until}",
                            (now_utc() + timedelta(seconds=10)).isoformat()),
            "just now")

    def test_epoch_seconds_input(self):
        self.assertEqual(
            self.render_one("{v | time_until}",
                            str(int(time_module.time()) + 7200)),
            "in 2h")

    def test_past_epoch_seconds_input(self):
        self.assertEqual(
            self.render_one("{v | time_until}",
                            str(int(time_module.time()) - 3700)),
            "1h ago")

    def test_unparseable_value_renders_empty(self):
        self.assertEqual(self.render_one("{v | time_until}", "not a date"), "")


class NumberFormatCurrencyTests(unittest.TestCase):
    """``number_format:currency:CODE[:decimals]`` renders the amount with
    the currency symbol (EUR/USD/GBP mapped); an unmapped code falls back
    to a suffixed code instead of guessing a glyph."""

    def render_one(self, template, value):
        return render(template, {"data": {"v": value}})

    def test_mapped_symbols(self):
        self.assertEqual(self.render_one("{v | number_format:currency:EUR}", "12345.67"),
                         "€12,345.67")
        self.assertEqual(self.render_one("{v | number_format:currency:USD}", "12345.67"),
                         "$12,345.67")
        self.assertEqual(self.render_one("{v | number_format:currency:GBP}", "12345.67"),
                         "£12,345.67")

    def test_lowercase_code_maps_the_same(self):
        self.assertEqual(self.render_one("{v | number_format:currency:eur}", "12345.67"),
                         "€12,345.67")

    def test_decimal_override(self):
        self.assertEqual(self.render_one("{v | number_format:currency:EUR:1}", "12345.67"),
                         "€12,345.7")

    def test_unmapped_code_falls_back_to_the_code(self):
        self.assertEqual(self.render_one("{v | number_format:currency:CHF}", "12345.67"),
                         "12,345.67 CHF")

    def test_currency_without_a_code_renders_empty(self):
        self.assertEqual(self.render_one("{v | number_format:currency}", "12345.67"), "")

    def test_non_numeric_value_renders_empty(self):
        self.assertEqual(self.render_one("{v | number_format:currency:USD}", "lots"), "")


class FormatterEdgeValidationTests(unittest.TestCase):
    """The new grammars clear save-time validation; over-arg forms do not."""

    def test_new_grammars_validate(self):
        validate_template("{v | date_format:%H:%M@Europe/Berlin} {w | time_until} "
                          "{n | number_format:currency:EUR:2} {m | number_format:currency:USD}")

    def test_plain_number_format_still_takes_one_argument(self):
        with pytest.raises(TemplateError):
            validate_template("{n | number_format:1:2}")

    def test_currency_mode_caps_at_three_arguments(self):
        with pytest.raises(TemplateError):
            validate_template("{n | number_format:currency:EUR:2:9}")


class CatalogSurfaceTests(unittest.TestCase):
    """Formatters reach the console through the registry catalog; the new
    names must be listed there (derived from FORMATTERS, sorted)."""

    def test_catalog_lists_the_new_formatters(self):
        formatters = registry.catalog()["formatters"]
        for name in ("time_until", "number_format", "date_format"):
            self.assertIn(name, formatters)
        self.assertEqual(formatters, sorted(formatters))


if __name__ == "__main__":
    unittest.main()
