"""One-time migration from the deployed YAML catalog to managed workflows.

Run before deploying the published-only engine. The default is a read-only
plan; --apply writes the missing workflows and materializes shared-flow
references in stored triggers. Existing published workflows always win.
"""

import argparse
import copy
import sys
from pathlib import Path

import boto3
import yaml

from src.dapier.api.designer_store import parse_workflow, workflow_yaml_text
from src.dapier.triggers import published_workflows

TABLES = {
    "workflows": ("PublishedWorkflowsTable", "workflow_id"),
    "email": ("EmailTriggersTable", "name"),
    "hook": ("HookTriggersTable", "hook_id"),
    "schedule": ("ScheduleTriggersTable", "schedule_id"),
    "poll": ("PollTriggersTable", "poll_id"),
}


def catalog(directory):
    workflows, flows = {}, {}
    for path in sorted(directory.glob("*.yaml")):
        doc = yaml.safe_load(path.read_text())
        if not isinstance(doc, dict):
            raise ValueError(f"{path}: expected a YAML mapping")
        for name, spec in (doc.get("flows") or {}).items():
            actions = spec if isinstance(spec, list) else spec.get("actions")
            if not isinstance(actions, list) or not actions:
                raise ValueError(f"{path}: flow {name} has no actions")
            if name in flows:
                raise ValueError(f"{path}: duplicate flow {name}")
            flows[name] = actions
        if doc.get("id"):
            if doc["id"] in workflows:
                raise ValueError(f"{path}: duplicate workflow id {doc['id']}")
            workflows[doc["id"]] = doc
    converted = {}
    for workflow_id, doc in workflows.items():
        result = copy.deepcopy(doc)
        if result.get("flow"):
            result["actions"] = copy.deepcopy(flows[result.pop("flow")])
        result.pop("flows", None)
        # Validate the exact definition the managed store will read.
        converted[workflow_id] = parse_workflow(workflow_yaml_text(result))
    return converted, flows


def physical_tables(stack_name, cloudformation, dynamodb):
    resources = {}
    paginator = cloudformation.get_paginator("list_stack_resources")
    for page in paginator.paginate(StackName=stack_name):
        resources.update({item["LogicalResourceId"]: item["PhysicalResourceId"]
                          for item in page["StackResourceSummaries"]})
    return {kind: (dynamodb.Table(resources[logical]), key)
            for kind, (logical, key) in TABLES.items()}


def scan_all(table):
    items, start = [], None
    while True:
        kwargs = {"ExclusiveStartKey": start} if start else {}
        page = table.scan(ConsistentRead=True, **kwargs)
        items.extend(page.get("Items", []))
        start = page.get("LastEvaluatedKey")
        if not start:
            return items


def plan(workflows, flows, tables):
    published_table, _ = tables["workflows"]
    existing = {item["workflow_id"]: item for item in scan_all(published_table)
                if not item.get("version_of")}
    missing = {key: value for key, value in workflows.items() if key not in existing}
    published_updates = []
    for workflow_id, item in existing.items():
        workflow = published_workflows._decode_numbers(item.get("workflow"))
        if not isinstance(workflow, dict):
            continue
        if workflow.get("flow") or workflow.get("flows"):
            converted = copy.deepcopy(workflow)
            if converted.get("flow"):
                flow = converted.pop("flow")
                if flow not in flows:
                    raise ValueError(f"published workflow {workflow_id} references unknown flow {flow}")
                converted["actions"] = copy.deepcopy(flows[flow])
            converted.pop("flows", None)
            converted = parse_workflow(workflow_yaml_text(converted))
            published_updates.append((item, converted))
    trigger_updates = []
    for kind in ("email", "hook", "schedule", "poll"):
        table, key = tables[kind]
        for item in scan_all(table):
            flow = item.get("flow")
            if not flow:
                continue
            if flow not in flows:
                raise ValueError(f"{kind} trigger {item[key]} references unknown flow {flow}")
            trigger_updates.append((kind, table, key, item, copy.deepcopy(flows[flow])))
    return missing, published_updates, trigger_updates


def apply(missing, published_updates, trigger_updates, tables):
    # The published records go first while the old bundle still runs. A
    # conditional put keeps a concurrently published edit from being replaced.
    table, _ = tables["workflows"]
    for workflow_id, workflow in sorted(missing.items()):
        item = published_workflows.publish(workflow, cause="legacy-migration",
                                           operator="migration", table_ref=table,
                                           only_if_absent=True)
        print(f"published {workflow_id} revision {item['revision']}")
    for previous, workflow in published_updates:
        item = published_workflows.publish(workflow, cause="legacy-migration",
                                           operator="migration", previous=previous,
                                           table_ref=table,
                                           expected_revision=previous.get("revision") or 0)
        print(f"converted published {workflow['id']} revision {item['revision']}")
    # Materialize the effective actions without resetting trigger identity,
    # tokens, EventBridge rules, or creation metadata. The conditional check
    # refuses to clobber a trigger edited since the plan was read.
    for kind, table, key, item, actions in trigger_updates:
        table.update_item(
            Key={key: item[key]},
            UpdateExpression="SET actions = :actions, flow = :empty",
            ConditionExpression="flow = :old",
            ExpressionAttributeValues={":actions": actions, ":empty": "",
                                       ":old": item["flow"]},
        )
        print(f"materialized {kind} trigger {item[key]}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stack", default="dapier")
    parser.add_argument("--region", default="eu-west-1")
    parser.add_argument("--source", type=Path,
                        default=Path("migrations/legacy-workflows"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    workflows, flows = catalog(args.source)
    session = boto3.Session(region_name=args.region)
    tables = physical_tables(args.stack, session.client("cloudformation"),
                             session.resource("dynamodb"))
    missing, published_updates, updates = plan(workflows, flows, tables)
    print(f"{len(workflows)} legacy workflows, {len(missing)} to publish, "
          f"{len(published_updates)} published definitions to convert, "
          f"{len(updates)} flow-bound triggers to convert")
    for workflow_id in sorted(missing):
        print(f"  publish {workflow_id}")
    for previous, _ in published_updates:
        print(f"  convert published {previous['workflow_id']}")
    for kind, _, key, item, _ in updates:
        print(f"  materialize {kind} trigger {item[key]} ({item['flow']})")
    if args.apply:
        apply(missing, published_updates, updates, tables)
        remaining, published_remaining, trigger_remaining = plan(workflows, flows, tables)
        if remaining or published_remaining or trigger_remaining:
            raise RuntimeError("migration verification failed: records still need conversion")
        print("Migration verified: every workflow is managed and every trigger has inline actions.")
    else:
        print("Read-only plan. Pass --apply after reviewing it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
