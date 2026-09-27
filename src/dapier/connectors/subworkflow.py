"""Sub-workflow connector: run another published workflow in-process."""
from ..engine.actions.subworkflow import run_workflow
from .registry import Action, register

register(Action(
    type="run_workflow",
    label="Run workflow",
    icon="workflow",
    description="Run another published workflow in-process and expose its step outputs "
                "to later templating (nested sub-runs cap at depth 2; cycles are rejected)",
    run=lambda action, event, workflow_id, steps=None: run_workflow(action, event, workflow_id, steps=steps),
    required=frozenset({"workflow_id"}),
    optional=frozenset({"payload", "output_field"}),
    fields=(
        {"key": "workflow_id", "label": "Workflow ID", "required": True,
         "placeholder": "invoice-notify"},
        {"key": "payload", "label": "Payload (JSON or template)", "type": "textarea",
         "help": "The event data the sub-run starts from: a JSON object (with {tokens} "
                 "rendered against this event), or any other value wrapped as "
                 "{\"payload\": ...}. Omitted, the triggering event's data passes through.",
         "placeholder": "{\"subject\": \"{subject}\"}"},
        {"key": "output_field", "label": "Output field",
         "help": "Name a step of the target workflow and its output becomes "
                 "{steps.<id>.output.output.<field>}; without it every step's output "
                 "sits under {steps.<id>.output.steps.<step>}.",
         "placeholder": "notify"},
    ),
))
