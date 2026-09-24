import type { CatalogField } from "./catalog";
import { actionCatalog, connectorCatalog, filterOperators } from "./catalog";
import type { ActionType, DiagramShape, FilterRule, NodeData, Workflow, WorkflowSummary } from "./types";

export { actionCatalog, connectorCatalog, filterOperators };

export const NODE_WIDTH = 264;
export const NODE_HEIGHT = 96;
const NODE_GAP_Y = 130;
const ORIGIN_X = 420;
const ORIGIN_Y = 140;

export function actionMeta(type: ActionType) {
  return actionCatalog.find((entry) => entry.type === type);
}

/** Inspector starting values for a new node of this action type. */
export function defaultFields(type: ActionType): Record<string, string> {
  const fields: Record<string, string> = {};
  for (const field of actionMeta(type)?.fields ?? []) {
    if (field.default !== undefined) fields[field.key] = field.default;
  }
  return fields;
}

export function defaultNodeData(kind: "trigger" | "action"): NodeData {
  if (kind === "trigger") {
    return { nodeKind: "trigger", connector: "email", event: "message.received", filters: [] };
  }
  return { nodeKind: "action", actionType: "webhook", fields: defaultFields("webhook") };
}

export function actionNodeTitle(data: NodeData): string {
  return actionMeta(data.actionType ?? "webhook")?.label ?? "Action";
}

export function actionNodeSubtitle(data: NodeData): string {
  const fields = data.fields ?? {};
  const meta = actionMeta(data.actionType ?? "webhook");
  const first = meta?.fields.find((field) => fields[field.key]);
  return first ? String(fields[first.key]) : meta?.fields[0]?.label ?? "";
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** The object a field writes to: the action itself or its `group` object. */
function fieldTarget(action: Record<string, unknown>, field: CatalogField): Record<string, unknown> {
  if (!field.group) return action;
  const group = action[field.group];
  if (isRecord(group)) return group;
  const created: Record<string, unknown> = {};
  action[field.group] = created;
  return created;
}

/** Chain actions in connector order starting at the trigger node. */
function orderedActions(shapes: DiagramShape[], trigger: DiagramShape): { actions: DiagramShape[]; problems: string[] } {
  const outgoing = new Map<string, string[]>();
  for (const shape of shapes) {
    if (shape.type !== "arrow" || !shape.sourceId || !shape.targetId) continue;
    outgoing.set(shape.sourceId, [...(outgoing.get(shape.sourceId) ?? []), shape.targetId]);
  }

  const actions: DiagramShape[] = [];
  const visited = new Set<string>([trigger.id]);
  const problems: string[] = [];
  let frontier = (outgoing.get(trigger.id) ?? []).filter((id) => id !== trigger.id);

  while (frontier.length) {
    const nextFrontier: string[] = [];
    for (const id of frontier) {
      if (visited.has(id)) {
        problems.push("The connector graph has a cycle; actions were ordered by traversal.");
        continue;
      }
      visited.add(id);
      const node = shapes.find((shape) => shape.id === id);
      if (!node || node.type !== "node") continue;
      if (node.data?.nodeKind === "trigger") {
        problems.push("Only one trigger per workflow; the extra trigger was ignored.");
        continue;
      }
      actions.push(node);
      nextFrontier.push(...(outgoing.get(id) ?? []));
    }
    frontier = nextFrontier;
  }

  for (const shape of shapes) {
    if (shape.type === "node" && shape.data?.nodeKind === "action" && !visited.has(shape.id)) {
      problems.push(`"${shape.label}" is not connected to the trigger and was not saved.`);
    }
  }
  return { actions, problems };
}

function filterRulesToYaml(rules: FilterRule[] | undefined): Record<string, Record<string, unknown>> {
  const filters: Record<string, Record<string, unknown>> = {};
  for (const rule of rules ?? []) {
    if (!rule.field.trim()) continue;
    filters[rule.field.trim()] = rule.operator === "equals" && rule.value !== ""
      ? { equals: rule.value }
      : { [rule.operator]: rule.value };
  }
  return filters;
}

function actionToYaml(node: DiagramShape, index: number): Record<string, unknown> {
  const data = node.data ?? defaultNodeData("action");
  const action: Record<string, unknown> = {
    id: (data.fields?.id ?? "").trim() || `action-${index + 1}`,
    type: data.actionType ?? "webhook"
  };
  const meta = actionMeta(data.actionType);
  if (!meta) {
    // Unknown action type: write the original YAML back untouched.
    return { ...action, ...(data.raw ?? {}) };
  }
  for (const field of meta.fields) {
    if (field.type === "boolean") {
      // Stay out of the YAML while the checkbox matches its default, so
      // engine defaults keep minimal diffs and round-trips stay stable.
      const on = (data.fields?.[field.key] ?? field.default ?? "false") === "true";
      if (on !== ((field.default ?? "false") === "true")) fieldTarget(action, field)[field.key] = on;
      continue;
    }
    const value = (data.fields?.[field.key] ?? "").trim() || field.default || "";
    if (value === "") continue;
    const parsed = field.type === "number" ? Number(value) : value;
    fieldTarget(action, field)[field.key] = field.type === "number" && Number.isFinite(parsed) ? parsed : value;
  }
  for (const field of meta.fields) {
    if (field.group && Object.keys(action[field.group] as Record<string, unknown>).length === 0) {
      delete action[field.group];
    }
  }
  return action;
}

export function workflowFromShapes(
  shapes: DiagramShape[],
  workflowId: string,
  enabled: boolean
): { workflow: Workflow; problems: string[] } {
  const problems: string[] = [];
  const triggers = shapes.filter((shape) => shape.type === "node" && shape.data?.nodeKind === "trigger");
  if (triggers.length === 0) problems.push("Add a trigger node before saving.");
  if (triggers.length > 1) problems.push("Only one trigger per workflow; the extra triggers were ignored.");

  const trigger = triggers[0];
  const triggerData = trigger?.data ?? defaultNodeData("trigger");
  const { actions, problems: chainProblems } = trigger
    ? orderedActions(shapes, trigger)
    : { actions: [], problems: [] };
  if (trigger && actions.length === 0) problems.push("Connect at least one action to the trigger.");

  return {
    workflow: {
      id: workflowId.trim() || "untitled-workflow",
      enabled,
      trigger: {
        connector: triggerData.connector ?? "custom",
        event: triggerData.event?.trim() || "received",
        filters: filterRulesToYaml(triggerData.filters)
      },
      actions: actions.map(actionToYaml)
    },
    problems: [...problems, ...chainProblems]
  };
}

function yamlFiltersToRules(filters: Record<string, Record<string, unknown>> | undefined): FilterRule[] {
  return Object.entries(filters ?? {}).flatMap(([field, rule]) => {
    if (!rule || typeof rule !== "object") return [{ field, operator: "equals" as const, value: String(rule ?? "") }];
    return Object.entries(rule).map(([operator, value]) => ({
      field,
      operator: (filterOperators as readonly string[]).includes(operator)
        ? operator as FilterRule["operator"]
        : "equals" as const,
      value: String(value ?? "")
    }));
  });
}

/** Catalog-owned keys of an action; anything else only survives via `raw`. */
function actionFields(type: ActionType, action: Record<string, unknown>): Record<string, string> {
  const fields: Record<string, string> = { id: String(action.id ?? "") };
  const meta = actionMeta(type);
  if (!meta) return fields;
  for (const field of meta.fields) {
    const holder = field.group ? action[field.group] : action;
    const value = isRecord(holder) ? holder[field.key] : undefined;
    fields[field.key] = value === undefined ? (field.default ?? "") : String(value);
  }
  return fields;
}

/** Everything but id/type, for actions the catalog does not model. */
function rawAction(action: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(Object.entries(action).filter(([key]) => key !== "id" && key !== "type"));
}

export function shapesFromWorkflow(workflow: Workflow): DiagramShape[] {
  const shapes: DiagramShape[] = [];
  const rowY = (index: number) => ORIGIN_Y + index * (NODE_HEIGHT + NODE_GAP_Y);

  const trigger: DiagramShape = {
    id: "trigger",
    type: "node",
    x: ORIGIN_X,
    y: rowY(0),
    width: NODE_WIDTH,
    height: NODE_HEIGHT,
    label: `${workflow.trigger.connector} · ${workflow.trigger.event}`,
    data: {
      nodeKind: "trigger",
      connector: connectorCatalog.some((entry) => entry.name === workflow.trigger.connector)
        ? workflow.trigger.connector
        : "custom",
      event: workflow.trigger.event,
      filters: yamlFiltersToRules(workflow.trigger.filters)
    }
  };
  shapes.push(trigger);

  let previous = trigger;
  workflow.actions.forEach((action, index) => {
    const type = String(action.type ?? "webhook");
    const known = actionMeta(type) !== undefined;
    const node: DiagramShape = {
      id: `action-${index}`,
      type: "node",
      x: ORIGIN_X,
      y: rowY(index + 1),
      width: NODE_WIDTH,
      height: NODE_HEIGHT,
      label: actionMeta(type)?.label ?? String(type),
      data: {
        nodeKind: "action",
        actionType: type,
        fields: actionFields(type, action),
        ...(known ? {} : { raw: rawAction(action) })
      }
    };
    shapes.push(node);
    shapes.push({
      id: `link-${index}`,
      type: "arrow",
      x: 0,
      y: 0,
      width: 0,
      height: 0,
      sourceId: previous.id,
      targetId: node.id,
      sourceHandleId: "bottom",
      targetHandleId: "top"
    });
    previous = node;
  });

  return shapes;
}

export function summarize(source: string, workflow: Workflow): WorkflowSummary {
  return {
    id: workflow.id,
    enabled: workflow.enabled !== false,
    source,
    connector: workflow.trigger?.connector ?? "?",
    event: workflow.trigger?.event ?? "?",
    actionCount: Array.isArray(workflow.actions) ? workflow.actions.length : 0
  };
}
