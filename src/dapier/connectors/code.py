"""Code connector: sandboxed Python and JavaScript transforms as actions."""
from ..engine.actions.code import run_code, run_js
from .registry import Action, register

register(Action(
    type="code",
    label="Code (Python)",
    icon="code-2",
    description=("Sandboxed Python transform: the event data arrives as `input`; "
                 "the last expression (or an `output` variable) becomes the step result."),
    run=lambda action, event, workflow_id, steps=None: run_code(action, event),
    required=frozenset({"code"}),
    optional=frozenset({"timeout_seconds", "tests"}),
    fields=(
        {"key": "code", "label": "Python source", "type": "textarea", "required": True,
         "placeholder": '# event data is `input`; last expression is the result\n{"route": input["route"], "score": len(input.get("body", ""))}'},
        {"key": "timeout_seconds", "label": "Timeout (s)", "type": "number"},
    ),
))

register(Action(
    type="js",
    label="Code (JavaScript)",
    icon="braces",
    description=("Sandboxed JavaScript transform (embedded V8): the event data arrives as `input`; "
                 "`return` a value to make it the step result. console.log is captured."),
    run=lambda action, event, workflow_id, steps=None: run_js(action, event),
    required=frozenset({"code"}),
    optional=frozenset({"timeout_seconds", "tests"}),
    fields=(
        {"key": "code", "label": "JavaScript source", "type": "textarea", "required": True,
         "placeholder": '// event data is `input`; return the result\nreturn {route: input.route, score: (input.items || []).length}'},
        {"key": "timeout_seconds", "label": "Timeout (s)", "type": "number"},
    ),
))
