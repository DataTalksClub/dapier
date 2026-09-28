export type Tool = "select" | "component";
export type NodeKind = "trigger" | "action" | "note";
export type ShapeType = "node" | "note" | "arrow";
/** Open set — the known actions and their fields live in catalog.ts. */
export type ActionType = string;
export type ConnectorName = string;
export type FilterOperator =
  | "equals" | "not_equals" | "in" | "prefix" | "suffix" | "contains"
  | "does_not_contain" | "gt" | "gte" | "lt" | "lte" | "exists" | "empty";

export interface Point {
  x: number;
  y: number;
}

export interface FilterRule {
  field: string;
  operator: FilterOperator;
  value: string;
}

/** Config payload of a canvas node, mirrored by the inspector form. */
export interface NodeData {
  nodeKind: NodeKind;
  connector?: ConnectorName;
  event?: string;
  filters?: FilterRule[];
  actionType?: ActionType;
  /** Flat action fields as entered in the inspector; grouping/coercion happens on save. */
  fields?: Record<string, string>;
  /**
   * Full YAML of an action whose type is not in the catalog, verbatim
   * (minus id/type) so hand-written workflows survive a round-trip.
   */
  raw?: Record<string, unknown>;
}

export interface DiagramShape {
  id: string;
  type: ShapeType;
  x: number;
  y: number;
  width: number;
  height: number;
  label?: string;
  data?: NodeData;
  sourceId?: string;
  targetId?: string;
  sourceHandleId?: string;
  targetHandleId?: string;
}

export interface TriggerSpec {
  connector: string;
  event: string;
  filters: Record<string, Record<string, unknown>>;
}

/** A named action chain in the file's `flows:` block, bound via `flow:`. */
export interface FlowSpec {
  description?: string;
  actions: Array<Record<string, unknown>>;
}

export interface Workflow {
  id: string;
  enabled: boolean;
  /** Exactly one of trigger / triggers is set. */
  trigger?: TriggerSpec;
  triggers?: TriggerSpec[];
  /** Inline actions, or a `flow` binding whose chain lives in `flows`. */
  actions?: Array<Record<string, unknown>>;
  flow?: string;
  flows?: Record<string, FlowSpec>;
  /** Offered in the template gallery when true (template flag route). */
  template?: boolean;
  description?: string;
}

export interface WorkflowSummary {
  id: string;
  enabled: boolean;
  source: string;
  connector: string;
  event: string;
  actionCount: number;
  description?: string;
  template?: boolean;
  triggerCount?: number;
  tags?: string[];
  /** false: a saved draft with nothing live (it fires nothing until publish). */
  published?: boolean;
  /** A drafted edit sits on top of the live definition (Publish/Discard). */
  has_draft?: boolean;
}

/** The `draft` block the API attaches where a saved draft exists. */
export interface DraftInfo {
  base_revision: number;
  stale: boolean;
  updated_at?: string;
  drafted_by?: string;
}

export interface GitStatus {
  branch: string;
  dirty: boolean;
  ahead: number;
  behind: number;
  lastCommit?: string;
}

/** One of the operator's connections, posted in by the console host
    (designer:set-connections) for the inspector's connection suggestions. */
export interface ConnectionOption {
  connection_id: string;
  provider: string;
  display_name?: string;
  account_title?: string;
  status?: string;
}

/** One action of a test run: what it would receive, or what went wrong. */
export interface TestStepResult {
  action_id: string;
  action_type?: string;
  rendered_input?: Record<string, unknown> | null;
  ok?: boolean;
  output?: Record<string, unknown>;
  error?: string;
  warnings?: string[];
  status?: string;
  duration_ms?: number;
}

/** Payload of POST /designer/workflows/{file}/test (dry-run or execute) and
    POST /designer/workflows/test-step (one step, dry or live). */
export interface TestRunResult {
  file?: string;
  mode: "dry-run" | "execute" | "test-step" | "execute-step";
  action_id?: string;
  matched: boolean;
  enabled?: boolean;
  ok?: boolean;
  event?: Record<string, unknown>;
  steps: TestStepResult[];
  error?: string;
}
