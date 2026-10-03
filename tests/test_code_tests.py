"""Code-block tests: named cases attached to a code/js action."""
import os
import subprocess
import sys
import unittest

from src.dapier.engine.code_tests import (
    CodeTestsError,
    run_code_tests,
    validate_code_tests,
)


def action(source, tests, action_type="code"):
    return {"type": action_type, "code": source, "tests": tests}


class CodeTestsRunTests(unittest.TestCase):
    def test_expected_result_passes_and_fails_on_mismatch(self):
        tests = action(
            '{"n": len(input["items"])}',
            [
                {"name": "empty", "input": {"items": []}, "expected": {"n": 0}},
                {"name": "two", "input": {"items": [1, 2]}, "expected": {"n": 2}},
                {"name": "wrong", "input": {"items": [1]}, "expected": {"n": 9}},
            ],
        )
        report = run_code_tests(tests)
        self.assertEqual(report["total"], 3)
        self.assertEqual(report["passed"], 2)
        self.assertEqual(report["failed"], 1)
        self.assertEqual([case["ok"] for case in report["cases"]], [True, True, False])
        self.assertEqual(report["cases"][2]["actual"], {"n": 1})

    def test_js_flavor_runs_through_v8(self):
        report = run_code_tests(action(
            "return {route: input.route, n: (input.attachments || []).length}",
            [
                {"name": "none", "input": {"route": "invoice"}, "expected": {"route": "invoice", "n": 0}},
                {"name": "one", "input": {"route": "invoice", "attachments": [{}]},
                 "expected": {"route": "invoice", "n": 1}},
            ],
            action_type="js",
        ))
        self.assertEqual(report["passed"], 2)

    def test_expected_error_matches_substring(self):
        report = run_code_tests(action(
            'raise ValueError(f"expected at most one attachment, got {len(input["a"])}")',
            [
                {"name": "two", "input": {"a": [1, 2]}, "expected_error": "got 2"},
                {"name": "wrong message", "input": {}, "expected_error": "got 0 attachments"},
                {"name": "fails as expected", "input": {"a": [1, 2, 3]}, "expected_error": "got 3"},
            ],
        ))
        self.assertEqual([case["ok"] for case in report["cases"]], [True, False, True])
        self.assertIn("code step failed: ValueError", report["cases"][0]["error"])

    def test_expected_error_but_step_succeeds_fails(self):
        report = run_code_tests(action("output = 'fine'", [
            {"expected_error": "boom"}]))
        self.assertEqual(report["failed"], 1)
        self.assertIn("expected a failure", report["cases"][0]["error"])

    def test_case_without_expectation_only_asserts_it_runs(self):
        report = run_code_tests(action("print(input.get('x'))", [
            {"name": "runs", "input": {"x": 1}},
            {},
        ]))
        self.assertEqual(report["passed"], 2)

    def test_snippet_errors_report_the_runtime_message(self):
        report = run_code_tests(action("input['missing']", [{"name": "raises"}]))
        self.assertEqual(report["failed"], 1)
        self.assertIn("code step failed: KeyError", report["cases"][0]["error"])

    def test_timeout_is_reported_as_failure(self):
        # In a subprocess: the timed-out snippet's V8 isolate (or the Python
        # daemon thread) keeps spinning and would flake later snippets.
        code = (
            "import os\n"
            "from src.dapier.engine.code_tests import run_code_tests\n"
            "report = run_code_tests({'type': 'code', 'code': 'while True:\\n    pass',"
            " 'timeout_seconds': 0.5, 'tests': [{'expected': 1}]})\n"
            "assert report['failed'] == 1, report\n"
            "assert 'timed out' in report['cases'][0]['error'], report\n"
            # os._exit skips interpreter finalization: the abandoned spinner
            # would otherwise deadlock the exit in py_mini_racer/__del__ paths.
            "os._exit(0)\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True,
            env={**os.environ, "PYTHONPATH": "."}, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_stdout_rides_along(self):
        report = run_code_tests(action("print('hi')\noutput = 1", [{"expected": 1}]))
        self.assertEqual(report["cases"][0]["stdout"], "hi\n")


class CodeTestsValidationTests(unittest.TestCase):
    def test_rejects_empty_and_non_list(self):
        for bad in ([], "yes", {}):
            with self.assertRaises(CodeTestsError):
                validate_code_tests({"type": "code", "code": "1", "tests": bad})

    def test_rejects_unknown_case_keys(self):
        with self.assertRaises(CodeTestsError):
            validate_code_tests(action("1", [{"input": {}, "when": "x"}]))

    def test_rejects_non_object_input(self):
        with self.assertRaises(CodeTestsError):
            validate_code_tests(action("1", [{"input": [1, 2]}]))

    def test_rejects_expected_and_expected_error_together(self):
        with self.assertRaises(CodeTestsError):
            validate_code_tests(action("1", [{"expected": 1, "expected_error": "x"}]))

    def test_rejects_non_string_expected_error(self):
        with self.assertRaises(CodeTestsError):
            validate_code_tests(action("1", [{"expected_error": 5}]))

    def test_rejects_more_than_fifty_cases(self):
        with self.assertRaises(CodeTestsError):
            validate_code_tests(action("1", [{"input": {}}] * 51))

    def test_registry_rejects_tests_on_other_action_types(self):
        from src.dapier.connectors.registry import ActionError, validate_action_chain

        with self.assertRaises(ActionError):
            validate_action_chain([
                {"id": "s", "type": "date_time", "value": "{date}",
                 "tests": [{"input": {}}]},
            ])

    def test_registry_accepts_valid_tests_block(self):
        from src.dapier.connectors.registry import validate_action_chain

        validate_action_chain([
            {"id": "s", "type": "code", "code": "len(input['a'])",
             "tests": [{"name": "one", "input": {"a": "x"}, "expected": 1}]},
        ])

    def test_registry_rejects_malformed_case(self):
        from src.dapier.connectors.registry import ActionError, validate_action_chain

        with self.assertRaises(ActionError):
            validate_action_chain([
                {"id": "s", "type": "code", "code": "1",
                 "tests": [{"expected": 1, "expected_error": "x"}]},
            ])


if __name__ == "__main__":
    unittest.main()
