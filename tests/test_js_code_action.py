"""js action: sandboxed JavaScript transforms, executed by embedded V8."""
import os
import subprocess
import sys
import unittest

from src.dapier.engine.actions.code import run_js


def run(source, data=None, **action):
    action.setdefault("type", "js")
    action["code"] = source
    return run_js(action, {"data": data or {}})


class JsCodeStepTests(unittest.TestCase):
    def test_return_value_is_the_result(self):
        output = run(
            "return {upper: input.title.toUpperCase(), n: input.title.length}",
            {"title": "invoice"},
        )
        self.assertEqual(output["result"], {"upper": "INVOICE", "n": 7})

    def test_modern_syntax_works(self):
        output = run(
            "const items = input.items || [];\n"
            "return items.filter(i => i.score > 10).map(i => i.name);",
            {"items": [{"name": "a", "score": 20}, {"name": "b", "score": 3}]},
        )
        self.assertEqual(output["result"], ["a"])

    def test_no_return_is_null(self):
        self.assertIsNone(run("console.log('side effect only')")["result"])

    def test_console_log_is_captured(self):
        output = run("console.log('hello', 'world')\nconsole.log({a: 1})\nreturn true")
        self.assertEqual(output["stdout"], 'hello world\n{"a":1}')
        self.assertTrue(output["result"])

    def test_thrown_error_fails_the_step(self):
        with self.assertRaises(RuntimeError) as ctx:
            run('throw new Error("boom")')
        self.assertIn("code step failed", str(ctx.exception))
        self.assertIn("boom", str(ctx.exception))

    def test_runtime_error_fails_the_step(self):
        with self.assertRaises(RuntimeError) as ctx:
            run("return input.missing.deep")
        self.assertIn("code step failed", str(ctx.exception))
        self.assertIn("TypeError", str(ctx.exception))

    def test_syntax_error_fails_the_step(self):
        with self.assertRaises(RuntimeError) as ctx:
            run("function broken({\n  return 1")
        message = str(ctx.exception)
        self.assertIn("code step failed", message)

    def test_timeout_fails_the_step(self):
        # In a subprocess: the timed-out V8 isolate keeps spinning (a daemon
        # thread is abandoned, not killed), and a live spinner in this
        # process flakes every later snippet under load.
        code = (
            "import os\n"
            "from src.dapier.engine.actions.code import run_js\n"
            "try:\n"
            "    run_js({'type': 'js', 'code': 'while (true) {}',"
            " 'timeout_seconds': 0.5}, {'data': {}})\n"
            "except RuntimeError as exc:\n"
            "    assert 'timed out after 0.5s' in str(exc), exc\n"
            "else:\n"
            "    raise AssertionError('the runaway snippet did not time out')\n"
            # os._exit skips interpreter finalization: finalizing the stuck
            # isolate deadlocks in py_mini_racer's __del__ (the daemon thread
            # is abandoned spinning, never killed).
            "os._exit(0)\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True,
            env={**os.environ, "PYTHONPATH": "."}, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_no_host_apis_reach_the_snippet(self):
        output = run("return [typeof fetch, typeof require, typeof process]")
        self.assertEqual(output["result"], ["undefined", "undefined", "undefined"])

    def test_oversized_stdout_is_capped(self):
        output = run("console.log('x'.repeat(100000))")
        self.assertLess(len(output["stdout"]), 3000)
        self.assertIn("truncated", output["stdout"])

    def test_oversized_result_keeps_a_preview(self):
        output = run("return {blob: 'y'.repeat(100000)}")
        self.assertTrue(output["result"]["truncated"])
        self.assertIn("blob", output["result"]["preview"])


class JsCatalogAndValidationTests(unittest.TestCase):
    def test_registry_has_the_js_step(self):
        from src.dapier.connectors import registry

        entry = registry.ACTIONS["js"]
        self.assertEqual(entry.required, frozenset({"code"}))
        self.assertEqual(entry.optional, frozenset({"timeout_seconds", "tests"}))

    def test_catalog_lists_the_js_step(self):
        from src.dapier.connectors import catalog

        self.assertIn("js", [entry["type"] for entry in catalog()["actions"]])

    def test_dispatch_goes_through_the_registry(self):
        from src.dapier.connectors import registry

        output = registry.run_action({"type": "js", "code": "return 6 * 7"}, {"data": {}})
        self.assertEqual(output, {"result": 42, "stdout": ""})

    def test_validation_accepts_pipes_and_braces_in_source(self):
        from src.dapier.connectors import validate_action_chain

        chain = [{"type": "js", "code": "return input.a || 'default'"}]
        self.assertEqual(validate_action_chain(chain), chain)

    def test_validation_still_rejects_unknown_keys(self):
        from src.dapier.connectors.registry import ActionError, validate_action_chain

        with self.assertRaises(ActionError) as ctx:
            validate_action_chain([{"type": "js", "code": "return 1", "nope": 1}])
        self.assertIn("nope", str(ctx.exception))

    def test_dry_run_support_follows_the_registry(self):
        from src.dapier.engine import dryrun

        supported = dryrun._supported_action_types()
        self.assertIn("js", supported)
        self.assertIn("code", supported)
        self.assertNotIn("mystery_action", supported)

    def test_code_key_is_exempt_from_template_validation(self):
        from src.dapier.engine.actions import templating

        templating.validate_action({"type": "js", "code": "return input.a || '{x|nope}'"})
        with self.assertRaises(templating.TemplateError):
            templating.validate_action({"type": "webhook", "url": "{x|nope}"})


if __name__ == "__main__":
    unittest.main()
