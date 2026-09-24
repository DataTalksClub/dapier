export type Tool = "select" | "component";
export type NodeKind = "trigger" | "action" | "note";
export type ShapeType = "node" | "note" | "arrow";
export type ActionType = "webhook" | "slack" | "dataops" | "dropbox_upload" | "render_html_to_pdf";
export type ConnectorName = "email" | "youtube" | "renderer" | "dropbox" | "custom";
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
  /** Flat action fields, e.g. channel, url, folder; pdf.* is nested on save. */
  fields?: Record<string, string>;
  pdfPageFormat?: string;
  pdfPrintBackground?: boolean;
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
