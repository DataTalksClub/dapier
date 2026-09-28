import { Braces, Clock, Code2, DatabaseZap, FileText, Filter, GitBranch, Globe, Layers, ListTree, Mail, RefreshCw, Send, Timer, Video, Webhook, Workflow } from "lucide-react";
import type { ReactNode } from "react";
import { DropboxLogo, MailLogo, S3Logo, SheetsLogo, SlackLogo, YouTubeLogo } from "./logos";

/**
 * The node catalog — the single place to edit when the designer should know a
 * new engine action (see run_* dispatch in src/dapier/engine/__init__.py), an
 * in-workflow logic step (filter, condition, paths, delay, for_each, digest — executed by
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
 * `on_error`/`error_actions` are not per-entry fields: the engine accepts
 * them on every step (engine/logic.py), so they render once as a generic
 * "Error handling" inspector section and round-trip like catalog fields
 * (errorHandlingFields, consumed by workflows.ts).
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
  /**
   * Offers a Browse… picker over the connector's live resources through the
   * discovery API. `resource` is the serving catalog's bare resource name
   * (the path segment after /discover/); `from` names the sibling field
   * holding the account's connection id (default "connection_id"), which
   * `account` overrides statically (e.g. the "aws" pseudo-connection);
   * `params` maps discovery param names to sibling field keys — only these
   * resolved, non-empty values ride along as the query string (the backend
   * rejects unknown params with 400); `value` templates the picked item
   * into the field (default "{id}", with {key} placeholders filled from
   * the item).
   */
  discover?: {
    resource: string;
    from?: string;
    params?: Record<string, string>;
    account?: string;
    value?: string;
  };
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
 * matching engine as trigger filters (engine/matching.py _matches_filter,
 * mirrored by registry.LOGIC_OPERATORS): compares run against the stringified
 * value; gt/gte/lt/lte compare numerically when both sides parse as numbers,
 * else lexicographically (so ISO dates order correctly); exists/empty test
 * for a present, non-empty value (the expected boolean flips the sense).
 */
export const logicOperators = [
  "equals", "not_equals", "in", "prefix", "suffix", "contains",
  "does_not_contain", "gt", "gte", "lt", "lte", "exists", "empty"
] as const;

/** Mirrors the run_* dispatch in src/dapier/engine/__init__.py. */
export const actionCatalog: ActionEntry[] = [
  {
    type: "webhook",
    label: "Webhook",
    icon: Webhook,
    fields: [
      { key: "url", label: "URL", required: true },
      { key: "payload", label: "Payload (JSON, templated)", type: "textarea", placeholder: '{"id": "{trigger.id}"}' },
      { key: "secret_id", label: "Signing secret ID", placeholder: "dapier/webhook" },
      { key: "timeout_seconds", label: "Timeout (s)", type: "number" }
    ]
  },
  {
    type: "http_request",
    label: "HTTP request",
    icon: Globe,
    description: "Call any API: templated URL, headers and body; basic, bearer or API-key auth. Output: {status, body}.",
    fields: [
      { key: "url", label: "URL", required: true, placeholder: "https://api.example.com/items/{id}" },
      { key: "method", label: "Method", type: "select", options: ["GET", "POST", "PUT", "PATCH", "DELETE"], default: "GET" },
      { key: "auth_type", label: "Auth", type: "select", options: ["none", "basic", "bearer", "api_key"], default: "none" },
      { key: "auth_username", label: "Basic username" },
      { key: "auth_password", label: "Basic password" },
      { key: "auth_token", label: "Bearer token", placeholder: "or a connection ID" },
      { key: "connection_id", label: "Connection ID", placeholder: "bearer token fallback" },
      { key: "auth_key_name", label: "API key name", placeholder: "x-api-key" },
      { key: "auth_key_value", label: "API key value" },
      { key: "auth_key_in", label: "API key in", type: "select", options: ["header", "query"] },
      { key: "headers", label: "Headers (YAML)", type: "textarea", placeholder: "accept: application/json\nx-trace: \"{trigger.id}\"" },
      { key: "body", label: "Body template", type: "textarea", placeholder: '{"subject": "{subject}"}' },
      { key: "content_type", label: "Content type", placeholder: "application/json" },
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
      { key: "channel", label: "Channel", placeholder: "#alerts", required: true,
        discover: { resource: "channels" } },
      { key: "text", label: "Text template", type: "textarea", placeholder: "{title}\n{url}" },
      { key: "timeout_seconds", label: "Timeout (s)", type: "number" },
      { key: "unfurl_links", label: "Unfurl links", type: "boolean", default: "true" },
      { key: "unfurl_media", label: "Unfurl media", type: "boolean", default: "true" },
      { key: "telegram_format", label: "Telegram formatting", type: "boolean",
        placeholder: "renders {text} + entities as Slack blocks; long posts split into a thread" },
      { key: "source_link", label: "Source link template",
        placeholder: "https://t.me/channel/{message_id}" }
    ]
  },
  {
    type: "slack_find_user",
    label: "Slack: find user by email",
    icon: SlackLogo,
    description: "Look up one workspace user by email (users.lookupByEmail). Output: {found, user}.",
    fields: [
      { key: "connection_id", label: "Slack connection", placeholder: "resolves the credential", required: true, provider: "slack" },
      { key: "email", label: "Email", placeholder: "person@example.com", required: true, discover: { resource: "users", value: "{email}" } },
      { key: "credential_id", label: "Credential ID" }
    ]
  },
  {
    type: "slack_find",
    label: "Slack: find user or channel",
    icon: SlackLogo,
    description: "Look up one workspace user (by email) or channel (by name). Output: {found, user} or {found, channel}.",
    fields: [
      { key: "connection_id", label: "Slack connection", placeholder: "resolves the credential", provider: "slack" },
      { key: "find", label: "Find", type: "select", options: ["user", "channel"], default: "user" },
      { key: "query", label: "Query", placeholder: "person@example.com or #channel", required: true },
      { key: "credential_id", label: "Credential ID" }
    ]
  },
  {
    type: "telegram_send",
    label: "Telegram",
    icon: Send,
    description: "Post a message through a Telegram bot connection. The chat defaults to the triggering Telegram message; other triggers name the chat explicitly.",
    fields: [
      { key: "connection_id", label: "Bot connection", required: true, provider: "telegram" },
      { key: "chat_id", label: "Chat ID", placeholder: "defaults to the triggering chat",
        discover: { resource: "chats" } },
      { key: "text", label: "Text template", type: "textarea", placeholder: "{text}" },
      { key: "timeout_seconds", label: "Timeout (s)", type: "number" }
    ]
  },
  {
    type: "telegram_find_chat",
    label: "Telegram find chat",
    icon: Send,
    description: "Look up one chat's profile (getChat); found is False when the bot cannot see it",
    fields: [
      { key: "connection_id", label: "Bot connection", required: true, provider: "telegram" },
      { key: "chat_id", label: "Chat ID", required: true, placeholder: "@channel or -100…",
        discover: { resource: "chats" } }
    ]
  },
  {
    type: "email_send",
    label: "Send email",
    icon: Mail,
    fields: [
      { key: "to", label: "To", required: true, placeholder: "you@example.com or {sender}" },
      { key: "subject", label: "Subject", placeholder: "{subject}" },
      { key: "text", label: "Text body", type: "textarea" },
      { key: "html", label: "HTML body", type: "textarea" },
      { key: "sender", label: "Sender", placeholder: "defaults to the workflow sender" }
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
      { key: "folder", label: "Folder", placeholder: "/Invoices",
        discover: { resource: "folders", value: "{path}" } },
      { key: "filename", label: "Filename override" }
    ]
  },
  {
    type: "dropbox_delete",
    label: "Dropbox delete",
    icon: DropboxLogo,
    fields: [
      { key: "connection_id", label: "Dropbox connection", placeholder: "dropbox", required: true, provider: "dropbox" },
      { key: "path", label: "Path", placeholder: "defaults to the event's file path",
        discover: { resource: "files", value: "{path}" } }
    ]
  },
  {
    type: "dropbox_find",
    label: "Dropbox: find file or folder",
    icon: DropboxLogo,
    description: "Search the connection's Dropbox for one file or folder by name. Output: {found, item}; folder finds can create the folder when missing.",
    fields: [
      { key: "connection_id", label: "Dropbox connection", placeholder: "dropbox", required: true, provider: "dropbox" },
      { key: "query", label: "Name to find", placeholder: "{filename}", required: true,
        discover: { resource: "search", params: { query: "query" }, value: "{name}" } },
      { key: "kind", label: "Kind", type: "select", options: ["any", "file", "folder"], default: "any" },
      { key: "path", label: "Folder to search", placeholder: "defaults to the connection's root",
        discover: { resource: "folders", value: "{path}" } },
      { key: "create_if_missing", label: "Create folder if missing", type: "boolean", default: "false" }
    ]
  },
  {
    type: "s3_upload",
    label: "Amazon S3",
    icon: S3Logo,
    description: "Upload a file to an S3 bucket with stored AWS keys (Upload File). The file comes from source_url or a staged source_s3 {bucket, key}.",
    fields: [
      { key: "credential_id", label: "Credential ID", placeholder: "aws (default)" },
      { key: "bucket", label: "Bucket", placeholder: "datatalks-mailchimp-backup", required: true,
        discover: { resource: "buckets", account: "aws" } },
      { key: "key", label: "Object key", placeholder: "mailchimp/{name}", required: true,
        discover: { resource: "objects", params: { bucket: "bucket" } } },
      { key: "source_url", label: "Source URL", placeholder: "https://www.googleapis.com/drive/v3/files/{id}?alt=media",
        discover: { resource: "files", from: "source_connection_id", value: "https://www.googleapis.com/drive/v3/files/{id}?alt=media" } },
      { key: "source_connection_id", label: "Source connection ID", placeholder: "google-drive — authorizes the source URL", provider: "google" },
      { key: "content_type", label: "Content type", placeholder: "defaults to the trigger's mimeType" }
    ]
  },
  {
    type: "s3_find",
    label: "S3: find object",
    icon: S3Logo,
    description: "Find the first object matching a name pattern in a bucket (Find Object)",
    fields: [
      { key: "credential_id", label: "Credential ID", placeholder: "aws (default)" },
      { key: "bucket", label: "Bucket", placeholder: "datatalks-mailchimp-backup", required: true,
        discover: { resource: "buckets", account: "aws" } },
      { key: "pattern", label: "Name pattern", placeholder: "{name}.pdf", required: true },
      { key: "prefix", label: "Key prefix", placeholder: "reports/2026/",
        discover: { resource: "objects", params: { bucket: "bucket", prefix: "prefix" } } },
      { key: "match", label: "Match", type: "select", options: ["exact", "prefix", "suffix", "contains"], default: "exact" }
    ]
  },
  {
    type: "mailchimp_find_member",
    label: "Mailchimp: find member",
    icon: MailLogo,
    description: "Find one audience member by email. Output: {found, member}; a miss is {found: false, member: null}.",
    fields: [
      { key: "credential_id", label: "Credential ID", placeholder: "mailchimp (default)" },
      { key: "list_id", label: "Audience", required: true,
        discover: { resource: "audiences", account: "mailchimp", value: "{id}" } },
      { key: "email", label: "Email", placeholder: "{sender}", required: true,
        discover: { resource: "members", params: { list_id: "list_id" }, account: "mailchimp", value: "{email}" } }
    ]
  },
  {
    type: "mailchimp_upsert_member",
    label: "Mailchimp",
    icon: MailLogo,
    description: "Add or update one audience member: Status applies to new members; merge fields are a JSON object.",
    fields: [
      { key: "credential_id", label: "Credential ID", placeholder: "mailchimp (default)" },
      { key: "list_id", label: "Audience", required: true,
        discover: { resource: "audiences", account: "mailchimp", value: "{id}" } },
      { key: "email", label: "Email", placeholder: "{sender}", required: true,
        discover: { resource: "members", params: { list_id: "list_id" }, account: "mailchimp", value: "{email}" } },
      { key: "status", label: "Status if new", type: "select",
        options: ["subscribed", "pending", "unsubscribed", "cleaned"], default: "subscribed" },
      { key: "merge_fields", label: "Merge fields (JSON)", placeholder: '{"FNAME": "{name}"}' }
    ]
  },
  {
    type: "drive_find_file",
    label: "Drive: find file",
    icon: FileText,
    description: "Find the most recently modified Drive file matching a name (Find File)",
    fields: [
      { key: "connection_id", label: "Google connection", placeholder: "google", required: true, provider: "google" },
      { key: "name", label: "File name", placeholder: "report.pdf — substring unless Match is exact", required: true,
        discover: { resource: "files", value: "{name}" } },
      { key: "folder", label: "Folder ID", placeholder: "restricts the search to one folder's children",
        discover: { resource: "folders" } },
      { key: "match", label: "Match", type: "select", options: ["contains", "exact"], default: "contains" }
    ]
  },
  {
    type: "youtube_find_video",
    label: "YouTube: find video",
    icon: YouTubeLogo,
    description: "Find the top videos for a search query (Find Video)",
    fields: [
      { key: "connection_id", label: "YouTube connection", placeholder: "youtube", required: true, provider: "youtube" },
      { key: "query", label: "Search query", placeholder: "DataTalks kubernetes", required: true }
    ]
  },
  {
    type: "youtube_find_playlist_items",
    label: "YouTube: playlist videos",
    icon: YouTubeLogo,
    description: "List the videos in a playlist, newest first (Find Playlist Videos). Output: {found, count, videos, video}.",
    fields: [
      { key: "connection_id", label: "YouTube connection", placeholder: "youtube", required: true, provider: "youtube" },
      { key: "playlist_id", label: "Playlist ID", discover: { resource: "playlists" } }
    ]
  },
  {
    type: "sheets_append_row",
    label: "Google Sheets",
    icon: SheetsLogo,
    description: "Append a row to a worksheet (Create Spreadsheet Row)",
    fields: [
      { key: "connection_id", label: "Google connection", placeholder: "google", required: true, provider: "google" },
      { key: "spreadsheet_id", label: "Spreadsheet ID", placeholder: "from the sheet URL", required: true,
        discover: { resource: "spreadsheets" } },
      { key: "sheet_name", label: "Worksheet", placeholder: "todo (default Sheet1)",
        discover: { resource: "worksheets", params: { spreadsheet_id: "spreadsheet_id" }, value: "{name}" } },
      { key: "values", label: "Row values (JSON)", type: "textarea", required: true,
        placeholder: '["{trigger.occurred_at|date_format:%Y-%m-%d}", "{text}", "", "NEW"]' },
      { key: "value_input_option", label: "Input option", type: "select", options: ["USER_ENTERED", "RAW"], default: "USER_ENTERED" }
    ]
  },
  {
    type: "sheets_find_row",
    label: "Google Sheets (find row)",
    icon: SheetsLogo,
    description: "Find a row by a column's value, optionally create it (Find-or-create Spreadsheet Row)",
    fields: [
      { key: "connection_id", label: "Google connection", placeholder: "google", required: true, provider: "google" },
      { key: "spreadsheet_id", label: "Spreadsheet ID", placeholder: "from the sheet URL", required: true,
        discover: { resource: "spreadsheets" } },
      { key: "sheet_name", label: "Worksheet", placeholder: "todo (default Sheet1)",
        discover: { resource: "worksheets", params: { spreadsheet_id: "spreadsheet_id" }, value: "{name}" } },
      { key: "match_field", label: "Match column (header name)", placeholder: "Task", required: true,
        discover: { resource: "columns", params: { spreadsheet_id: "spreadsheet_id", worksheet: "sheet_name" }, value: "{name}" } },
      { key: "match_value", label: "Match value", placeholder: "{text}", required: true },
      { key: "create_if_missing", label: "Create the row when missing", type: "boolean", default: "false" },
      { key: "values", label: "Row values for creation (JSON)", type: "textarea",
        placeholder: '["{trigger.occurred_at|date_format:%Y-%m-%d}", "{text}", "", "NEW"]' },
      { key: "value_input_option", label: "Input option", type: "select", options: ["USER_ENTERED", "RAW"], default: "USER_ENTERED" }
    ]
  },
  {
    type: "sheets_lookup_row",
    label: "Google Sheets (lookup row)",
    icon: SheetsLogo,
    description: "Find worksheet rows whose column equals a value (Lookup Spreadsheet Row). Output: {found, row, values, matches}.",
    fields: [
      { key: "connection_id", label: "Google connection", placeholder: "google", required: true, provider: "google" },
      { key: "spreadsheet_id", label: "Spreadsheet ID", placeholder: "from the sheet URL", required: true,
        discover: { resource: "spreadsheets" } },
      { key: "worksheet", label: "Worksheet", placeholder: "todo", required: true,
        discover: { resource: "worksheets", params: { spreadsheet_id: "spreadsheet_id" }, value: "{name}" } },
      { key: "column", label: "Column", placeholder: "B or Email", required: true,
        discover: { resource: "columns", params: { spreadsheet_id: "spreadsheet_id", worksheet: "worksheet" }, value: "{name}" } },
      { key: "value", label: "Value", placeholder: "{text}", required: true },
      { key: "limit", label: "Max matches", type: "number", default: "1" }
    ]
  },
  {
    type: "sheets_update_row",
    label: "Google Sheets (update row)",
    icon: SheetsLogo,
    description: "Overwrite one worksheet row starting at column A (Update Spreadsheet Row). Pairs with sheets_lookup_row's row output.",
    fields: [
      { key: "connection_id", label: "Google connection", placeholder: "google", required: true, provider: "google" },
      { key: "spreadsheet_id", label: "Spreadsheet ID", placeholder: "from the sheet URL", required: true,
        discover: { resource: "spreadsheets" } },
      { key: "worksheet", label: "Worksheet", placeholder: "todo", required: true,
        discover: { resource: "worksheets", params: { spreadsheet_id: "spreadsheet_id" }, value: "{name}" } },
      { key: "row", label: "Row number", type: "number", required: true, placeholder: "{steps.lookup.output.row}",
        discover: { resource: "rows", params: { spreadsheet_id: "spreadsheet_id", worksheet: "worksheet" }, value: "{row}" } },
      { key: "values", label: "Row values (JSON)", type: "textarea", required: true,
        placeholder: '["{trigger.occurred_at|date_format:%Y-%m-%d}", "{text}", "", "DONE"]' },
      { key: "value_input_option", label: "Input option", type: "select", options: ["USER_ENTERED", "RAW"], default: "USER_ENTERED" }
    ]
  },
  {
    type: "zoom_find_meeting",
    label: "Zoom find meeting",
    icon: Video,
    description: "Find a Zoom meeting by id, or by topic among upcoming meetings",
    fields: [
      { key: "connection_id", label: "Zoom connection", placeholder: "zoom", required: true, provider: "zoom" },
      { key: "meeting_id", label: "Meeting ID",
        discover: { resource: "meetings" } },
      { key: "topic", label: "Topic", placeholder: "used when no meeting id is given" },
      { key: "match", label: "Topic match", type: "select", options: ["contains", "exact"], default: "contains" }
    ]
  },
  {
    type: "zoom_find_recording",
    label: "Zoom: find recording",
    icon: Video,
    description: "Find Zoom cloud recordings by meeting id or topic, or the most recent (Find Recording). Output: {found, recording, recordings, count}.",
    fields: [
      { key: "connection_id", label: "Zoom connection", placeholder: "zoom", required: true, provider: "zoom" },
      { key: "meeting_id", label: "Meeting ID",
        discover: { resource: "recordings" } },
      { key: "topic", label: "Topic", placeholder: "filters the last 30 days when no meeting id is given" },
      { key: "match", label: "Topic match", type: "select", options: ["contains", "exact"], default: "contains" }
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
    type: "paths",
    label: "Paths",
    icon: GitBranch,
    description: "Run the first matching branch's steps, or a default",
    fields: [
      { key: "paths", label: "Paths (YAML)", type: "yaml", required: true,
        placeholder: "- label: invoices\n  when: {subject: {contains: invoice}}\n  actions:\n    - id: notify\n      type: slack\n      channel: \"#alerts\"\n      text: \"{subject}\"" },
      { key: "default", label: "Default steps (YAML)", type: "yaml" }
    ]
  },
  {
    type: "delay",
    label: "Delay",
    icon: Timer,
    description: "Pause the chain before the next step — over 60s the run suspends and the queue resumes it automatically",
    fields: [
      { key: "seconds", label: "Seconds", type: "number", placeholder: "30" },
      { key: "minutes", label: "Minutes", type: "number" },
      { key: "hours", label: "Hours", type: "number" },
      { key: "days", label: "Days", type: "number" },
      { key: "until", label: "Until (ISO datetime)", placeholder: "2026-10-01T09:00:00Z" }
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
    type: "digest",
    label: "Digest",
    icon: Layers,
    description: "Accumulate items across runs, then flush them as one batch (a schedule trigger usually fires the flush). Later steps template {digest.items} and {digest.count}.",
    fields: [
      { key: "mode", label: "Mode", type: "select", options: ["accumulate", "flush"], default: "accumulate" },
      { key: "key", label: "Digest key", placeholder: "nightly-invoices", required: true },
      { key: "item", label: "Item (accumulate)", type: "textarea", placeholder: "{subject}" },
      { key: "items", label: "Items (accumulate, YAML)", type: "yaml", placeholder: "- \"{subject}\"\n- \"{trigger.occurred_at}\"" },
      { key: "shared", label: "Shared across workflows", type: "boolean", default: "false" }
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
  },
  {
    type: "js",
    label: "Code (JavaScript)",
    icon: Braces,
    description: "Sandboxed JavaScript transform (embedded V8): the event data arrives as `input`; `return` a value to make it the step result. console.log is captured.",
    fields: [
      {
        key: "code",
        label: "JavaScript source",
        type: "textarea",
        required: true,
        placeholder: "// event data is `input`; return the result\nreturn {route: input.route, score: (input.items || []).length}"
      },
      { key: "timeout_seconds", label: "Timeout (s)", type: "number" }
    ]
  },
  {
    type: "run_workflow",
    label: "Run workflow",
    icon: Workflow,
    description: "Run another published workflow in-process and expose its step outputs to later templating. Nested sub-runs cap at depth 2 (A→B→C runs, A→B→C→D fails); a workflow calling itself is rejected.",
    fields: [
      { key: "workflow_id", label: "Workflow ID", placeholder: "invoice-notify", required: true },
      { key: "payload", label: "Payload (JSON or template)", type: "textarea", placeholder: "{\"subject\": \"{subject}\"}" },
      { key: "output_field", label: "Output field", placeholder: "notify" }
    ]
  },
  {
    type: "storage_get",
    label: "Storage: get",
    icon: DatabaseZap,
    description: "Read this workflow's persistent key-value state (cross-run memory). A missing key is {found: false}, not an error.",
    fields: [
      { key: "key", label: "Key", placeholder: "last-seen-cursor", required: true }
    ]
  },
  {
    type: "storage_set",
    label: "Storage: set",
    icon: DatabaseZap,
    description: "Write this workflow's persistent key-value state; templates render against the event and earlier steps. Optional TTL cleans the value up.",
    fields: [
      { key: "key", label: "Key", placeholder: "last-seen-cursor", required: true },
      { key: "value", label: "Value", type: "textarea", required: true, placeholder: "{steps.lookup.output.row}" },
      { key: "ttl_seconds", label: "Expire after (seconds)", type: "number" }
    ]
  },
  {
    type: "storage_delete",
    label: "Storage: delete",
    icon: DatabaseZap,
    description: "Remove one key from this workflow's storage; deleting a missing key is fine ({deleted: false}).",
    fields: [
      { key: "key", label: "Key", placeholder: "last-seen-cursor", required: true }
    ]
  },
  {
    type: "storage_find",
    label: "Storage: find",
    icon: DatabaseZap,
    description: "List this workflow's stored keys under a prefix, ascending (default 20, at most 50) — the search half of Zapier Storage.",
    fields: [
      { key: "prefix", label: "Key prefix", placeholder: "seen/", required: true },
      { key: "limit", label: "Max keys", type: "number" }
    ]
  },
  {
    type: "digest_add",
    label: "Digest: add",
    icon: ListTree,
    description: "Collect an item into this workflow's digest so a later (e.g. scheduled) run can release the list together — Zapier's Digest. dedupe: true skips a repeat item; holds up to max_items pending (default 500), dropping the oldest beyond that.",
    fields: [
      { key: "key", label: "Digest key", placeholder: "todo-items", required: true },
      { key: "item", label: "Item", type: "textarea", placeholder: "{text}" },
      { key: "dedupe", label: "Skip if already pending", type: "boolean", default: "false" },
      { key: "max_items", label: "Hold at most (drop oldest beyond)", type: "number" },
      { key: "ttl_seconds", label: "Expire after (seconds)", type: "number" }
    ]
  },
  {
    type: "digest_flush",
    label: "Digest: flush",
    icon: ListTree,
    description: "Release this workflow's collected digest items as one list ({items, count, empty}, arrival order) and empty it. Branch on empty with a filter step when a scheduled run may have nothing to release. reset: false peeks without clearing.",
    fields: [
      { key: "key", label: "Digest key", placeholder: "todo-items", required: true },
      { key: "reset", label: "Clear after reading", type: "boolean", default: "true" }
    ]
  }
];

export const connectorCatalog: ConnectorEntry[] = [
  { name: "email", label: "Email", logo: MailLogo, events: ["message.received"] },
  { name: "youtube", label: "YouTube", logo: YouTubeLogo, events: ["video.published"] },
  { name: "dropbox", label: "Dropbox", logo: DropboxLogo, events: ["file.created", "file.updated", "file.deleted"] },
  { name: "zoom", label: "Zoom", logo: Video, events: ["recording.completed", "recording.transcript_completed", "meeting.started", "meeting.ended"] },
  { name: "slack", label: "Slack", logo: SlackLogo, events: ["message.received"] },
  { name: "renderer", label: "Renderer", logo: FileText, events: ["job.completed"] },
  { name: "schedule", label: "Schedule", logo: Clock, events: ["schedule.triggered"] },
  { name: "poll", label: "Poll", logo: RefreshCw, events: ["item.new"] },
  { name: "custom", label: "Custom", logo: Webhook, events: [] }
];

/** Mirrors registry.FILTER_OPERATORS (engine/matching.py): the operators a
 * trigger's `filters` rules accept — the same evaluator as the logic steps. */
export const filterOperators = [
  "equals", "not_equals", "in", "prefix", "suffix", "contains",
  "does_not_contain", "gt", "gte", "lt", "lte", "exists", "empty"
] as const;

/**
 * Per-step autoretry (Zapier's autoretry, engine/logic.py): connector actions
 * only, never logic steps. A failed action retries `attempts` times (1-3,
 * total tries = attempts + 1) with exponential backoff — `initial_seconds`
 * doubling up to `max_seconds` (both 1-60; defaults 1 and 60), plus jitter —
 * and only then falls through to `on_fail`/`on_error`. The step's output
 * records `attempts` (tries made), so run history shows a step that
 * succeeded on try 3. Opt-in per step; no inspector field yet (it is a
 * mapping, so edit it in the workflow YAML — the registry validates the
 * shape at save time via registry.validate_autoretry_key).
 */

/**
 * Generic per-step error handling (engine/logic.py): a step that fails runs
 * its `on_error` policy — halt aborts the run (the default), continue records
 * the failure and moves on, run also executes `error_actions` as a sub-chain.
 * The failed step's message is templatable as `{steps.<id>.error}`. Legal on
 * every action and logic step, so these live outside the per-entry fields.
 */
export const onErrorField: CatalogField = {
  key: "on_error",
  label: "On error",
  type: "select",
  options: ["halt", "continue", "run"],
  default: "halt"
};

export const errorActionsField: CatalogField = {
  key: "error_actions",
  label: "Error steps (YAML)",
  type: "yaml",
  placeholder: '- id: alert\n  type: slack\n  channel: "#alerts"\n  text: "step failed: {steps.upload.error}"'
};

/**
 * The lighter failure policy (engine/logic.py): `continue` absorbs one step's
 * failure — the step reads `skipped` in run history — while absent or `halt`
 * fails the run. Mutually exclusive with `on_error` on one step.
 */
export const onFailField: CatalogField = {
  key: "on_fail",
  label: "On fail",
  type: "select",
  options: ["continue", "halt"],
  default: "halt"
};

export const errorHandlingFields: CatalogField[] = [onErrorField, onFailField, errorActionsField];
