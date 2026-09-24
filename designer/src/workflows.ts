import type { ActionType, DiagramShape, FilterRule, NodeData, Workflow, WorkflowSummary } from "./types";

export const actionCatalog: Array<{
  type: ActionType;
  label: string;
  /** Order follows the fields the engine reads in src/engine.py. */
  fields: Array<{ key: string; label: string; placeholder?: string; required?: boolean }>;
}> = [
  {
    type: "webhook",
    label: "Webhook",
    fields: [
      { key: "url", label: "URL", required: true },
      { key: "secret_id", label: "Signing secret ID", placeholder: "dapier/webhook" },
      { key: "timeout_seconds", label: "Timeout (s)" }
    ]
  },
  {
    type: "slack",
    label: "Slack",
    fields: [
      { key: "credential_id", label: "Credential ID", required: true },
      { key: "channel", label: "Channel", placeholder: "#alerts", required: true },
      { key: "text", label: "Text template", placeholder: "{title}\n{url}" }
    ]
  },
  {
    type: "dataops",
    label: "DataOps intake",
    fields: [
      { key: "auth_secret_id", label: "Auth secret ID", placeholder: "dapier/dataops", required: true },
      { key: "url_env", label: "URL env var", placeholder: "DATAOPS_INTAKE_URL" },
      { key: "url", label: "URL (overrides env)" }
    ]
  },
  {
    type: "dropbox_upload",
    label: "Dropbox upload",
    fields: [
      { key: "connection_id", label: "Connection ID", placeholder: "dropbox", required: true },
      { key: "source", label: "Source", placeholder: "attachment | output" },
      { key: "folder", label: "Folder", placeholder: "/Invoices" },
      { key: "filename", label: "Filename override" }
    ]
  },
  {
    type: "render_html_to_pdf",
    label: "Render PDF",
    fields: [
      { key: "input_field", label: "Input field", placeholder: "html" },
      { key: "output_key", label: "Output key", placeholder: "rendered/{event_id}.pdf" },
      { key: "output_bucket_env", label: "Output bucket env" }
    ]
  }
];

export const connectorCatalog = ["email", "youtube", "renderer", "dropbox", "custom"] as const;
export const filterOperators = ["equals", "prefix", "suffix", "contains"] as const;

export const NODE_WIDTH = 264;
export const NODE_HEIGHT = 96;
const NODE_GAP_Y = 130;
const ORIGIN_X = 420;
const ORIGIN_Y = 140;

export function actionMeta(type: ActionType) {
  return actionCatalog.find((entry) => entry.type === type);
}

export function defaultNodeData(kind: "trigger" | "action"): NodeData {
  if (kind === "trigger") {
    return { nodeKind: "trigger", connector: "email", event: "message.received", filters: [] };
  }
  return { nodeKind: "action", actionType: "webhook", fields: {} };
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
  for (const field of actionMeta(data.actionType ?? "webhook")?.fields ?? []) {
    const value = (data.fields?.[field.key] ?? "").trim();
    if (value === "") continue;
    const parsed = Number(value);
    action[field.key] = field.key === "timeout_seconds" && Number.isFinite(parsed) ? parsed : value;
  }
  if (data.actionType === "render_html_to_pdf") {
    action.pdf = {
      page_format: data.pdfPageFormat?.trim() || "A4",
      print_background: data.pdfPrintBackground ?? true
    };
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

/** Strip keys the inspector owns so round-trips do not accumulate unknowns. */
function actionFields(type: ActionType, action: Record<string, unknown>): Record<string, string> {
  const known = new Set(actionMeta(type)?.fields.map((field) => field.key) ?? []);
  const fields: Record<string, string> = { id: String(action.id ?? "") };
  for (const [key, value] of Object.entries(action)) {
    if (key === "id" || key === "type" || key === "pdf") continue;
    if (known.has(key)) fields[key] = String(value ?? "");
  }
  return fields;
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
      connector: (connectorCatalog as readonly string[]).includes(workflow.trigger.connector)
        ? workflow.trigger.connector as NodeData["connector"]
        : "custom",
      event: workflow.trigger.event,
      filters: yamlFiltersToRules(workflow.trigger.filters)
    }
  };
  shapes.push(trigger);

  let previous = trigger;
  workflow.actions.forEach((action, index) => {
    const type = (actionCatalog.find((entry) => entry.type === action.type)?.type ?? "webhook") as ActionType;
    const node: DiagramShape = {
      id: `action-${index}`,
      type: "node",
      x: ORIGIN_X,
      y: rowY(index + 1),
      width: NODE_WIDTH,
      height: NODE_HEIGHT,
      label: actionMeta(type)?.label ?? "Action",
      data: {
        nodeKind: "action",
        actionType: type,
        fields: actionFields(type, action),
        pdfPageFormat: String((action.pdf as Record<string, unknown> | undefined)?.page_format ?? "A4"),
        pdfPrintBackground: Boolean((action.pdf as Record<string, unknown> | undefined)?.print_background ?? true)
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
