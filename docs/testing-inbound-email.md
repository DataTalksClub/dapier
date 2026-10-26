# Testing inbound email workflows

Use the workflow test API for synthetic samples. The same behavior is available
from the Console designer's Test action and the CLI:

```bash
dapier workflows test workflow.yaml --event @sample.json
```

The default is a dry-run. Add `--execute` to run actions only when those side
effects are authorized. The CLI calls `POST /api/agent/designer/workflows/test`
with `yaml`, `event`, and `execute` fields. The Console calls the corresponding
admin route; both use the shared workflow tester. Inline tests do not enter the
live trigger queue or run the mailbox sender gate.

Never use SQS SendMessage, direct live-worker invocation, or SNS publishing to
simulate a receipt. A fabricated TEST receipt sent to EventQueue on 25 October
2026 triggered the operator sender-refusal alarm even though it was not mail.
Keep sender allowlists unchanged when testing. Inbox and run replay execute an
existing delivery again and can cause live side effects.

The worker now checks inbound-email provenance before normalization, raw MIME
reads, attachment staging, sender policy, or matching. Direct queue messages
require an AWS SQS SenderId for the intake, API replay, or worker retry role.
SNS notifications require a valid AWS signature and a configured inbound topic.
Event IDs, S3 pointers, and message attributes cannot establish provenance.
Untrusted inbound events are recorded as ignored with an invalid-provenance
error and a WARNING log; no workflow runs and no refusal metric is emitted.

Genuine non-allowlisted mail still logs ERROR and emits the existing deduplicated
EmailSenderPolicyRefused metric. A minute schedule adds zero values to this
metric: hourly Sum preserves every refusal, while regular data prevents
CloudWatch's sparse-metric evaluation lookback from retaining an expired
refusal. Threshold, period, evaluation count, missing-data policy, and alarm
actions are unchanged. See AWS's
[missing-data evaluation documentation](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/alarms-and-missing-data.html).
