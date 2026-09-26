import { Code2, DatabaseZap, FileText, Filter, GitBranch, ListTree, Send, Timer, Video, Webhook } from "lucide-react";
import type { ReactNode } from "react";
import { DropboxLogo, MailLogo, S3Logo, SheetsLogo, SlackLogo, YouTubeLogo } from "./logos";

/**
 * The node catalog — the single place to edit when the designer should know a
 * new engine action (see run_* dispatch in src/dapier/engine/__init__.py), an
 * in-workflow logic step (filter, condition, delay, for_each — executed by
 * src/dapier/engine/logic.py), or trigger connector.
 *
 * Adding an action is one object in `actionCatalog`:
 *   1. type   — the exact string written to the action's `type` YAML key;
 *   2. label  — shown in the palette, canvas nodes and inspector;
 *   3. icon   — a lucide-react icon or a product logo from logos.tsx
 *      (optional, falls back to FileText);
 *   4. fields — one entry per YAML key the engine reads. `group` nests the key
 *      under an object (e.g. group: "pdf" → action.pdf.page_format), `type`
 *      picks the inspector widget and YAML coercion ("number" writes numbers,
 *      "boolean" writes true/false and stays out of the YAML while it matches
 *      its default, "select" offers fixed choices), `default` prefills new
 *      nodes and fills in values read from YAML that omit the key.
 *
 * Connectors are the products workflows hook into; each entry in
 * `connectorCatalog` renders as one trigger chip in the palette's Triggers
 * group (logo + label) and contributes the events its trigger suggests.
 *
 * Action types that are not in the catalog are still safe: the designer keeps
 * them as opaque nodes and round-trips their YAML untouched, so hand-written
 * workflows are never mangled on save.
 */

/** Props every icon (lucide or logo) accepts; logos ignore color/strokeWidth. */
export interface IconProps {
  size?: number | string;
  className?: string;
  color?: string;
  strokeWidth?: number | string;
  x?: number | string;
  y?: number | string;
}

export type IconComponent = (props: IconProps) => ReactNode;

export interface CatalogField {
  key: string;
  label: string;
  placeholder?: string;
  required?: boolean;
  /** Inspector widget and YAML coercion; default "text". "yaml" edits a list/object as YAML text. */
  type?: "text" | "number" | "textarea" | "boolean" | "select" | "yaml";
  /** Choices for type: "select". */
  options?: string[];
  /** Value assumed when absent; prefills new nodes and YAML round-trips. */
  default?: string;
  /** Nest under this object in the action YAML, e.g. group: "pdf" → action.pdf.page_format. */
  group?: string;
  /**
   * The connection record's provider this field must name (a `connection_id`
   * key). The inspector suggests the operator's connections of that provider
   * by display name and verified identity instead of a bare ID field.
   */
  provider?: string;
}

export interface ActionEntry {
  /** Exact value of the action's `type` key in the workflow YAML. */
  type: string;
  label: string;
  description?: string;
  /** Lucide icon or product logo, shown on palette chips and canvas nodes. */
  icon?: IconComponent;
  /** Order is the order the inspector renders and the YAML is written. */
  fields: CatalogField[];
}

export interface ConnectorEntry {
  /** Value of the trigger's `connector` key in the workflow YAML. */
  name: string;
  /** Product name shown on the trigger chip, node title and inspector. */
  label: string;
  /** Product logo from logos.tsx. */
  logo: IconComponent;
  /** Events offered as suggestions for this connector's trigger. */
  events: string[];
}

/**
 * Operators the in-workflow logic steps (filter, condition) accept — the same
 * matching engine as trigger filters, plus "in".
 */
export const logicOperators = ["equals", "in", "prefix", "suffix", "contains"] as const;

/** Mirrors the run_* dispatch in src/dapier/engine/__init__.py. */
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
    icon: SlackLogo,
    fields: [
      { key: "credential_id", label: "Credential ID" },
      { key: "connection_id", label: "Slack connection", placeholder: "resolves the credential", provider: "slack" },
      { key: "channel", label: "Channel", placeholder: "#alerts", required: true },
      { key: "text", label: "Text template", type: "textarea", placeholder: "{title}\n{url}" },
      { key: "timeout_seconds", label: "Timeout (s)", type: "number" },
      { key: "unfurl_links", label: "Unfurl links", type: "boolean", default: "true" },
      { key: "unfurl_media", label: "Unfurl media", type: "boolean", default: "true" }
    ]
  },
  {
    type: "telegram_send",
    label: "Telegram",
    icon: Send,
    description: "Post a message through a Telegram bot connection. The chat defaults to the triggering Telegram message; other triggers name the chat explicitly.",
    fields: [
      { key: "connection_id", label: "Bot connection", required: true, provider: "telegram" },
      { key: "chat_id", label: "Chat ID", placeholder: "defaults to the triggering chat" },
      { key: "text", label: "Text template", type: "textarea", placeholder: "{text}" },
      { key: "timeout_seconds", label: "Timeout (s)", type: "number" }
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
      { key: "connection_id", label: "Dropbox connection", placeholder: "dropbox — for file-event intakes", provider: "dropbox" },
      { key: "filename", label: "Filename override" },
      { key: "timeout_seconds", label: "Timeout (s)", type: "number" }
    ]
  },
  {
    type: "dropbox_upload",
    label: "Dropbox upload",
    icon: DropboxLogo,
    fields: [
      { key: "connection_id", label: "Dropbox connection", placeholder: "dropbox", required: true, provider: "dropbox" },
      { key: "source", label: "Source", type: "select", options: ["attachment", "output"], default: "attachment" },
      { key: "folder", label: "Folder", placeholder: "/Invoices" },
      { key: "filename", label: "Filename override" }
    ]
  },
  {
    type: "dropbox_delete",
    label: "Dropbox delete",
    icon: DropboxLogo,
    fields: [
      { key: "connection_id", label: "Dropbox connection", placeholder: "dropbox", required: true, provider: "dropbox" },
      { key: "path", label: "Path", placeholder: "defaults to the event's file path" }
    ]
  },
  {
    type: "s3_upload",
    label: "Amazon S3",
    icon: S3Logo,
    description: "Upload a file to an S3 bucket with stored AWS keys (Upload File). The file comes from source_url or a staged source_s3 {bucket, key}.",
    fields: [
      { key: "credential_id", label: "Credential ID", placeholder: "aws (default)" },
      { key: "bucket", label: "Bucket", placeholder: "datatalks-mailchimp-backup", required: true },
      { key: "key", label: "Object key", placeholder: "mailchimp/{name}", required: true },
      { key: "source_url", label: "Source URL", placeholder: "https://www.googleapis.com/drive/v3/files/{id}?alt=media" },
      { key: "source_connection_id", label: "Source connection ID", placeholder: "google-drive — authorizes the source URL", provider: "google" },
      { key: "content_type", label: "Content type", placeholder: "defaults to the trigger's mimeType" }
    ]
  },
  {
    type: "sheets_append_row",
    label: "Google Sheets",
    icon: SheetsLogo,
    description: "Append a row to a worksheet (Create Spreadsheet Row)",
    fields: [
      { key: "connection_id", label: "Google connection", placeholder: "google", required: true, provider: "google" },
      { key: "spreadsheet_id", label: "Spreadsheet ID", placeholder: "from the sheet URL", required: true },
      { key: "sheet_name", label: "Worksheet", placeholder: "todo (default Sheet1)" },
      { key: "values", label: "Row values (JSON)", type: "textarea", required: true,
        placeholder: '["{trigger.occurred_at|date_format:%Y-%m-%d}", "{text}", "", "NEW"]' },
      { key: "value_input_option", label: "Input option", type: "select", options: ["USER_ENTERED", "RAW"], default: "USER_ENTERED" }
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
  },
  {
    type: "filter",
    label: "Filter",
    icon: Filter,
    description: "Stop the chain quietly unless the condition holds",
    fields: [
      { key: "field", label: "Field", placeholder: "subject", required: true },
      { key: "operator", label: "Operator", type: "select", options: [...logicOperators], default: "equals" },
      { key: "value", label: "Value", placeholder: "invoice" }
    ]
  },
  {
    type: "condition",
    label: "Condition",
    icon: GitBranch,
    description: "Run the then steps, or the else steps",
    fields: [
      { key: "field", label: "Field", placeholder: "route", required: true },
      { key: "operator", label: "Operator", type: "select", options: [...logicOperators], default: "equals" },
      { key: "value", label: "Value" },
      { key: "then", label: "Then steps (YAML)", type: "yaml", placeholder: "- id: notify\n  type: slack\n  channel: \"#alerts\"\n  text: \"{subject}\"" },
      { key: "else", label: "Else steps (YAML)", type: "yaml" }
    ]
  },
  {
    type: "delay",
    label: "Delay",
    icon: Timer,
    description: "Pause the chain before the next step (max 60s)",
    fields: [
      { key: "seconds", label: "Seconds (max 60)", type: "number", required: true, placeholder: "30" }
    ]
  },
  {
    type: "for_each",
    label: "For each",
    icon: ListTree,
    description: "Run steps once per item of a list (max 100 items)",
    fields: [
      { key: "list", label: "List field", placeholder: "attachments", required: true },
      { key: "item", label: "Item variable", default: "item" },
      { key: "max_iterations", label: "Max iterations (max 100)", type: "number" },
      { key: "actions", label: "Steps per item (YAML)", type: "yaml", placeholder: "- id: upload\n  type: dropbox_upload\n  connection_id: dropbox\n  folder: \"/Invoices/{item.filename}\"" }
    ]
  },
  {
    type: "code",
    label: "Code (Python)",
    icon: Code2,
    description: "Sandboxed Python transform: the event data arrives as `input`; the last expression (or an `output` variable) becomes the step result.",
    fields: [
      {
        key: "code",
        label: "Python source",
        type: "textarea",
        required: true,
        placeholder: "# event data is `input`; last expression is the result\n{\"route\": input[\"route\"], \"score\": len(input.get(\"body\",\ \"\"))}"
      },
      { key: "timeout_seconds", label: "Timeout (s)", type: "number" }
    ]
  }
];

export const connectorCatalog: ConnectorEntry[] = [
  { name: "email", label: "Email", logo: MailLogo, events: ["message.received"] },
  { name: "youtube", label: "YouTube", logo: YouTubeLogo, events: ["video.published"] },
  { name: "dropbox", label: "Dropbox", logo: DropboxLogo, events: ["file.created"] },
  { name: "zoom", label: "Zoom", logo: Video, events: ["recording.completed"] },
  { name: "renderer", label: "Renderer", logo: FileText, events: ["job.completed"] },
  { name: "custom", label: "Custom", logo: Webhook, events: [] }
];

export const filterOperators = ["equals", "prefix", "suffix", "contains"] as const;
