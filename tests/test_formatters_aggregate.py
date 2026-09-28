"""List-aggregation formatters: sum/min/max/avg over a rendered list, plus
unique and sort to reshape one — the digest/for_each companion from the gap
analysis (templates could iterate a list but not aggregate it)."""
import unittest

from src.dapier.connectors import registry
from src.dapier.engine.actions.templating import (
    FORMATTERS,
    TemplateError,
    render,
    validate_template,
)


ROWS = [
    {"id": "r1", "amount": 10, "tag": "a"},
    {"id": "r2", "amount": 20.5, "tag": "b"},
    {"id": "r3", "amount": 2, "tag": "a"},
]


class AggregateTests(unittest.TestCase):
    def render_rows(self, template, rows=ROWS):
        return render(template, {"data": {"rows": rows}})

    def test_sum_min_and_max_over_a_field(self):
        self.assertEqual(self.render_rows("{rows | sum:amount}"), "32.5")
        self.assertEqual(self.render_rows("{rows | min:amount}"), "2")
        self.assertEqual(self.render_rows("{rows | max:amount}"), "20.5")

    def test_avg_divides_the_fold(self):
        even = [{"amount": n} for n in (10, 20, 30)]
        self.assertEqual(self.render_rows("{rows | avg:amount}", even), "20")
        fractional = [{"amount": 1}, {"amount": 2}]
        self.assertEqual(self.render_rows("{rows | avg:amount}", fractional), "1.5")

    def test_thousands_separators_match_number_format(self):
        rows = [{"amount": n} for n in (1000, 2000, 3000)]
        self.assertEqual(self.render_rows("{rows | sum:amount}", rows), "6,000")
        self.assertEqual(self.render_rows("{rows | max:amount}", rows), "3,000")

    def test_nested_dotted_paths_aggregate(self):
        rows = [{"meta": {"depth": 4}}, {"meta": {"depth": 6}}]
        self.assertEqual(self.render_rows("{rows | sum:meta.depth}", rows), "10")

    def test_missing_and_non_numeric_entries_are_skipped(self):
        rows = [{"amount": 5}, {"id": "no-amount"}, {"amount": "20"}, {"amount": None}]
        self.assertEqual(self.render_rows("{rows | sum:amount}", rows), "5")

    def test_booleans_do_not_count_as_numbers(self):
        rows = [{"amount": True}, {"amount": 3}]
        self.assertEqual(self.render_rows("{rows | sum:amount}", rows), "3")

    def test_empty_fold_renders_empty(self):
        self.assertEqual(self.render_rows("{rows | sum:amount}", []), "")
        self.assertEqual(self.render_rows("{rows | sum:missing}", ROWS), "")
        self.assertEqual(self.render_rows("{rows | sum:amount}", [1, 2, 3]), "")

    def test_a_non_list_value_renders_empty(self):
        self.assertEqual(self.render_rows("{rows | sum:amount}", "nope"), "")
        self.assertEqual(self.render_rows("{rows | sum:amount}", {"amount": 4}), "")


class UniqueTests(unittest.TestCase):
    def render_rows(self, template, rows=ROWS):
        return render(template, {"data": {"rows": rows}})

    def test_unique_by_field_preserves_first_seen_order(self):
        self.assertEqual(self.render_rows("{rows | unique:tag}"), '["a", "b"]')

    def test_unique_whole_items(self):
        rows = [{"id": 1}, {"id": 2}, {"id": 1}]
        self.assertEqual(self.render_rows("{rows | unique}", rows),
                         '[{"id": 1}, {"id": 2}]')

    def test_unique_dedupes_dict_items_ignoring_key_order(self):
        rows = [{"a": 1, "b": 2}, {"b": 2, "a": 1}]
        self.assertEqual(self.render_rows("{rows | unique}", rows), '[{"a": 1, "b": 2}]')

    def test_unique_drops_entries_missing_the_field(self):
        rows = [{"tag": "a"}, {"id": "x"}, {"tag": "a"}]
        self.assertEqual(self.render_rows("{rows | unique:tag}", rows), '["a"]')

    def test_unique_on_a_non_list_renders_empty(self):
        self.assertEqual(self.render_rows("{rows | unique}", 5), "")


class SortTests(unittest.TestCase):
    def render_value(self, template, value, rows=ROWS):
        return render(template, {"data": {"rows": rows, "v": value}})

    def test_sort_by_field_numerically_ascending_and_descending(self):
        self.assertEqual(self.render_value("{rows | sort:amount | pluck:0.id}", None), "r3")
        self.assertEqual(self.render_value("{rows | sort:amount:desc | pluck:0.id}", None), "r2")

    def test_sort_defaults_to_ascending(self):
        self.assertEqual(self.render_value("{rows | sort:amount:asc | pluck:0.id}", None), "r3")

    def test_sort_the_list_itself_without_a_field(self):
        self.assertEqual(self.render_value("{v | sort}", [3, 1, 2]), "[1, 2, 3]")
        self.assertEqual(self.render_value("{v | sort:desc}", ["b", "c", "a"]),
                         '["c", "b", "a"]')

    def test_sort_is_stable_for_equal_keys(self):
        rows = [{"amount": 2, "id": "first"}, {"amount": 1, "id": "x"},
                {"amount": 2, "id": "second"}]
        self.assertEqual(self.render_value("{rows | sort:amount}", None, rows),
                         '[{"amount": 1, "id": "x"}, {"amount": 2, "id": "first"},'
                         ' {"amount": 2, "id": "second"}]')
        self.assertEqual(self.render_value("{rows | sort:amount:desc}", None, rows),
                         '[{"amount": 2, "id": "first"}, {"amount": 2, "id": "second"},'
                         ' {"amount": 1, "id": "x"}]')

    def test_mixed_types_degrade_to_string_order(self):
        rows = [{"v": 10}, {"v": 9}, {"v": "apple"}]
        self.assertEqual(self.render_value("{rows | sort:v}", None, rows),
                         '[{"v": 10}, {"v": 9}, {"v": "apple"}]')

    def test_a_missing_field_sorts_as_empty_string(self):
        rows = [{"id": "b", "tag": "z"}, {"id": "a"}, {"id": "c", "tag": "y"}]
        self.assertEqual(self.render_value("{rows | sort:tag}", None, rows),
                         '[{"id": "a"}, {"id": "c", "tag": "y"}, {"id": "b", "tag": "z"}]')

    def test_unknown_order_renders_empty(self):
        self.assertEqual(self.render_value("{rows | sort:amount:upward}", None), "")

    def test_sort_on_a_non_list_renders_empty(self):
        self.assertEqual(self.render_value("{rows | sort:amount}", None, rows=7), "")


class CompositionAndCatalogTests(unittest.TestCase):
    def render_rows(self, template, rows=ROWS):
        return render(template, {"data": {"rows": rows}})

    def test_unique_composes_with_join(self):
        self.assertEqual(self.render_rows("{rows | unique:tag | join:,}"), "a,b")

    def test_sorted_rows_feed_pluck(self):
        self.assertEqual(self.render_rows("{rows | sort:amount:desc | pluck:0.amount}"), "20.5")

    def test_aggregates_compose_left_to_right(self):
        self.assertEqual(self.render_rows("{rows | sum:amount | default:0}"), "32.5")

    def test_new_names_pass_save_time_validation(self):
        validate_template("{rows | sum:a} {rows | min:a} {rows | max:a} {rows | avg:a}"
                          " {rows | unique} {rows | unique:a} {rows | sort}"
                          " {rows | sort:a:desc}")

    def test_wrong_arity_fails_save_time_validation(self):
        with self.assertRaises(TemplateError):
            validate_template("{rows | sum}")
        with self.assertRaises(TemplateError):
            validate_template("{rows | avg:a:b}")
        with self.assertRaises(TemplateError):
            validate_template("{rows | sort:a:desc:extra}")

    def test_the_catalog_lists_the_new_formatters(self):
        names = set(registry.catalog()["formatters"])
        self.assertTrue({"sum", "min", "max", "avg", "unique", "sort"} <= names)
        for name in ("sum", "min", "max", "avg", "unique", "sort"):
            self.assertIn(name, FORMATTERS)
