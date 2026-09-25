"""code action: sandboxed Python transforms (issue #12)."""
import unittest
from unittest.mock import patch

from src.dapier.engine import execute, run_code


def run(source, data=None, **action):
    action.setdefault("type", "code")
    action["code"] = source
    return run_code(action, {"data": data or {}})


class CodeStepTests(unittest.TestCase):
    def test_transforms_input_into_output(self):
        output = run(
            '{"upper": input["title"].upper(), "n": len(input["title"])}',
            {"title": "invoice"},
        )
        self.assertEqual(output["result"], {"upper": "INVOICE", "n": 7})

    def test_last_expression_is_the_result(self):
        self.assertEqual(run("[x * 2 for x in range(3)]")["result"], [0, 2, 4])
        self.assertEqual(run("1 + 1")["result"], 2)

    def test_output_variable_when_the_tail_is_a_statement(self):
        output = run("output = {k: v for k, v in input.items() if v}\noutput", {"keep": 1, "drop": 0})
        self.assertEqual(output["result"], {"keep": 1})

    def test_bare_output_assignment_without_trailing_expression(self):
        output = run("output = {'sum': sum(input['values'])}", {"values": [1, 2, 3]})
        self.assertEqual(output["result"], {"sum": 6})

    def test_result_variable_alias(self):
        self.assertEqual(run("result = 'ok'")["result"], "ok")

    def test_no_result_is_null(self):
        self.assertIsNone(run("print('side effect only')")["result"])

    def test_allowed_module_imports_work(self):
        output = run(
            "import json, re, hashlib\n"
            "match = re.search(r'\\d+', input['s'])\n"
            "output = {'digits': match.group(), 'md5': hashlib.md5(b'x').hexdigest()[:4]}",
            {"s": "abc123"},
        )
        self.assertEqual(output["result"]["digits"], "123")
        self.assertEqual(len(output["result"]["md5"]), 4)

    def test_blocked_import_fails_the_step(self):
        with self.assertRaises(RuntimeError) as ctx:
            run("import os")
        self.assertIn("not allowed", str(ctx.exception))
        self.assertIn("os", str(ctx.exception))

        with self.assertRaises(RuntimeError) as ctx:
            run("import socket")
        self.assertIn("socket", str(ctx.exception))

    def test_dunder_import_is_not_available(self):
        # __import__ exists but is the guarded one: os is refused like a
        # plain `import os`.
        with self.assertRaises(RuntimeError) as ctx:
            run('__import__("os").getcwd()')
        self.assertIn("not allowed", str(ctx.exception))

    def test_open_and_eval_are_not_available(self):
        for snippet in ("open('/etc/passwd')", "eval('1+1')", "exec('x=1')"):
            with self.assertRaises(RuntimeError, msg=snippet) as ctx:
                run(snippet)
            self.assertIn("NameError", str(ctx.exception))

    def test_runtime_error_propagates_with_line_number(self):
        with self.assertRaises(RuntimeError) as ctx:
            run("value = 1\nvalue / 0")
        message = str(ctx.exception)
        self.assertIn("ZeroDivisionError", message)
        self.assertIn("line 2", message)

    def test_syntax_error_names_the_line(self):
        with self.assertRaises(RuntimeError) as ctx:
            run("def broken(:\n    pass")
        self.assertIn("SyntaxError", str(ctx.exception))
        self.assertIn("line 1", str(ctx.exception))

    def test_stdout_is_captured(self):
        output = run("print('hello', 'world')\nprint('second line')\n{'done': True}")
        self.assertEqual(output["stdout"], "hello world\nsecond line\n")
        self.assertEqual(output["result"], {"done": True})

    def test_timeout_fails_the_step(self):
        with self.assertRaises(RuntimeError) as ctx:
            run(
                "import time\ntime.sleep(30)",
                timeout_seconds=0.5,
            )
        self.assertIn("timed out after 0.5s", str(ctx.exception))

    def test_timeout_is_clamped(self):
        from src.dapier.engine.actions import code

        self.assertEqual(code._timeout_seconds({"timeout_seconds": 999}), code.MAX_TIMEOUT_SECONDS)
        self.assertEqual(code._timeout_seconds({"timeout_seconds": 0}), code.MIN_TIMEOUT_SECONDS)
        self.assertEqual(code._timeout_seconds({}), code.DEFAULT_TIMEOUT_SECONDS)
        with self.assertRaises(ValueError):
            code._timeout_seconds({"timeout_seconds": "soon"})

    def test_oversized_stdout_is_capped(self):
        output = run("print('x' * 100_000)")
        self.assertLess(len(output["stdout"]), 3000)
        self.assertIn("truncated", output["stdout"])

    def test_oversized_result_keeps_a_preview(self):
        output = run("{'blob': 'y' * 100_000}")
        self.assertTrue(output["result"]["truncated"])
        self.assertIn("blob", output["result"]["preview"])

    def test_output_parts_stay_under_the_worker_trim_limit(self):
        import json

        from src.dapier.engine.worker import _trim

        output = run(
            "print('x' * 100_000)\ninput"
        )
        self.assertLessEqual(len(json.dumps(_trim(output), default=str)), 6000)

    def test_non_serializable_result_is_stringified(self):
        output = run("{1, 2, 3}")
        self.assertEqual(output["result"], "{1, 2, 3}")

    def test_empty_or_missing_code_fails_fast(self):
        for action in ({"type": "code"}, {"type": "code", "code": ""}, {"type": "code", "code": "  \n"},
                       {"type": "code", "code": 42}):
            with self.assertRaises(ValueError, msg=action):
                run_code(action, {"data": {}})

    def test_none_result_and_empty_input_are_defaults(self):
        output = run("{'keys': list(input)}")
        self.assertEqual(output["result"], {"keys": []})


class CodeDispatchTests(unittest.TestCase):
    """execute() runs code steps like any other action and records the result."""

    WORKFLOW = {
        "id": "wf-code", "enabled": True,
        "trigger": {"connector": "email", "event": "message.received", "filters": {}},
        "actions": [{"id": "shape", "type": "code", "code": "{'route': input['route']}"}],
    }

    def test_execute_runs_the_code_step_and_records_its_output(self):
        seen = {}
        hooks = {
            "before_action": lambda *args: True,
            "after_action": lambda wf, action_id, event, **kw: seen.update(kw),
        }
        event = {"id": "evt-code", "connector": "email", "event": "message.received",
                 "data": {"route": "todo"}}
        with patch("src.dapier.engine.all_workflows", return_value=[self.WORKFLOW]):
            execute(event, **hooks)
        self.assertEqual(seen["output"], {"result": {"route": "todo"}, "stdout": ""})

    def test_failing_code_step_fails_the_run(self):
        workflow = {
            **self.WORKFLOW,
            "actions": [{"id": "boom", "type": "code", "code": "1 / 0"}],
        }
        errors = []
        hooks = {
            "before_action": lambda *args: True,
            "on_action_error": lambda *args, **kw: errors.append(str(args[3])),
        }
        event = {"id": "evt-boom", "connector": "email", "event": "message.received",
                 "data": {}}
        with patch("src.dapier.engine.all_workflows", return_value=[workflow]), \
             self.assertRaises(RuntimeError):
            execute(event, **hooks)
        self.assertEqual(len(errors), 1)
        self.assertIn("ZeroDivisionError", errors[0])


if __name__ == "__main__":
    unittest.main()
