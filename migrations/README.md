# Legacy workflow cutover

`legacy-workflows/` is a one-time migration input, not a runtime catalog. The
engine and console now read workflow definitions from `PublishedWorkflowsTable`.

The deploy workflow runs this migration against the existing stack **before**
deploying the published-only code. It copies missing workflow definitions, expands the five
shared action chains into their workflows and stored triggers, and preserves
existing published edits, trigger credentials, enabled flags, and identities.
The command is repeatable after a partial failure. It can also be run manually
with AWS credentials that can read the CloudFormation stack and edit the five
workflow and trigger tables:

```bash
uv run --with boto3 --with pyyaml python -m scripts.migrate_legacy_workflows
uv run --with boto3 --with pyyaml python -m scripts.migrate_legacy_workflows --apply
uv run --with boto3 --with pyyaml python -m scripts.migrate_legacy_workflows
```

The final plan must report zero workflows to publish and zero flow references
to convert. The API and CLI both edit the managed store;
Git sync is optional and only records a copy when configured.
