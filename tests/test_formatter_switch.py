"""The extract/switch/pluck formatters: value mapping with an odd-trailing
default, first-match extraction, and dotted-path reads out of JSON — each
with its empty paths, and all passing save-time validation."""
import unittest

import pytest

from src.dapier.engine.actions.templating import (
    TemplateError,
    render,
    validate_template,
)


class SwitchTests(unittest.TestCase):
    def render_one(self, template, value):
        return render(template, {"data": {"v": value}})

    def test_maps_values_to_targets(self):
        self.assertEqual(self.render_one("{v | switch:open:Open:closed:Closed}", "open"), "Open")
        self.assertEqual(self.render_one("{v | switch:open:Open:closed:Closed}", "closed"), "Closed")

    def test_an_odd_trailing_argument_is_the_default(self):
        self.assertEqual(self.render_one("{v | switch:a:b:c:d:other}", "zzz"), "other")
        self.assertEqual(self.render_one("{v | switch:a:b:other}", "zzz"), "other")

    def test_a_matched_pair_beats_the_default(self):
        self.assertEqual(self.render_one("{v | switch:a:b:other}", "a"), "b")

    def test_no_match_and_no_default_renders_empty(self):
        self.assertEqual(self.render_one("{v | switch:a:b:c:d}", "zzz"), "")
        self.assertEqual(self.render_one("{v | switch:a:b}", "zzz"), "")

    def test_no_arguments_match_renders_empty(self):
        self.assertEqual(self.render_one("{v | switch:a:b}", ""), "")

    def test_one_argument_fails_save_time_validation(self):
        with pytest.raises(TemplateError, match="takes 2 argument"):
            validate_template("{v | switch:a}")

    def test_many_pairs_pass_save_time_validation(self):
        validate_template("{v | switch:a:b:c:d:e:f:g}")


class ExtractTests(unittest.TestCase):
    def render_one(self, template, value):
        return render(template, {"data": {"v": value}})

    def test_extract_email_takes_the_first_address(self):
        self.assertEqual(self.render_one("{v | extract_email}",
                                         "ping bob.smith+ops@example.co.uk thanks"),
                         "bob.smith+ops@example.co.uk")

    def test_extract_email_without_an_address_renders_empty(self):
        self.assertEqual(self.render_one("{v | extract_email}", "no address here"), "")
        self.assertEqual(self.render_one("{v | extract_email}", ""), "")

    def test_extract_url_takes_the_first_http_url(self):
        self.assertEqual(
            self.render_one("{v | extract_url}",
                            "see https://example.test/a?q=1 first, ftp://skipme.test ignored"),
            "https://example.test/a?q=1")

    def test_extract_url_also_matches_plain_http(self):
        self.assertEqual(self.render_one("{v | extract_url}", "http://example.test"),
                         "http://example.test")

    def test_extract_url_leaves_prose_punctuation_behind(self):
        self.assertEqual(self.render_one("{v | extract_url}", "visit https://x.test/page."),
                         "https://x.test/page")

    def test_extract_url_without_a_url_renders_empty(self):
        self.assertEqual(self.render_one("{v | extract_url}", "not a link"), "")

    def test_extract_number_takes_the_first_number_with_sign_and_decimals(self):
        self.assertEqual(self.render_one("{v | extract_number}", "total: -3.14 USD"), "-3.14")
        self.assertEqual(self.render_one("{v | extract_number}", "order +7 shipped"), "+7")

    def test_extract_number_without_a_number_renders_empty(self):
        self.assertEqual(self.render_one("{v | extract_number}", "no digits"), "")


import pytest


class PluckTests(unittest.TestCase):
    def render_one(self, template, value):
        return render(template, {"data": {"v": value}})

    JSON = '{"user": {"email": "a@b.c", "tags": [1, 2], "active": true}, "n": 5}'

    def test_reads_a_scalar_as_a_string(self):
        self.assertEqual(self.render_one("{v | pluck:user.email}", self.JSON), "a@b.c")
        self.assertEqual(self.render_one("{v | pluck:n}", self.JSON), "5")
        self.assertEqual(self.render_one("{v | pluck:user.active}", self.JSON), "True")

    def test_reads_a_container_as_compact_json(self):
        self.assertEqual(self.render_one("{v | pluck:user.tags}", self.JSON), "[1,2]")
        self.assertEqual(self.render_one("{v | pluck:user}", self.JSON),
                         '{"email":"a@b.c","tags":[1,2],"active":true}')

    def test_numeric_segments_index_lists(self):
        self.assertEqual(self.render_one("{v | pluck:user.tags.1}", self.JSON), "2")

    def test_a_json_list_input_plucks_too(self):
        self.assertEqual(self.render_one("{v | pluck:0}", "[\"x\", \"y\"]"), "x")

    def test_an_already_rendered_value_reaches_the_parser_as_text(self):
        # A dict step output stringifies as JSON before the chain runs, so a
        # pluck on it reads the same as on a JSON string field.
        self.assertEqual(
            render("{steps.find.output | pluck:row}", {"data": {}},
                   {"find": {"status": "completed", "output": {"row": 7}}}),
            "7")

    def test_a_miss_renders_empty(self):
        self.assertEqual(self.render_one("{v | pluck:user.nope}", self.JSON), "")
        self.assertEqual(self.render_one("{v | pluck:user.tags.9}", self.JSON), "")

    def test_a_parse_failure_renders_empty(self):
        self.assertEqual(self.render_one("{v | pluck:user}", "not json"), "")
        self.assertEqual(self.render_one("{v | pluck:user}", ""), "")

    def test_the_path_argument_is_required_at_save_time(self):
        with pytest.raises(TemplateError):
            validate_template("{v | pluck}")


class CatalogAndValidationTests(unittest.TestCase):
    def test_the_new_formatters_reach_the_catalog(self):
        from src.dapier.connectors import registry

        formatters = registry.catalog()["formatters"]
        for name in ("switch", "extract_email", "extract_url", "extract_number", "pluck"):
            self.assertIn(name, formatters)

    def test_save_time_validation_accepts_the_new_grammars(self):
        validate_template("{a | switch:x:y:default} {b | extract_email} "
                          "{c | extract_url} {d | extract_number} {e | pluck:user.email}")


if __name__ == "__main__":
    unittest.main()
