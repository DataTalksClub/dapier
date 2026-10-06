"""Bounded read-only evidence for the connections-list 500.

The console's Connections register and `dapier connections list` fail with
HTTP 500 while every other overview section answers. Both share exactly one
resource: the connections table (env var CONNECTIONS_TABLE, GetItem/Scan on
ConnectionsTable). This script gathers, without printing any secret:

- the API function's table/queue environment variable NAMES and values
  (table names only — the Variables block also carries OAuth/LLM secrets,
  which are never touched);
- the Ingress role's inline policy names, and whether the applied documents
  grant dynamodb GetItem/Scan on the connections table;
- the last bounded page of ERROR/Traceback events from the function's log
  group, truncated — the actual traceback beats every hypothesis.

Access denial on any single probe is reported as explicitly unverified.
"""
import json
import time

import boto3
from botocore.exceptions import ClientError

THRESHOLD_HOURS = 14
MAX_LOG_EVENTS = 6
MAX_EVENT_CHARS = 700


def inspect(cf, iam, lam, logs, ddb):
    proof = {}

    def physical(logical):
        return cf.describe_stack_resource(
            StackName="dapier", LogicalResourceId=logical
        )["StackResourceDetail"]["PhysicalResourceId"]

    fn = physical("IngressFunction")
    role = physical("IngressFunctionRole")
    connections_table = physical("ConnectionsTable")
    proof["resources"] = {"function": fn, "role": role, "connectionsTable": connections_table}

    def read(name, callback):
        try:
            proof[name] = callback()
        except ClientError as error:
            code = error.response["Error"]["Code"]
            if code not in ("AccessDenied", "AccessDeniedException", "UnauthorizedOperation"):
                raise RuntimeError(f"{name}: AWS {code}") from None
            proof[name] = {"unverified": True, "awsErrorCode": code}

    def env_tables():
        variables = lam.get_function_configuration(FunctionName=fn)["Environment"]["Variables"]
        return {k: v for k, v in sorted(variables.items()) if "TABLE" in k or "QUEUE" in k}

    read("functionTableEnv", env_tables)

    def role_policies():
        names = iam.list_role_policies(RoleName=role)["PolicyNames"]
        found = []
        for name in names:
            document = iam.get_role_policy(RoleName=role, PolicyName=name)["PolicyDocument"]
            text = json.dumps(document, default=str)
            if "ConnectionsTable" in text or connections_table in text:
                statements = document.get("Statement", [])
                if isinstance(statements, dict):
                    statements = [statements]
                hits = [
                    s for s in statements
                    if "dynamodb" in json.dumps(s.get("Action", []))
                    and connections_table in json.dumps(s.get("Resource", []))
                ]
                found.append({"policy": name, "dynamodbOnConnectionsTable": hits})
        return found

    read("roleConnectionsTablePolicies", role_policies)

    def table_exists():
        return ddb.describe_table(TableName=connections_table)["Table"]["TableStatus"]

    read("connectionsTableStatus", table_exists)

    start = int((time.time() - THRESHOLD_HOURS * 3600) * 1000)

    def recent_errors():
        group = "/aws/lambda/" + fn
        pattern = "?ERROR ?Traceback ?AccessDenied ?\"KeyError\""
        events = logs.filter_log_events(
            logGroupName=group, startTime=start, limit=MAX_LOG_EVENTS,
            filterPattern=pattern,
        )["events"]
        return [
            {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(e["timestamp"] / 1000)),
                "message": e["message"][:MAX_EVENT_CHARS],
            }
            for e in events
        ]

    read("recentErrorLogs", recent_errors)
    return proof


if __name__ == "__main__":
    summary = inspect(
        boto3.client("cloudformation"),
        boto3.client("iam"),
        boto3.client("lambda"),
        boto3.client("logs"),
        boto3.client("dynamodb"),
    )
    print(json.dumps(summary, indent=1, default=str))
