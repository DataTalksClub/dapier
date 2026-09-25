export type Tool = "select" | "component";
export type NodeKind = "trigger" | "action" | "note";
export type ShapeType = "node" | "note" | "arrow";
/** Open set — the known actions and their fields live in catalog.ts. */
export type ActionType = string;
export type ConnectorName = string;
export type FilterOperator = "equals" | "prefix" | "suffix" | "contains";

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

export interface Workflow {
  id: string;
  enabled: boolean;
  trigger: {
    connector: string;
    event: string;
    filters: Record<string, Record<string, unknown>>;
  };
  actions: Array<Record<string, unknown>>;
}

export interface WorkflowSummary {
  id: string;
  enabled: boolean;
  source: string;
  connector: string;
  event: string;
  actionCount: number;
}

export interface GitStatus {
  branch: string;
  dirty: boolean;
  ahead: number;
  behind: number;
  lastCommit?: string;
}

/** One action of a test run: what it would receive, or what went wrong. */
export interface TestStepResult {
  action_id: string;
  action_type?: string;
  rendered_input?: Record<string, unknown> | null;
  ok?: boolean;
  output?: Record<string, unknown>;
  error?: string;
}

/** Payload of POST /designer/workflows/{file}/test (dry-run or execute). */
export interface TestRunResult {
  file?: string;
  mode: "dry-run" | "execute";
  matched: boolean;
  enabled?: boolean;
  ok?: boolean;
  event?: Record<string, unknown>;
  steps: TestStepResult[];
  error?: string;
}
