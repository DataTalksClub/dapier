# LLM connections

The plugin owns connection verification and authentication for `ai_complete`.
Choose Anthropic OIDC or a stored API key in Console → Connections → LLM.
Save verifies authentication and model availability before writing a record.
Console **Test** and `dapier connections test llm` use the same API behavior.
Test sends a small completion to the configured model and shows its actual reply.
It uses the same authentication and inference path as workflows, with a 32-token
output limit. Testing can incur a small provider charge.
`dapier connections test openrouter` tests the typed jev decision endpoint and
shows the returned choice.

Anthropic exchanges the worker's AWS identity for a short-lived token, cached
only in worker memory and refreshed before expiry. API tests execute on that
worker so they use the federation rule's trusted IAM role. Keyed providers keep
keys and optional custom headers in the connection's credential record.

Use `dapier connections import llm --provider llm --config-file config.json`
to create or update a connection. The configuration formats and verified
federation identifiers are in [AI configuration](../../docs/connectors/ai.md)
and [Anthropic workload identity](../../docs/anthropic-workload-identity.md).
The workflow field is `connection_id` and defaults to `llm`.

Run this plugin's tests with `make plugin-test PLUGIN=llm`.
