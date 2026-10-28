import { dump, load } from "js-yaml";
import type { CatalogField } from "./catalog";
import { actionCatalog, connectorCatalog, errorHandlingFields, eventInfo, filterOperators, logicOperators } from "./catalog";
import type { ActionType, DiagramShape, FilterRule, NodeData, TriggerSpec, Workflow, WorkflowSummary } from "./types";

export { actionCatalog, connectorCatalog, errorHandlingFields, filterOperators, logicOperators };

export const NODE_WIDTH = 264;
export const NODE_HEIGHT = 96;
export const BRANCH_PLACEHOLDER_HEIGHT = 60;
export const UNION_JOIN_HEIGHT = 52;
export const BRANCH_GAP_X = 64;
const NODE_GAP_Y = 130;
const BRANCH_NODE_GAP_Y = 88;
const ORIGIN_X = 420;
const ORIGIN_Y = 140;

export function isBranchingAction(type: ActionType | undefined): boolean {
  return type === "condition" || type === "paths" || type === "decision";
}

export function actionMeta(type: ActionType) {
  return actionCatalog.find((entry) => entry.type === type);
}

export function connectorMeta(name: string) {
  return connectorCatalog.find((entry) => entry.name === name);
}

/** Product name for a connector ("youtube" → "YouTube"); falls back to the raw name. */
export function connectorLabel(name: string) {
  return connectorMeta(name)?.label ?? name;
}

/** A trigger in operator words: "Email arrives", "Dropbox file added",
 * "Telegram channel post received" — the catalog's event label, prefixed by
 * the connector unless the label already names it. Custom events keep
 * "Connector · event". */
export function triggerLabel(connector: string, event: string) {
  const name = connectorLabel(connector);
  const info = eventInfo(connector, event);
  if (!info) return event ? `${name} · ${event}` : name;
  const source = name.toLowerCase().split(" ")[0];
  const first = info.label.toLowerCase().split(" ")[0];
  if (info.label.toLowerCase().includes(source) || source.startsWith(first)) return info.label;
  return `${name} ${info.label.charAt(0).toLowerCase()}${info.label.slice(1)}`;
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

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Last dot-segment of a condition field path, so canvas subtitles stay
    readable: "steps.triage.output.result.has_attachment" → "has_attachment".
    Short fields ("subject", "route") pass through untouched. */
export function shortConditionField(field: string): string {
  const trimmed = field.trim();
  if (!trimmed) return trimmed;
  const last = trimmed.split(".").pop()?.trim();
  return last || trimmed;
}

/** Extracts predicate rules from either flat `fields` (field/operator/value)
    or a `when:` mapping in `raw.when` ({ field: { op: value } }). */
export function extractPredicateRules(
  fields?: Record<string, string>,
  raw?: Record<string, unknown>
): FilterRule[] {
  const flatField = (fields?.field ?? "").trim();
  if (flatField) {
    const op = (fields?.operator || "equals").trim();
    return [{
      field: flatField,
      operator: (logicOperators as readonly string[]).includes(op)
        ? (op as FilterRule["operator"])
        : "equals",
      value: fields?.value ?? ""
    }];
  }
  const when = raw?.when;
  if (Array.isArray(when)) {
    const out: FilterRule[] = [];
    for (const item of when) {
      if (!isRecord(item)) continue;
      if (typeof item.field === "string" && item.field.trim()) {
        const op = String(item.operator ?? "equals").trim();
        out.push({
          field: item.field.trim(),
          operator: (logicOperators as readonly string[]).includes(op)
            ? (op as FilterRule["operator"])
            : "equals",
          value: String(item.value ?? "")
        });
      } else {
        out.push(...yamlFiltersToRules(item as Record<string, Record<string, unknown>>));
      }
    }
    if (out.length > 0) return out;
  }
  if (isRecord(when)) {
    return yamlFiltersToRules(when as Record<string, Record<string, unknown>>);
  }
  return [];
}

/** Infers whether a `decision` step runs in `if` or `switch` mode. */
export function decisionMode(
  fields?: Record<string, string>,
  raw?: Record<string, unknown>
): "if" | "switch" {
  const explicit = (fields?.mode ?? (typeof raw?.mode === "string" ? raw.mode : "")).trim();
  if (explicit === "if" || explicit === "switch") return explicit;
  if (
    (fields?.choices ?? "").trim() ||
    (fields?.cases ?? "").trim() ||
    (fields?.branches ?? "").trim() ||
    Array.isArray(raw?.choices) ||
    Array.isArray(raw?.cases) ||
    isRecord(raw?.branches)
  ) {
    return "switch";
  }
  if (
    (fields?.then ?? "").trim() ||
    (fields?.else ?? "").trim() ||
    (fields?.then_actions ?? "").trim() ||
    Array.isArray(raw?.then) ||
    Array.isArray(raw?.else) ||
    Array.isArray(raw?.then_actions)
  ) {
    return "if";
  }
  return "switch";
}

export interface NodeBranch {
  key: string;
  label: string;
  description?: string;
  when?: Record<string, Record<string, unknown>>;
  steps: Array<Record<string, unknown>>;
  isDefault?: boolean;
}

function parseYamlArray(text: string | undefined, fallback?: unknown): unknown[] {
  if (text !== undefined) {
    const trimmed = text.trim();
    if (!trimmed) return [];
    try {
      const parsed = load(trimmed);
      if (Array.isArray(parsed)) return parsed;
    } catch {
      /* fall through */
    }
  }
  return Array.isArray(fallback) ? fallback : [];
}

function parseYamlStepObjects(text: string | undefined, fallback?: unknown): Array<Record<string, unknown>> {
  return parseYamlArray(text, fallback).filter(isRecord);
}

/** Extracts the outgoing branches and their nested step lists for a branching
    node (`condition`, `decision`, `paths`). */
export function extractNodeBranches(data: NodeData): NodeBranch[] {
  const fields = data.fields ?? {};
  const raw = data.raw ?? {};
  const type = data.actionType;

  if (type === "condition") {
    const thenSteps = parseYamlStepObjects(fields.then, raw.then);
    const elseSteps = parseYamlStepObjects(fields.else, raw.else);
    return [
      { key: "then", label: "then", steps: thenSteps },
      { key: "else", label: "else", steps: elseSteps, isDefault: true }
    ];
  }

  if (type === "decision") {
    const mode = decisionMode(fields, raw);
    if (mode === "if") {
      const thenSteps = parseYamlStepObjects(fields.then ?? fields.then_actions, raw.then ?? raw.then_actions);
      const elseSteps = parseYamlStepObjects(fields.else ?? fields.default_actions, raw.else ?? raw.default_actions);
      return [
        { key: "then", label: "then", steps: thenSteps },
        { key: "else", label: "else", steps: elseSteps, isDefault: true }
      ];
    }
    let branchesMap: Record<string, unknown> | null = null;
    if ((fields.branches ?? "").trim()) {
      try {
        const loaded = load(fields.branches);
        if (isRecord(loaded)) branchesMap = loaded;
      } catch { /* fall through */ }
    } else if (isRecord(raw.branches) && !(fields.cases ?? "").trim() && !(fields.choices ?? "").trim()) {
      branchesMap = raw.branches;
    }

    let branches: NodeBranch[];
    if (branchesMap) {
      branches = Object.entries(branchesMap).map(([key, entry]) => {
        const isObj = isRecord(entry);
        const description = isObj && typeof entry.description === "string" ? entry.description : undefined;
        const steps = Array.isArray(entry)
          ? entry.filter(isRecord)
          : isObj && Array.isArray(entry.actions)
            ? entry.actions.filter(isRecord)
            : [];
        return {
          key,
          label: key,
          ...(description !== undefined ? { description } : {}),
          steps
        };
      });
    } else {
      const rawChoices = parseYamlArray(fields.choices, raw.choices)
        .map((item) => String(item ?? "").trim())
        .filter(Boolean);
      const rawCases = parseYamlArray(fields.cases, raw.cases).filter(isRecord);
      const choiceNames: string[] = [...rawChoices];
      for (const c of rawCases) {
        const name = String(c.choice ?? c.label ?? "").trim();
        if (name && !choiceNames.includes(name)) choiceNames.push(name);
      }
      const effectiveChoices = choiceNames.length > 0 ? choiceNames : ["choice-1", "choice-2"];
      branches = effectiveChoices.map((choice) => {
        const matching = rawCases.find((c) => String(c.choice ?? c.label ?? "").trim() === choice);
        const description = typeof matching?.description === "string" ? matching.description : undefined;
        const steps = Array.isArray(matching?.actions)
          ? matching.actions.filter(isRecord)
          : Array.isArray(matching?.then)
            ? matching.then.filter(isRecord)
            : [];
        return {
          key: choice,
          label: choice,
          ...(description !== undefined ? { description } : {}),
          steps
        };
      });
    }
    const defaultSource = raw.default_actions ?? raw.default ?? raw.else;
    const defaultSteps = parseYamlStepObjects(fields.default_actions, defaultSource);
    const hasDefaultDefined =
      defaultSteps.length > 0 ||
      (fields.default_actions ?? "").trim() !== "" ||
      raw.default_actions !== undefined ||
      raw.default !== undefined ||
      raw.else !== undefined;
    if (hasDefaultDefined) {
      branches.push({ key: "else", label: "else", steps: defaultSteps, isDefault: true });
    }
    return branches;
  }

  if (type === "paths") {
    const rawPaths = parseYamlArray(fields.paths, raw.paths).filter(isRecord);
    const effectivePaths = rawPaths.length > 0
      ? rawPaths
      : [{ label: "path-1", when: {}, actions: [] }];
    const branches: NodeBranch[] = effectivePaths.map((p, idx) => {
      const key = String(p.id ?? p.label ?? `path-${idx + 1}`).trim() || `path-${idx + 1}`;
      const label = String(p.label ?? p.id ?? `path-${idx + 1}`).trim() || `path-${idx + 1}`;
      let when: Record<string, Record<string, unknown>> = {};
      if (isRecord(p.when)) {
        when = p.when as Record<string, Record<string, unknown>>;
      } else if (typeof p.field === "string" && p.field.trim()) {
        when = { [p.field.trim()]: { [String(p.operator || "equals")]: p.value ?? "" } };
      }
      const steps = Array.isArray(p.actions)
        ? p.actions.filter(isRecord)
        : Array.isArray(p.then)
          ? p.then.filter(isRecord)
          : [];
      return { key, label, when, steps };
    });
    const defaultSource = raw.default ?? raw.else;
    const defaultSteps = parseYamlStepObjects(fields.default, defaultSource);
    branches.push({ key: "else", label: "else", steps: defaultSteps, isDefault: true });
    return branches;
  }

  return [];
}

export function actionNodeTitle(data: NodeData): string {
  if (data.isBranchPlaceholder) {
    return `+ Add step to ${data.branchLabel || "branch"}`;
  }
  if (data.isUnionJoin) {
    return "Union";
  }
  return actionMeta(data.actionType ?? "webhook")?.label ?? data.actionType ?? "Action";
}

export function actionNodeSubtitle(data: NodeData): string {
  if (data.isBranchPlaceholder) {
    return data.branchLabel ? `Empty ${data.branchLabel} branch` : "Empty branch";
  }
  if (data.isUnionJoin) {
    return "branches rejoin";
  }
  const fields = data.fields ?? {};
  // A condition or filter reads as its test ("channel = course-ml"), so a chain
  // of Conditions stays tellable apart on the canvas and in the node list.
  // Long template paths ("steps.triage.output.result.has_attachment") shorten
  // to their last segment ("has_attachment") — the canvas truncates at ~26
  // characters, so the full path always rendered as unreadable "…res...".
  if (data.actionType === "condition" || data.actionType === "filter") {
    const symbols: Record<string, string> = { equals: "=", not_equals: "≠", gt: ">", gte: "≥", lt: "<", lte: "≤" };
    const rules = extractPredicateRules(fields, data.raw);
    if (rules.length > 0) {
      const first = rules[0];
      const base = `${shortConditionField(first.field)} ${symbols[first.operator] ?? first.operator} ${first.value}`.trim();
      return rules.length > 1 ? `${base} (+${rules.length - 1})` : base;
    }
  }
  if (data.actionType === "decision") {
    const mode = decisionMode(fields, data.raw);
    if (mode === "if") {
      const promptSnippet = (fields.prompt ?? "").trim().replace(/\s+/g, " ").slice(0, 22) || "yes / no";
      return `jev if · ${promptSnippet}`;
    }
    const branches = extractNodeBranches(data).filter((b) => !b.isDefault);
    if (branches.length > 0) {
      return `jev · ${branches.map((b) => b.label).join(" | ")}`;
    }
    return "jev · switch";
  }
  if (data.actionType === "paths") {
    const branches = extractNodeBranches(data).filter((b) => !b.isDefault);
    if (branches.length > 0) {
      return branches.map((b) => b.label).join(" | ");
    }
    return "Paths";
  }
  if (data.actionType === "union") {
    const from = (fields.from ?? "").trim() || "branches";
    return `merge ← ${from}`;
  }
  const meta = actionMeta(data.actionType ?? "webhook");
  const first = meta?.fields.find((field) => fields[field.key]);
  return first ? String(fields[first.key]) : meta?.fields[0]?.label ?? "";
}

export interface BranchStepSummary {
  id: string;
  type: string;
  label: string;
  detail: string;
}

export interface ParsedBranch {
  steps: BranchStepSummary[];
  /** Non-null when the YAML text does not parse to a step list. */
  error: string | null;
}

/** Parses a condition branch (`then`/`else` YAML text) into displayable step
    rows. Empty text is a valid empty branch; anything that does not parse to
    a list of step objects reports an error and lets the caller fall back to
    the raw-YAML editor. */
export function parseBranchSteps(text: string): ParsedBranch {
  if (!text.trim()) return { steps: [], error: null };
  let parsed: unknown;
  try {
    parsed = load(text);
  } catch {
    return { steps: [], error: "Not valid YAML." };
  }
  if (!Array.isArray(parsed)) return { steps: [], error: "Must be a YAML list of steps." };
  return { steps: parsed.map(describeBranchStep), error: null };
}

/** One nested step as an inspector row: its id, its catalog label, and the
    first telling scalar (catalog field order wins, code shows its first
    line). Nested/grouped objects are skipped — this is a preview, not a
    full field renderer. */
export function describeBranchStep(step: unknown): BranchStepSummary {
  if (!isRecord(step)) return { id: "?", type: "?", label: "Step", detail: "" };
  const id = String(step.id ?? "?");
  const type = String(step.type ?? "?");
  const label = actionMeta(type)?.label ?? type;
  const catalogKeys = actionMeta(type)?.fields.map((field) => field.key) ?? [];
  const preferred = [...catalogKeys, ...Object.keys(step)];
  for (const key of preferred) {
    if (key === "id" || key === "type" || key === "tests") continue;
    const value = step[key];
    if (typeof value === "string" && value.trim()) {
      const firstLine = value.split("\n").map((line) => line.trim()).find((line) => line) ?? "";
      return { id, type, label, detail: clipDetail(firstLine) };
    }
    if (typeof value === "number" || typeof value === "boolean") {
      return { id, type, label, detail: String(value) };
    }
  }
  return { id, type, label, detail: "" };
}

function clipDetail(text: string, limit = 64): string {
  return text.length > limit ? `${text.slice(0, limit - 1)}…` : text;
}

/** Top-level key order for workflow YAML dumps — the server's
    WORKFLOW_KEY_ORDER: identity, state, trigger, then the action graph.
    Keys outside the canon keep their relative order at the end. */
const WORKFLOW_KEY_ORDER = ["id", "name", "enabled", "trigger", "triggers", "actions"];

export function orderedWorkflow(workflow: Workflow): Workflow {
  const source = workflow as unknown as Record<string, unknown>;
  const ordered: Record<string, unknown> = {};
  for (const key of WORKFLOW_KEY_ORDER) {
    if (source[key] !== undefined) ordered[key] = source[key];
  }
  for (const [key, value] of Object.entries(source)) {
    if (!(key in ordered)) ordered[key] = value;
  }
  return ordered as unknown as Workflow;
}

/** Every entry point of a workflow: its `triggers` list or single `trigger`. */
export function workflowTriggers(workflow: Workflow): TriggerSpec[] {
  if (Array.isArray(workflow.triggers) && workflow.triggers.length) return workflow.triggers;
  return workflow.trigger ? [workflow.trigger] : [];
}

/** The chain a workflow runs: its inline `actions`. */
export function workflowActions(workflow: Workflow): Array<Record<string, unknown>> {
  return Array.isArray(workflow.actions) ? workflow.actions : [];
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

function isSyntheticHelperNode(shape: DiagramShape): boolean {
  return Boolean(shape.data?.isBranchPlaceholder || shape.data?.isUnionJoin);
}

function isBranchChildNode(shape: DiagramShape): boolean {
  return Boolean(shape.data?.parentBranchNodeId && !shape.data?.isBranchPlaceholder && !shape.data?.isUnionJoin);
}

/** Chain top-level actions in traversal order starting at the trigger nodes.
    Branch subgraphs and synthetic union join nodes act as transparent bridges
    between a branching node and the next top-level step after the union join. */
function orderedActions(shapes: DiagramShape[], roots: DiagramShape[]): { actions: DiagramShape[]; lost: DiagramShape[]; problems: string[] } {
  const outgoing = new Map<string, string[]>();
  const mergeTargets = new Set<string>();
  for (const shape of shapes) {
    if (shape.type !== "arrow" || !shape.sourceId || !shape.targetId) continue;
    outgoing.set(shape.sourceId, [...(outgoing.get(shape.sourceId) ?? []), shape.targetId]);
    if (shape.isMergeEdge) mergeTargets.add(shape.targetId);
  }

  const byId = new Map<string, DiagramShape>(shapes.map((s) => [s.id, s]));
  const actions: DiagramShape[] = [];
  const visited = new Set<string>(roots.map((root) => root.id));
  const problems: string[] = [];
  let reportedCycle = false;
  let frontier = roots.flatMap((root) => (outgoing.get(root.id) ?? []).filter((id) => !visited.has(id)));

  while (frontier.length) {
    const nextFrontier: string[] = [];
    for (const id of frontier) {
      const node = byId.get(id);
      if (visited.has(id)) {
        // Diamond convergence into a union/merge join node (or into an explicit
        // union step) is normal control flow, not a cycle.
        const isConvergence =
          mergeTargets.has(id) ||
          Boolean(node?.data?.isUnionJoin) ||
          node?.data?.actionType === "union";
        if (!isConvergence && !reportedCycle) {
          problems.push("The connector graph has a cycle; actions were ordered by traversal.");
          reportedCycle = true;
        }
        continue;
      }
      visited.add(id);
      if (!node || node.type !== "node") continue;
      if (node.data?.nodeKind === "trigger") {
        problems.push("A trigger can only start a workflow; the mid-chain trigger was ignored.");
        continue;
      }
      if (isSyntheticHelperNode(node) || isBranchChildNode(node)) {
        nextFrontier.push(...(outgoing.get(id) ?? []));
        continue;
      }
      actions.push(node);
      nextFrontier.push(...(outgoing.get(id) ?? []));
    }
    frontier = nextFrontier;
  }

  const lost = shapes.filter(
    (shape) =>
      shape.type === "node" &&
      shape.data?.nodeKind === "action" &&
      !isSyntheticHelperNode(shape) &&
      !isBranchChildNode(shape) &&
      !visited.has(shape.id)
  );
  for (const shape of lost) {
    problems.push(`"${shape.label}" is not connected to a trigger and was not saved.`);
  }
  return { actions, lost, problems };
}

/** Action nodes in run order (trigger-first traversal) — the same order a
    save writes, so the insert-from-previous picker can name each step's
    effective id (`fields.id` or the save-time `action-<n>` fallback) and
    list only the steps that run before a given one. */
export function orderedActionNodes(shapes: DiagramShape[]): DiagramShape[] {
  const triggerNodes = shapes
    .filter((shape) => shape.type === "node" && shape.data?.nodeKind === "trigger")
    .sort((a, b) => a.y - b.y || a.x - b.x);
  return triggerNodes.length ? orderedActions(shapes, triggerNodes).actions : [];
}

/** The id a save writes for one action node: its own `fields.id`, else the
    `action-<n>` fallback from its run-order position (or the hierarchical
    `<parent>.<branch>.<step_id>` scoped ID for a step inside a branch).
    Null for a node not connected to a trigger (a save drops it). */
export function effectiveActionId(shapes: DiagramShape[], nodeId: string): string | null {
  const chain = orderedActionNodes(shapes);
  const index = chain.findIndex((node) => node.id === nodeId);
  if (index >= 0) {
    return (chain[index].data?.fields?.id ?? "").trim() || `action-${index + 1}`;
  }
  const node = shapes.find((shape) => shape.id === nodeId);
  if (!node || node.type !== "node" || !isBranchChildNode(node)) return null;
  const parentId = node.data?.parentBranchNodeId;
  const branchKey = node.data?.branchKey;
  if (!parentId || !branchKey) return null;
  const parentEffectiveId = effectiveActionId(shapes, parentId);
  if (!parentEffectiveId) return null;
  const localId = (node.data?.fields?.id ?? "").trim() || `step-${(node.data?.branchIndex ?? 0) + 1}`;
  return `${parentEffectiveId}.${branchKey}.${localId}`;
}

export function filterRulesToYaml(rules: FilterRule[] | undefined, problems: string[] = []): Record<string, Record<string, unknown>> {
  const filters: Record<string, Record<string, unknown>> = {};
  for (const rule of rules ?? []) {
    const field = rule.field.trim();
    if (!field) continue;
    let value: unknown = rule.value;
    if (rule.operator === "in") {
      try {
        value = JSON.parse(rule.value);
        if (!Array.isArray(value)) throw new Error("not a list");
      } catch {
        problems.push(`Filter ${field}: in needs a JSON list, such as ["invoice", "receipts"].`);
      }
    } else if (rule.operator === "exists" || rule.operator === "empty") {
      if (rule.value !== "true" && rule.value !== "false") {
        problems.push(`Filter ${field}: ${rule.operator} needs true or false.`);
      }
      value = rule.value === "true";
    } else if (rule.value === "true" || rule.value === "false") {
      value = rule.value === "true";
    }
    filters[field] = { ...filters[field], [rule.operator]: value };
  }
  return filters;
}

function actionToYaml(
  node: DiagramShape,
  index: number,
  problems: string[],
  allShapes: DiagramShape[] = [],
  defaultIdPrefix = "action"
): Record<string, unknown> {
  const data = node.data ?? defaultNodeData("action");
  const type = data.actionType ?? "webhook";
  const action: Record<string, unknown> = {
    id: (data.fields?.id ?? "").trim() || `${defaultIdPrefix}-${index + 1}`,
    type
  };
  const meta = actionMeta(type);
  if (!meta) {
    // Unknown action type: write the original YAML back untouched.
    return { ...action, ...(data.raw ?? {}) };
  }
  const written: Record<string, unknown> = {};
  // The error-handling keys are the engine's generic step keys, owned here
  // for every catalog action exactly like the per-entry fields.
  for (const field of [...meta.fields, ...errorHandlingFields]) {
    // Presence-based: keys the YAML omits stay omitted (engine defaults
    // apply), keys it sets — or the inspector touches — are written back.
    if (field.type === "boolean") {
      const raw = data.fields?.[field.key];
      if (raw === undefined) continue;
      fieldTarget(written, field)[field.key] = raw === "true";
      continue;
    }
    if (field.type === "json") {
      const text = (data.fields?.[field.key] ?? "").trim();
      if (text === "") continue;
      try {
        const parsed: unknown = JSON.parse(text);
        if (!isRecord(parsed) && !Array.isArray(parsed)) {
          problems.push(`"${node.label ?? type}": ${field.label} must be a JSON object or array.`);
          continue;
        }
        fieldTarget(written, field)[field.key] = parsed;
      } catch {
        problems.push(`"${node.label ?? type}": ${field.label} is not valid JSON.`);
      }
      continue;
    }
    if (field.type === "yaml") {
      const text = (data.fields?.[field.key] ?? "").trim();
      if (text === "") continue;
      try {
        const parsed: unknown = load(text);
        if (type === "union" && field.key === "fields") {
          if (!isRecord(parsed)) {
            problems.push(`"${node.label ?? type}": ${field.label} must be a YAML mapping.`);
            continue;
          }
        } else if (!Array.isArray(parsed)) {
          problems.push(`"${node.label ?? type}": ${field.label} must be a YAML list of steps.`);
          continue;
        }
        fieldTarget(written, field)[field.key] = parsed;
      } catch {
        problems.push(`"${node.label ?? type}": ${field.label} is not valid YAML.`);
      }
      continue;
    }
    const rawValue = data.fields?.[field.key] ?? "";
    const value = field.key === "prompt" || field.key === "system" ? rawValue : rawValue.trim();
    if (value.trim() === "") continue;
    // halt is the engine default for both policies (engine/logic.py): the
    // keys are written only for the non-default modes, so steps that never
    // handled a failure save byte-identical YAML.
    if ((field.key === "on_error" || field.key === "on_fail") && value === "halt") continue;
    const parsed = field.type === "number" ? Number(value) : value;
    fieldTarget(written, field)[field.key] = field.type === "number" && Number.isFinite(parsed) ? parsed : value;
  }
  // Extras the catalog does not model (nested branch/loop steps, hand-written
  // keys) round-trip untouched; the form's values win where both exist.
  const merged: Record<string, unknown> = { ...action, ...(data.raw ?? {}), ...written };
  for (const field of [...meta.fields, ...errorHandlingFields]) {
    // A cleared yaml field removes the key (and any stale extra under it).
    if ((field.type === "yaml" || field.type === "json") && !(data.fields?.[field.key] ?? "").trim()) {
      delete merged[field.key];
    }
  }

  // Keep `when` and flat `field`/`operator`/`value` consistent on condition/filter:
  // if the user set flat `field`, drop any stale `raw.when`; if `raw.when` is used
  // without flat `field`, omit empty flat keys.
  if (type === "condition" || type === "filter") {
    if ((data.fields?.field ?? "").trim()) {
      delete merged.when;
    } else if (isRecord(data.raw?.when) || Array.isArray(data.raw?.when)) {
      merged.when = data.raw.when;
      delete merged.field;
      delete merged.operator;
      delete merged.value;
    }
  }

  // Reconstruct nested branch steps from materialized canvas child nodes when present.
  const canvasBranchNodes = allShapes.filter(
    (s) => s.type === "node" && s.data?.parentBranchNodeId === node.id
  );
  if (canvasBranchNodes.length > 0 && isBranchingAction(type)) {
    const serializeBranchChildren = (branchKey: string): Array<Record<string, unknown>> => {
      const matchDefault = branchKey === "else" || branchKey === "default";
      const children = canvasBranchNodes
        .filter((s) => {
          const k = s.data?.branchKey;
          const matchesKey = k === branchKey || (matchDefault && (k === "else" || k === "default"));
          return matchesKey && !s.data?.isBranchPlaceholder && !s.data?.isUnionJoin;
        })
        .sort((a, b) => (a.data?.branchIndex ?? 0) - (b.data?.branchIndex ?? 0) || a.y - b.y);
      return children.map((child, childIdx) => actionToYaml(child, childIdx, problems, allShapes, "step"));
    };

    if (type === "condition") {
      const thenSteps = serializeBranchChildren("then");
      const elseSteps = serializeBranchChildren("else");
      if (thenSteps.length > 0 || data.fields?.then !== undefined || data.raw?.then !== undefined) {
        merged.then = thenSteps;
      }
      if (elseSteps.length > 0 || (data.fields?.else ?? "").trim() !== "" || Array.isArray(data.raw?.else)) {
        merged.else = elseSteps;
      } else {
        delete merged.else;
      }
    } else if (type === "decision") {
      const mode = decisionMode(data.fields, data.raw);
      if (mode === "if") {
        const thenSteps = serializeBranchChildren("then");
        const elseSteps = serializeBranchChildren("else");
        merged.then = thenSteps;
        if (elseSteps.length > 0 || (data.fields?.else ?? "").trim() !== "" || Array.isArray(data.raw?.else)) {
          merged.else = elseSteps;
        } else {
          delete merged.else;
        }
        delete merged.choices;
        delete merged.cases;
        delete merged.branches;
        delete merged.default_actions;
      } else {
        const branches = extractNodeBranches(data);
        const choiceBranches = branches.filter((b) => !b.isDefault);
        const useBranchesDict =
          (isRecord(data.raw?.branches) || Boolean((data.fields?.branches ?? "").trim())) &&
          !Array.isArray(data.raw?.cases) &&
          !(data.fields?.cases ?? "").trim();

        if (useBranchesDict) {
          const origBranches = isRecord(data.raw?.branches) ? data.raw.branches : {};
          const nextBranches: Record<string, unknown> = {};
          for (const b of choiceBranches) {
            const origEntry = origBranches[b.key];
            const steps = serializeBranchChildren(b.key);
            if (isRecord(origEntry) || (b.description !== undefined && b.description.trim() !== "")) {
              const entryObj: Record<string, unknown> = isRecord(origEntry) ? { ...origEntry } : {};
              if (b.description !== undefined && b.description.trim() !== "") {
                entryObj.description = b.description;
              } else if (b.description === "") {
                delete entryObj.description;
              }
              entryObj.actions = steps;
              nextBranches[b.key] = entryObj;
            } else {
              nextBranches[b.key] = steps;
            }
          }
          merged.branches = nextBranches;
        } else {
          const origCases = parseYamlArray(data.fields?.cases, data.raw?.cases).filter(isRecord);
          merged.choices = choiceBranches.map((b) => b.key);
          merged.cases = choiceBranches.map((b) => {
            const orig = origCases.find((c) => String(c.choice ?? c.label ?? "").trim() === b.key) ?? {};
            const caseEntry: Record<string, unknown> = { ...orig, choice: b.key };
            if (b.description !== undefined && b.description.trim() !== "") {
              caseEntry.description = b.description;
            } else if (b.description === "") {
              delete caseEntry.description;
            }
            caseEntry.actions = serializeBranchChildren(b.key);
            return caseEntry;
          });
        }
        const defaultSteps = serializeBranchChildren("default");
        const defaultKey =
          data.raw?.else !== undefined && data.raw?.default_actions === undefined
            ? "else"
            : data.raw?.default !== undefined && data.raw?.default_actions === undefined
              ? "default"
              : "default_actions";
        if (
          defaultSteps.length > 0 ||
          (data.fields?.default_actions ?? "").trim() !== "" ||
          Array.isArray(data.raw?.[defaultKey])
        ) {
          merged[defaultKey] = defaultSteps;
        } else {
          delete merged.default_actions;
        }
      }
    } else if (type === "paths") {
      const branches = extractNodeBranches(data);
      const pathBranches = branches.filter((b) => !b.isDefault);
      const origPaths = parseYamlArray(data.fields?.paths, data.raw?.paths).filter(isRecord);
      merged.paths = pathBranches.map((b, idx) => {
        const orig = origPaths.find((p) => String(p.label ?? p.id ?? "").trim() === b.key) ?? origPaths[idx] ?? {};
        const pathEntry: Record<string, unknown> = { ...orig, label: b.label };
        if (b.when && Object.keys(b.when).length > 0 && !orig.field) {
          pathEntry.when = b.when;
        }
        pathEntry.actions = serializeBranchChildren(b.key);
        return pathEntry;
      });
      const defaultSteps = serializeBranchChildren("default");
      const defaultKey = data.raw?.else !== undefined && data.raw?.default === undefined ? "else" : "default";
      if (
        defaultSteps.length > 0 ||
        (data.fields?.default ?? "").trim() !== "" ||
        Array.isArray(data.raw?.[defaultKey])
      ) {
        merged[defaultKey] = defaultSteps;
      } else {
        delete merged.default;
      }
    }
  }

  return merged;
}

function triggerToYaml(data: NodeData, problems: string[]): TriggerSpec {
  return {
    connector: data.connector ?? "custom",
    event: data.event?.trim() || "received",
    filters: filterRulesToYaml(data.filters, problems)
  };
}

export function workflowFromShapes(
  shapes: DiagramShape[],
  workflowId: string,
  enabled: boolean,
  base?: Workflow | null
): { workflow: Workflow; problems: string[]; lost: DiagramShape[] } {
  const problems: string[] = [];
  const triggerNodes = shapes
    .filter((shape) => shape.type === "node" && shape.data?.nodeKind === "trigger")
    .sort((a, b) => a.y - b.y || a.x - b.x);
  if (triggerNodes.length === 0) problems.push("Add a trigger node before saving.");

  const { actions, lost, problems: chainProblems } = triggerNodes.length
    ? orderedActions(shapes, triggerNodes)
    : { actions: [], lost: [], problems: [] };
  if (triggerNodes.length && actions.length === 0) problems.push("Connect at least one action to the trigger.");

  const workflow: Workflow = {
    ...base,
    id: workflowId.trim() || "untitled-workflow",
    enabled
  };
  delete workflow.trigger;
  delete workflow.triggers;
  workflow.actions = actions.map((action, index) => actionToYaml(action, index, problems, shapes));
  if (triggerNodes.length === 1) {
    workflow.trigger = triggerToYaml(triggerNodes[0].data!, problems);
  } else if (triggerNodes.length > 1) {
    workflow.triggers = triggerNodes.map((node) => triggerToYaml(node.data!, problems));
  }
  return { workflow, problems: [...problems, ...chainProblems], lost };
}

function yamlFiltersToRules(filters: Record<string, Record<string, unknown>> | undefined): FilterRule[] {
  return Object.entries(filters ?? {}).flatMap(([field, rule]) => {
    if (!rule || typeof rule !== "object") return [{ field, operator: "equals" as const, value: String(rule ?? "") }];
    return Object.entries(rule).map(([operator, value]) => ({
      field,
      operator: (filterOperators as readonly string[]).includes(operator)
        ? operator as FilterRule["operator"]
        : "equals" as const,
      value: Array.isArray(value) ? JSON.stringify(value) : String(value ?? "")
    }));
  });
}

/** Catalog-owned keys of an action; anything else only survives via `raw`. */
function actionFields(type: ActionType, action: Record<string, unknown>): Record<string, string> {
  const fields: Record<string, string> = { id: String(action.id ?? "") };
  const meta = actionMeta(type);
  if (!meta) return fields;
  for (const field of [...meta.fields, ...errorHandlingFields]) {
    const holder = field.group ? action[field.group] : action;
    let value = isRecord(holder) ? holder[field.key] : undefined;
    // Normalize fallback aliases onto the catalog field key (`else` on paths -> `default`,
    // `else`/`default` on decision[switch] -> `default_actions`).
    if (value === undefined && !field.group) {
      if (type === "paths" && field.key === "default" && Array.isArray(action.else)) {
        value = action.else;
      } else if (type === "decision" && field.key === "default_actions") {
        if (Array.isArray(action.default)) value = action.default;
        else if (Array.isArray(action.else) && (action.mode ?? "switch") === "switch") value = action.else;
      }
    }
    if (field.type === "json") {
      if (value !== undefined) fields[field.key] = typeof value === "string" ? value : JSON.stringify(value, null, 2);
      continue;
    }
    if (field.type === "yaml") {
      // Sub-step lists (condition branches, loop bodies) and union field maps edit as YAML text.
      if (Array.isArray(value) || isRecord(value)) {
        fields[field.key] = dump(value, { lineWidth: 100 }).trim();
      }
      continue;
    }
    // Keys the YAML omits stay unset: the inspector falls back to the
    // field default, and saving omits them again.
    if (value !== undefined) fields[field.key] = String(value);
  }
  return fields;
}

/** Everything but id/type, for actions the catalog does not model. */
function rawAction(action: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(Object.entries(action).filter(([key]) => key !== "id" && key !== "type"));
}

/**
 * Keys of a catalog-modeled action the inspector form does not own — nested
 * branch/loop steps, hand-written extras. They ride along in `raw` so a save
 * never drops them; the form's own fields win where both exist.
 */
function rawExtras(action: Record<string, unknown>): Record<string, unknown> | undefined {
  const extras = rawAction(action);
  const type = String(action.type ?? "webhook");
  const meta = actionMeta(type);
  for (const field of [...(meta?.fields ?? []), ...(meta ? errorHandlingFields : [])]) {
    if (!field.group) {
      // Keep original empty arrays on branch keys in raw so empty `then: []` vs omitted `else`
      // is distinguishable on round-trip.
      if (
        field.type === "yaml" &&
        Array.isArray(action[field.key]) &&
        (action[field.key] as unknown[]).length === 0
      ) {
        extras[field.key] = [];
      } else {
        delete extras[field.key];
      }
      continue;
    }
    const group = extras[field.group];
    if (isRecord(group)) {
      const rest = { ...group };
      delete rest[field.key];
      if (Object.keys(rest).length) extras[field.group] = rest;
      else delete extras[field.group];
    }
  }
  return Object.keys(extras).length ? extras : undefined;
}

function makeNodeDataFromAction(action: Record<string, unknown>): NodeData {
  const type = String(action.type ?? "webhook");
  const extras = rawExtras(action);
  const data: NodeData = {
    nodeKind: "action",
    actionType: type,
    fields: actionFields(type, action),
    ...(extras ? { raw: extras } : {})
  };
  if (isBranchingAction(type)) {
    data.branchPills = extractNodeBranches(data).map((b) => b.label);
  }
  return data;
}

/** Computes the horizontal width required by an action step (including any
    fan-out of parallel branch columns and nested branch splits). */
function actionSubtreeWidth(action: Record<string, unknown>): number {
  const type = String(action.type ?? "webhook");
  if (!isBranchingAction(type)) return NODE_WIDTH;
  const data = makeNodeDataFromAction(action);
  const branches = extractNodeBranches(data);
  if (branches.length === 0) return NODE_WIDTH;
  const branchWidths = branches.map((b) => branchColumnWidth(b.steps));
  const total = branchWidths.reduce((sum, w) => sum + w, 0) + BRANCH_GAP_X * Math.max(0, branches.length - 1);
  return Math.max(NODE_WIDTH, total);
}

function branchColumnWidth(steps: Array<Record<string, unknown>>): number {
  if (steps.length === 0) return NODE_WIDTH;
  return Math.max(NODE_WIDTH, ...steps.map(actionSubtreeWidth));
}

function makeArrow(
  sourceId: string,
  targetId: string,
  extra?: { branchLabel?: string; isBranchEdge?: boolean; isMergeEdge?: boolean }
): DiagramShape {
  return {
    id: `link-${sourceId}-${targetId}`,
    type: "arrow",
    x: 0,
    y: 0,
    width: 0,
    height: 0,
    sourceId,
    targetId,
    sourceHandleId: "bottom",
    targetHandleId: "top",
    ...(extra ?? {})
  };
}

export function shapesFromWorkflow(workflow: Workflow): DiagramShape[] {
  const triggerShapes: DiagramShape[] = [];
  const topActionShapes: DiagramShape[] = [];
  const childActionShapes: DiagramShape[] = [];
  const helperNodeShapes: DiagramShape[] = [];
  const arrowShapes: DiagramShape[] = [];

  const triggers = workflowTriggers(workflow);
  const actions = workflowActions(workflow);

  // Entry points sit in one row, centered over the action chain; a lone
  // trigger keeps the chain's column.
  const TRIGGER_GAP_X = 72;
  const rowWidth = triggers.length * NODE_WIDTH + (triggers.length - 1) * TRIGGER_GAP_X;
  const rowStartX = ORIGIN_X + NODE_WIDTH / 2 - rowWidth / 2;

  const triggerNodes = triggers.map((spec, index) => {
    const node: DiagramShape = {
      id: triggers.length === 1 ? "trigger" : `trigger-${index}`,
      type: "node",
      x: rowStartX + index * (NODE_WIDTH + TRIGGER_GAP_X),
      y: ORIGIN_Y,
      width: NODE_WIDTH,
      height: NODE_HEIGHT,
      label: triggerLabel(spec.connector, spec.event),
      data: {
        nodeKind: "trigger",
        connector: connectorCatalog.some((entry) => entry.name === spec.connector)
          ? spec.connector
          : "custom",
        event: spec.event,
        filters: yamlFiltersToRules(spec.filters)
      }
    };
    triggerShapes.push(node);
    return node;
  });

  function layoutBranchesForNode(
    branchNode: DiagramShape,
    scopedPrefix: string,
    centerX: number,
    nextSiblingIsExplicitUnion: boolean
  ): { exitNodes: Array<{ node: DiagramShape; isMerge: boolean }>; bottomY: number } {
    const branches = extractNodeBranches(branchNode.data!);
    branchNode.data!.branchPills = branches.map((b) => b.label);
    const colWidths = branches.map((b) => branchColumnWidth(b.steps));
    const totalWidth = colWidths.reduce((sum, w) => sum + w, 0) + BRANCH_GAP_X * Math.max(0, branches.length - 1);
    const groupLeft = centerX + NODE_WIDTH / 2 - totalWidth / 2;
    const branchStartY = branchNode.y + branchNode.height + 104;

    let cursorLeft = groupLeft;
    let maxBranchBottomY = branchStartY;
    const branchLeafNodes: DiagramShape[] = [];

    branches.forEach((branch, bIdx) => {
      const colW = colWidths[bIdx];
      const colNodeX = Math.round(cursorLeft + colW / 2 - NODE_WIDTH / 2);
      cursorLeft += colW + BRANCH_GAP_X;

      if (branch.steps.length === 0) {
        const placeholder: DiagramShape = {
          id: `${branchNode.id}-${branch.key}-empty`,
          type: "node",
          x: colNodeX,
          y: branchStartY,
          width: NODE_WIDTH,
          height: BRANCH_PLACEHOLDER_HEIGHT,
          label: `+ Add step`,
          data: {
            nodeKind: "action",
            actionType: "__branch_placeholder__",
            parentBranchNodeId: branchNode.id,
            branchKey: branch.key,
            branchLabel: branch.label,
            branchIndex: 0,
            isBranchPlaceholder: true
          }
        };
        helperNodeShapes.push(placeholder);
        arrowShapes.push(makeArrow(branchNode.id, placeholder.id, {
          branchLabel: branch.label,
          isBranchEdge: true
        }));
        branchLeafNodes.push(placeholder);
        maxBranchBottomY = Math.max(maxBranchBottomY, placeholder.y + placeholder.height);
        return;
      }

      let curY = branchStartY;
      let prevInBranch: DiagramShape = branchNode;
      branch.steps.forEach((stepAction, stepIdx) => {
        const stepType = String(stepAction.type ?? "webhook");
        const stepId = String(stepAction.id ?? "").trim() || `step-${stepIdx + 1}`;
        const scopedId = `${scopedPrefix}.${branch.key}.${stepId}`;
        const childData = makeNodeDataFromAction(stepAction);
        childData.parentBranchNodeId = branchNode.id;
        childData.branchKey = branch.key;
        childData.branchLabel = branch.label;
        childData.branchIndex = stepIdx;
        childData.scopedActionId = scopedId;

        const childNode: DiagramShape = {
          id: `${branchNode.id}-${branch.key}-${stepIdx}`,
          type: "node",
          x: colNodeX,
          y: curY,
          width: NODE_WIDTH,
          height: NODE_HEIGHT,
          label: actionMeta(stepType)?.label ?? stepType,
          data: childData
        };
        childActionShapes.push(childNode);

        if (stepIdx === 0) {
          arrowShapes.push(makeArrow(branchNode.id, childNode.id, {
            branchLabel: branch.label,
            isBranchEdge: true
          }));
        } else {
          arrowShapes.push(makeArrow(prevInBranch.id, childNode.id));
        }

        if (isBranchingAction(stepType)) {
          const nextInBranchIsUnion = String(branch.steps[stepIdx + 1]?.type ?? "") === "union";
          const subLayout = layoutBranchesForNode(childNode, scopedId, colNodeX, nextInBranchIsUnion);
          prevInBranch = subLayout.exitNodes[0]?.node ?? childNode;
          curY = subLayout.bottomY + BRANCH_NODE_GAP_Y;
        } else {
          prevInBranch = childNode;
          curY = childNode.y + childNode.height + BRANCH_NODE_GAP_Y;
        }
      });

      branchLeafNodes.push(prevInBranch);
      maxBranchBottomY = Math.max(maxBranchBottomY, prevInBranch.y + prevInBranch.height);
    });

    if (nextSiblingIsExplicitUnion) {
      return {
        exitNodes: branchLeafNodes.map((n) => ({ node: n, isMerge: true })),
        bottomY: maxBranchBottomY
      };
    }

    const unionY = maxBranchBottomY + 88;
    const unionNode: DiagramShape = {
      id: `${branchNode.id}-union`,
      type: "node",
      x: centerX,
      y: unionY,
      width: NODE_WIDTH,
      height: UNION_JOIN_HEIGHT,
      label: "Union",
      data: {
        nodeKind: "action",
        actionType: "__union_join__",
        isUnionJoin: true,
        unionForNodeId: branchNode.id,
        branchPills: branches.map((b) => b.label),
        ...(branchNode.data?.parentBranchNodeId ? { parentBranchNodeId: branchNode.data.parentBranchNodeId } : {})
      }
    };
    helperNodeShapes.push(unionNode);
    for (const leaf of branchLeafNodes) {
      arrowShapes.push(makeArrow(leaf.id, unionNode.id, { isMergeEdge: true }));
    }
    return {
      exitNodes: [{ node: unionNode, isMerge: false }],
      bottomY: unionNode.y + unionNode.height
    };
  }

  let currentY = ORIGIN_Y + (triggers.length ? NODE_HEIGHT + NODE_GAP_Y : 0);
  let previousSources: Array<{ node: DiagramShape; isMerge: boolean }> =
    triggerNodes.map((t) => ({ node: t, isMerge: false }));

  actions.forEach((action, index) => {
    const type = String(action.type ?? "webhook");
    const data = makeNodeDataFromAction(action);
    const node: DiagramShape = {
      id: `action-${index}`,
      type: "node",
      x: ORIGIN_X,
      y: currentY,
      width: NODE_WIDTH,
      height: NODE_HEIGHT,
      label: actionMeta(type)?.label ?? String(type),
      data
    };
    topActionShapes.push(node);

    for (const source of previousSources) {
      arrowShapes.push(makeArrow(
        source.node.id,
        node.id,
        source.isMerge ? { isMergeEdge: true } : undefined
      ));
    }

    if (isBranchingAction(type)) {
      const effectiveId = String(action.id ?? "").trim() || `action-${index + 1}`;
      const nextIsExplicitUnion = String(actions[index + 1]?.type ?? "") === "union";
      const branchLayout = layoutBranchesForNode(node, effectiveId, ORIGIN_X, nextIsExplicitUnion);
      previousSources = branchLayout.exitNodes;
      currentY = branchLayout.bottomY + (nextIsExplicitUnion ? 88 : NODE_GAP_Y);
    } else {
      previousSources = [{ node, isMerge: false }];
      currentY = node.y + node.height + NODE_GAP_Y;
    }
  });

  return [
    ...triggerShapes,
    ...topActionShapes,
    ...childActionShapes,
    ...helperNodeShapes,
    ...arrowShapes
  ];
}

/** Syncs every branching node's YAML fields (`then`, `else`, `cases`, `choices`,
    `default_actions`, `paths`, `default`) from its materialized child branch
    nodes on the canvas so Inspector textareas and `data.fields` stay current
    whenever a branch child node is edited. */
export function syncBranchFieldsFromCanvas(shapes: DiagramShape[], _targetParentId?: string): DiagramShape[] {
  const branchingNodes = shapes.filter(
    (s) => s.type === "node" && isBranchingAction(s.data?.actionType)
  );
  if (branchingNodes.length === 0) return shapes;
  // Process bottom-up (deepest y first) so nested branches serialize into their parent first.
  const sortedIds = [...branchingNodes]
    .sort((a, b) => b.y - a.y)
    .map((n) => n.id);

  let current = shapes;
  for (const branchNodeId of sortedIds) {
    const hasChildren = current.some(
      (s) => s.type === "node" && s.data?.parentBranchNodeId === branchNodeId
    );
    if (!hasChildren) continue;
    current = current.map((shape) => {
      if (shape.id !== branchNodeId || !shape.data) return shape;
      const serialized = actionToYaml(shape, 0, [], current);
      const updatedFields = actionFields(shape.data.actionType ?? "condition", serialized);
      const updatedData: NodeData = {
        ...shape.data,
        raw: serialized,
        fields: {
          ...shape.data.fields,
          ...updatedFields,
          id: shape.data.fields?.id ?? ""
        }
      };
      updatedData.branchPills = extractNodeBranches(updatedData).map((b) => b.label);
      return { ...shape, data: updatedData };
    });
  }
  return current;
}

/** Rebuilds the 2D canvas layout after adding/removing/reordering branch steps
    or editing branch structure in the Inspector. If `stripChildrenOfNodeId` is
    given, existing canvas child nodes of that branching node are removed first
    so its freshly updated `data.fields` drives the new layout. */
export function rebuildShapesFromBranchEdit(
  shapes: DiagramShape[],
  workflowId: string,
  enabled: boolean,
  base?: Workflow | null,
  stripChildrenOfNodeId?: string
): DiagramShape[] {
  let working = shapes;
  if (stripChildrenOfNodeId) {
    const toRemove = new Set<string>();
    const collectDescendants = (parentId: string) => {
      for (const s of working) {
        if (s.type === "node" && (s.data?.parentBranchNodeId === parentId || s.data?.unionForNodeId === parentId)) {
          toRemove.add(s.id);
          collectDescendants(s.id);
        }
      }
    };
    collectDescendants(stripChildrenOfNodeId);
    working = working.filter(
      (s) => !toRemove.has(s.id) && (!s.sourceId || !toRemove.has(s.sourceId)) && (!s.targetId || !toRemove.has(s.targetId))
    );
  }
  const notes = working.filter((s) => s.type === "note");
  const { workflow } = workflowFromShapes(working, workflowId, enabled, base);
  return [...shapesFromWorkflow(workflow), ...notes];
}

export function summarize(source: string, workflow: Workflow): WorkflowSummary {
  const primary = workflowTriggers(workflow)[0];
  return {
    id: workflow.id,
    enabled: workflow.enabled !== false,
    source,
    connector: primary?.connector ?? "?",
    event: primary?.event ?? "?",
    actionCount: workflowActions(workflow).length
  };
}

