"""code action: run a sandboxed Python snippet over the event data.

The snippet transforms the triggering event and returns JSON for later steps
(issue #12). Contract:

- the snippet runs with the event's ``data`` payload bound to the name
  ``input`` (a dict);
- the step output is the snippet's last top-level *expression*, falling back
  to a variable named ``output`` (``result`` is accepted as an alias); with
  neither, the result is ``null``;
- anything the snippet prints is captured and reported alongside the result.

Sandbox (best-effort — see "Threat model"): the snippet executes with a
restricted ``__builtins__`` — no ``open``, ``eval``, ``exec``, ``compile``,
``getattr``/``setattr``, ``globals``/``locals`` or ``input``; ``import`` is
rewired to a guard that serves only a fixed allowlist of side-effect-free
standard-library modules::

    base64, collections, datetime, decimal, hashlib, itertools, json, math,
    random, re, statistics, string, time, uuid

``os``, ``sys``, ``subprocess``, ``socket``, ``urllib``, ``requests`` and
everything else raise ImportError.

Threat model: this guards workflow authors against accidents and casual
unsafe access, not determined adversaries — attribute tricks (``().__class__``)
cannot be fully closed in-process, and workflow authors can already commit
arbitrary YAML to the workflows repo. A hard boundary would need a separate
runtime; v1 is Python-only (the Lambda image is python3.12, no Node).

Limits: the snippet runs on a daemon thread and fails after
``timeout_seconds`` (default 5, clamped to [0.5, 15]); a timed-out thread is
abandoned, not killed. Result and captured stdout are each JSON-capped in the
style of worker._trim (per-part 2500 chars, so the combined ``{"result",
"stdout"}`` output stays under the worker's 6000-char run-history cap and
keeps its shape). Exceptions fail the step with a one-line error carrying the
exception type, message and snippet line number, which is what the run view
records.
"""
import ast
import importlib
import io
import json
import threading

DEFAULT_TIMEOUT_SECONDS = 5
MIN_TIMEOUT_SECONDS = 0.5
MAX_TIMEOUT_SECONDS = 15
# Per-part cap, half of worker._trim's 6000 so result + stdout stay under it.
_OUTPUT_PART_LIMIT = 2500

ALLOWED_MODULES = frozenset({
    "base64", "collections", "datetime", "decimal", "hashlib", "itertools",
    "json", "math", "random", "re", "statistics", "string", "time", "uuid",
})


def _safe_builtins():
    """The builtin names a snippet may see; deliberately exclude every name
    that reaches the interpreter, the filesystem or the process."""
    import builtins

    names = [
        # numbers, sequences, iteration
        "abs", "all", "any", "bool", "bytes", "callable", "chr", "complex",
        "dict", "divmod", "enumerate", "filter", "float", "format",
        "frozenset", "hash", "hex", "int", "isinstance", "issubclass",
        "iter", "len", "list", "map", "max", "min", "next", "oct", "ord",
        "pow", "range", "repr", "reversed", "round", "set", "slice",
        "sorted", "str", "sum", "tuple", "type", "zip",
        # exceptions a transform may want to catch or raise
        "ArithmeticError", "AssertionError", "AttributeError", "Exception",
        "IndexError", "KeyError", "LookupError", "NameError", "RuntimeError",
        "StopIteration", "TypeError", "ValueError", "ZeroDivisionError",
    ]
    return {name: getattr(builtins, name) for name in names}


def _guarded_import(name, _globals=None, _locals=None, fromlist=(), level=0):
    """``__import__`` for snippets: allowlist, then the real machinery."""
    root = name.split(".")[0]
    if root not in ALLOWED_MODULES:
        raise ImportError(
            f"module '{name}' is not allowed in code steps "
            f"(allowed: {', '.join(sorted(ALLOWED_MODULES))})"
        )
    return importlib.import_module(name)


def _sandbox_for(input_value, stdout):
    """Globals for one snippet run: ``input`` in, restricted builtins, and a
    ``print`` that captures instead of touching the process stdout."""
    def _print(*values, sep=" ", end="\n"):
        stdout.write(sep.join(str(value) for value in values) + end)

    builtins = _safe_builtins()
    builtins["__import__"] = _guarded_import
    builtins["print"] = _print
    return {"__builtins__": builtins, "input": input_value}


def _compile_snippet(source):
    """Compile the snippet; split a trailing expression so its value can be
    evaluated as the step result (exec alone discards it)."""
    try:
        module = ast.parse(source, "<code step>")
    except SyntaxError as exc:
        raise RuntimeError(
            f"code step failed: SyntaxError: {exc.msg} (line {exc.lineno})"
        ) from exc
    tail = None
    if module.body and isinstance(module.body[-1], ast.Expr):
        tail = compile(ast.Expression(module.body.pop().value), "<code step>", "eval")
    try:
        return compile(module, "<code step>", "exec"), tail
    except (SyntaxError, ValueError) as exc:
        raise RuntimeError(f"code step failed: {type(exc).__name__}: {exc}") from exc


def _snippet_line(exc):
    """The innermost snippet frame's line number, for the error message."""
    tb = exc.__traceback__
    line = None
    while tb is not None:
        if tb.tb_frame.f_code.co_filename == "<code step>":
            line = tb.tb_lineno
        tb = tb.tb_next
    return line


def _error_message(exc):
    location = _snippet_line(exc)
    suffix = f" (line {location})" if location else ""
    return f"code step failed: {type(exc).__name__}: {exc}{suffix}"


def _run(sandbox, exec_code, tail_code, outcome):
    """The thread body: exec the snippet, then eval the trailing expression
    (or fall back to an ``output``/``result`` variable) into ``outcome``."""
    try:
        exec(exec_code, sandbox)  # noqa: S102 - the sandbox is the point
        if tail_code is not None:
            outcome["result"] = eval(tail_code, sandbox)  # noqa: S307
        else:
            outcome["result"] = sandbox.get("output", sandbox.get("result"))
    except BaseException as exc:  # noqa: BLE001 - every failure is a step failure
        outcome["error"] = exc


def _timeout_seconds(action):
    raw = action.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    try:
        timeout = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"code action has an invalid timeout_seconds: {raw!r}") from None
    return min(max(timeout, MIN_TIMEOUT_SECONDS), MAX_TIMEOUT_SECONDS)


def _json_safe(value, limit=_OUTPUT_PART_LIMIT):
    """JSON-safe copy of the snippet result; oversized values keep a preview,
    matching worker._trim."""
    try:
        text = json.dumps(value, default=str)
    except (TypeError, ValueError):
        text = json.dumps(str(value))
    if len(text) > limit:
        return {"truncated": True, "preview": text[:limit]}
    return json.loads(text)


def _capped_text(text, limit=_OUTPUT_PART_LIMIT):
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[truncated, {len(text)} chars total]"


def run_code(action, event):
    """Execute the snippet and return what happened for the run record."""
    source = action.get("code")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("code action needs a non-empty 'code' source")
    timeout = _timeout_seconds(action)
    exec_code, tail_code = _compile_snippet(source)

    stdout = io.StringIO()
    sandbox = _sandbox_for(event.get("data") or {}, stdout)
    outcome = {}
    thread = threading.Thread(
        target=_run, args=(sandbox, exec_code, tail_code, outcome),
        name="dapier-code-step", daemon=True,
    )
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise RuntimeError(
            f"code step timed out after {timeout:g}s (limit {MAX_TIMEOUT_SECONDS:g}s)"
        )
    if "error" in outcome:
        raise RuntimeError(_error_message(outcome["error"]))

    return {
        "result": _json_safe(outcome.get("result")),
        "stdout": _capped_text(stdout.getvalue()),
    }
