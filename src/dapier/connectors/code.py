"""Code connector: a sandboxed Python transform as a connector action."""
from ..engine.actions.code import run_code
from .registry import Action, register

register(Action(
    type="code",
    label="Code (Python)",
    icon="code-2",
    description=("Sandboxed Python transform: the event data arrives as `input`; "
                 "the last expression (or an `output` variable) becomes the step result."),
    run=lambda action, event, workflow_id, steps=None: run_code(action, event),
    required=frozenset({"code"}),
    optional=frozenset({"timeout_seconds"}),
    fields=(
        {"key": "code", "label": "Python source", "type": "textarea", "required": True,
         "placeholder": '# event data is `input`; last expression is the result\n{"route": input["route"], "score": len(input.get("body", ""))}'},
        {"key": "timeout_seconds", "label": "Timeout (s)", "type": "number"},
    ),
))
