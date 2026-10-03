"""Code-block tests: named cases attached to a code/js action.

A ``code`` or ``js`` action may carry a ``tests`` list; each case runs the
snippet against its own ``input`` as the event data and checks the outcome:

    - id: triage
      type: code
      code: |
        ...
      tests:
      - name: no attachments
        input:
          attachments: []
        expected:
          has_attachment: false
      - name: two attachments fail
        input:
          attachments: [{}, {}]
        expected_error: "2 attachments"

A case with ``expected`` asserts the step result equals it deep-equal —
against the same JSON-safe value the run history records (same capping). A
case with ``expected_error`` asserts the step fails with that substring in
the error. A case with neither only asserts the snippet runs cleanly.
``input`` defaults to ``{}``; ``name`` defaults to the case's position.

Tests are pure: no connections are touched and nothing is recorded to the
executions table — the same contract as the dry-run, at snippet granularity.
They run through the real ``run_code``/``run_js`` runners, so sandbox,
timeouts and error shaping are exactly what a live run sees.
"""
from .actions.code import run_code, run_js

_MAX_CASES = 50


class CodeTestsError(ValueError):
    """The ``tests`` block itself is malformed (save time and run time)."""


def _runner(action):
    return run_js if action.get("type") == "js" else run_code


def validate_code_tests(action):
    """Raise :class:`CodeTestsError` unless ``tests`` is a usable case list.

    Shared by the save-time validator (registry.validate_action_chain) and
    the run, so both surfaces reject a bad block with the same message.
    """
    tests = action.get("tests")
    if tests is None or (isinstance(tests, str) and not tests.strip()):
        raise CodeTestsError("code tests: 'tests' must be a non-empty list of cases")
    if not isinstance(tests, list) or not tests:
        raise CodeTestsError("code tests: 'tests' must be a non-empty list of cases")
    if len(tests) > _MAX_CASES:
        raise CodeTestsError(f"code tests: at most {_MAX_CASES} cases per action")
    for index, case in enumerate(tests):
        label = f"code tests case {index + 1}"
        if not isinstance(case, dict):
            raise CodeTestsError(f"{label}: each case must be an object")
        unknown = sorted(set(case) - {"name", "input", "expected", "expected_error"})
        if unknown:
            raise CodeTestsError(f"{label}: unknown keys: {', '.join(unknown)}")
        if "input" in case and case["input"] is not None and not isinstance(case["input"], dict):
            raise CodeTestsError(f"{label}: 'input' must be an object")
        if "expected" in case and "expected_error" in case:
            raise CodeTestsError(f"{label}: 'expected' and 'expected_error' are mutually exclusive")
        name = case.get("name")
        if name is not None and not isinstance(name, str):
            raise CodeTestsError(f"{label}: 'name' must be a string")
        if "expected_error" in case and not isinstance(case.get("expected_error"), str):
            raise CodeTestsError(f"{label}: 'expected_error' must be a string")


def run_code_tests(action):
    """Run every case; returns ``{total, passed, failed, cases}``.

    Each case reports ``{name, ok, expected, actual, error, stdout}`` with
    the non-meaningful fields None, so the designer and the CLI can render
    one uniform shape. The action's own ``tests`` key rides along into the
    runners harmless — they read only ``code`` and ``timeout_seconds``.
    """
    validate_code_tests(action)
    runner = _runner(action)
    cases = []
    for index, case in enumerate(action["tests"]):
        name = str(case.get("name") or f"case {index + 1}")
        expected_error = case.get("expected_error")
        has_expected = "expected" in case
        expected = case.get("expected")
        event = {"data": case.get("input") or {}}
        actual = error = stdout = None
        try:
            output = runner(action, event)
        except RuntimeError as exc:
            error = str(exc)
            ok = expected_error is not None and str(expected_error) in error
        else:
            actual, stdout = output.get("result"), output.get("stdout")
            if expected_error is not None:
                ok = False
                error = f"expected a failure matching {expected_error!r}, but the step succeeded"
            elif not has_expected:
                ok = True
            else:
                ok = actual == expected
        cases.append({
            "name": name,
            "ok": bool(ok),
            "expected": expected,
            "actual": actual,
            "error": error,
            "stdout": stdout,
        })
    passed = sum(1 for case in cases if case["ok"])
    return {"total": len(cases), "passed": passed,
            "failed": len(cases) - passed, "cases": cases}
