import {
  DatabaseZap,
  FileText,
  MessageSquare,
  Trash2,
  Upload,
  Webhook,
  type LucideIcon
} from "lucide-react";

/**
 * The node catalog — the single place to edit when the designer should know a
 * new engine action (see run_* dispatch in src/engine.py) or trigger connector.
 *
 * Adding an action is one object in `actionCatalog`:
 *   1. type   — the exact string written to the action's `type` YAML key;
 *   2. label  — shown in the palette, canvas nodes and inspector;
 *   3. icon   — any lucide-react icon (optional, falls back to FileText);
 *   4. fields — one entry per YAML key the engine reads. `group` nests the key
 *      under an object (e.g. group: "pdf" → action.pdf.page_format), `type`
 *      picks the inspector widget and YAML coercion ("number" writes numbers,
 *      "boolean" writes true/false and stays out of the YAML while it matches
 *      its default, "select" offers fixed choices), `default` prefills new
 *      nodes and fills in values read from YAML that omit the key.
 *
 * Action types that are not in the catalog are still safe: the designer keeps
 * them as opaque nodes and round-trips their YAML untouched, so hand-written
 * workflows are never mangled on save.
 */

export interface CatalogField {
  key: string;
  label: string;
  placeholder?: string;
  required?: boolean;
  /** Inspector widget and YAML coercion; default "text". */
  type?: "text" | "number" | "textarea" | "boolean" | "select";
  /** Choices for type: "select". */
  options?: string[];
  /** Value assumed when absent; prefills new nodes and YAML round-trips. */
  default?: string;
  /** Nest under this object in the action YAML, e.g. "pdf" → action.pdf.page_format. */
  group?: string;
}

export interface ActionEntry {
  /** Exact value of the action's `type` key in the workflow YAML. */
  type: string;
  label: string;
  description?: string;
  icon?: LucideIcon;
  /** Order is the order the inspector renders and the YAML is written. */
  fields: CatalogField[];
}

export interface ConnectorEntry {
  /** Value of the trigger's `connector` key in the workflow YAML. */
  name: string;
  /** Events offered as suggestions for this connector's trigger. */
  events: string[];
}

/** Mirrors the run_* dispatch in src/engine.py. */
export const actionCatalog: ActionEntry[] = [
  {
    type: "webhook",
    label: "Webhook",
    icon: Webhook,
    fields: [
      { key: "url", label: "URL", required: true },
      { key: "secret_id", label: "Signing secret ID", placeholder: "dapier/webhook" },
      { key: "timeout_seconds", label: "Timeout (s)", type: "number" }
    ]
  },
  {
    type: "slack",
    label: "Slack",
    icon: MessageSquare,
    fields: [
      { key: "credential_id", label: "Credential ID" },
      { key: "connection_id", label: "Connection ID", placeholder: "resolves the credential" },
      { key: "channel", label: "Channel", placeholder: "#alerts", required: true },
      { key: "text", label: "Text template", type: "textarea", placeholder: "{title}\n{url}" },
      { key: "timeout_seconds", label: "Timeout (s)", type: "number" },
      { key: "unfurl_links", label: "Unfurl links", type: "boolean", default: "true" },
      { key: "unfurl_media", label: "Unfurl media", type: "boolean", default: "true" }
    ]
  },
  {
    type: "dataops",
    label: "DataOps intake",
    icon: DatabaseZap,
    fields: [
      { key: "auth_secret_id", label: "Auth secret ID", placeholder: "dapier/dataops", required: true },
      { key: "url_env", label: "URL env var", placeholder: "DATAOPS_INTAKE_URL" },
      { key: "url", label: "URL (overrides env)" },
      { key: "connection_id", label: "Dropbox connection ID", placeholder: "dropbox — for file-event intakes" },
      { key: "filename", label: "Filename override" },
      { key: "timeout_seconds", label: "Timeout (s)", type: "number" }
    ]
  },
  {
    type: "dropbox_upload",
    label: "Dropbox upload",
    icon: Upload,
    fields: [
      { key: "connection_id", label: "Connection ID", placeholder: "dropbox", required: true },
      { key: "source", label: "Source", type: "select", options: ["attachment", "output"], default: "attachment" },
      { key: "folder", label: "Folder", placeholder: "/Invoices" },
      { key: "filename", label: "Filename override" }
    ]
  },
  {
    type: "dropbox_delete",
    label: "Dropbox delete",
    icon: Trash2,
    fields: [
      { key: "connection_id", label: "Connection ID", placeholder: "dropbox", required: true },
      { key: "path", label: "Path", placeholder: "defaults to the event's file path" }
    ]
  },
  {
    type: "render_html_to_pdf",
    label: "Render PDF",
    icon: FileText,
    fields: [
      { key: "input_field", label: "Input field", placeholder: "html" },
      { key: "output_key", label: "Output key", placeholder: "rendered/{event_id}.pdf" },
      { key: "output_bucket", label: "Output bucket" },
      { key: "output_bucket_env", label: "Output bucket env", placeholder: "RENDER_ARTIFACTS_BUCKET" },
      { key: "page_format", label: "PDF page format", group: "pdf", default: "A4" },
      { key: "print_background", label: "Print background", group: "pdf", type: "boolean", default: "true" }
    ]
  }
];

export const connectorCatalog: ConnectorEntry[] = [
  { name: "email", events: ["message.received"] },
  { name: "youtube", events: ["video.published"] },
  { name: "dropbox", events: ["file.created"] },
  { name: "renderer", events: ["job.completed"] },
  { name: "custom", events: [] }
];

export const filterOperators = ["equals", "prefix", "suffix", "contains"] as const;
