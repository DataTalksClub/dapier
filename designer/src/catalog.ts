import { Bot, Braces, Calendar, Clock, Code2, DatabaseZap, FileText, Filter, Folder, GitBranch, Globe, Layers, ListTree, Mail, RefreshCw, Rss, Send, Sparkles, Table, Timer, Video, Webhook, Workflow } from "./icons";
import type { ReactNode } from "react";
import { DropboxLogo, MailLogo, S3Logo, SheetsLogo, SlackLogo, TelegramLogo, YouTubeLogo } from "./logos";

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
 * `connectorCatalog` is one trigger row in the step picker (logo + label)
 * and contributes the events its trigger suggests.
 *
 * The step picker browses `actionCatalog` grouped by the job a step is hired
 * for — flow control, AI, developer/data plumbing, and per-app verb lists
 * (see `stepSection` at the bottom of this file).
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
  type?: "text" | "number" | "textarea" | "boolean" | "select" | "yaml" | "json";
  /** Choices for type: "select". */
  options?: string[];
  /** Value assumed when absent; prefills new nodes and YAML round-trips. */
  default?: string;
  /** Nest under this object in the action YAML, e.g. group: "pdf" → action.pdf.page_format. */
  group?: string;
  /** Maintainer documentation for the field's expected shape; the inspector does not render it. */
  help?: string;
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
  /** Family stroke icon or product logo, shown on palette chips and canvas nodes. */
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
    type: "agent",
    label: "Agent",
    icon: Bot,
    description: "Queue a prompt for a Dapier worker — a machine running `dapier worker`, listed on the console's Workers page. The worker runs the prompt as a headless Claude session in its workspace; the step returns as soon as the task is queued. With no worker running, tasks stay queued until one starts. The trigger's stored attachments (an email's files) are staged into the workspace under attachments/ and the run is told where they landed; set Pass trigger attachments to off to skip them.",
    fields: [
      { key: "prompt", label: "Prompt", type: "textarea", required: true, placeholder: "{subject}\n\n{text}" },
      { key: "workspace", label: "Workspace", required: true, placeholder: "/home/alexey/git/dapier" },
      { key: "engine", label: "Engine", placeholder: "claude" },
      { key: "requires", label: "Required capabilities", placeholder: "browser" },
      { key: "tag_prefix", label: "Tag prefix", placeholder: "agent" },
      { key: "attachments", label: "Pass trigger attachments", type: "select", options: ["default", "off"], default: "default" }
    ]
  },
  {
    type: "webhook",
    label: "Webhook",
    icon: Webhook,
    description: "POST the event — or a templated JSON payload — to any URL, optionally HMAC-signed with a stored secret. The receiver's response body lands in the step output (parsed JSON, or a text preview) for later steps.",
    fields: [
      { key: "url", label: "URL", required: true },
      { key: "payload", label: "Payload (JSON, templated)", type: "textarea", placeholder: '{"id": "{trigger.id}"}' },
      { key: "secret_id", label: "Signing secret ID", placeholder: "dapier/webhook" },
      {"key": "payload_format", "label": "Payload format", "type": "select", "options": ["json", "text"], "default": "json"},
      {"key": "content_type", "label": "Content type"},
      {"key": "secret_id_env", "label": "Signing secret ID env var"},
      {"key": "signature_algorithm", "label": "Signature algorithm", "type": "select", "options": ["sha256", "sha1"], "default": "sha256"},
      {"key": "signature_header", "label": "Signature header"},
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
    type: "ai_complete",
    label: "AI: complete",
    icon: Sparkles,
    description: "One chat completion against a configured OpenAI-compatible endpoint (LLM_* env on the Worker function — no connection). The prompt renders from the event; JSON mode parses the reply into `data` (an unparsable reply returns {ok: false, error} instead of failing the step). Output: {ok, text|data, model, usage}.",
    fields: [
      { key: "prompt", label: "Prompt", type: "textarea", required: true,
        placeholder: "Summarize this message for the digest:\n{body}" },
      { key: "system", label: "System message", type: "textarea" },
      { key: "json_mode", label: "JSON mode", type: "boolean", default: "false" },
      { key: "temperature", label: "Temperature", type: "number" },
      { key: "model", label: "Model" },
      { key: "timeout_seconds", label: "Timeout (s)", type: "number" }
    ]
  },
  {
    type: "date_time", label: "Date / time", icon: Clock,
    description: "Format a timestamp, or current processing time when Timestamp is omitted. Output: {iso, formatted, timezone}.",
    fields: [
      { key: "value", label: "Timestamp", placeholder: "{date}", help: "ISO or email Date with offset; omit for current processing time" },
      { key: "timezone", label: "Timezone", placeholder: "America/Chicago", default: "UTC" },
      { key: "format", label: "Format", default: "%Y-%m-%d" }
    ]
  },
  {
    type: "slack",
    label: "Slack",
    icon: SlackLogo,
    description: "Post a templated message to a Slack channel through a stored credential or Slack connection. Output: {ok, channel, ts}.",
    fields: [
      { key: "credential_id", label: "Credential ID" },
      { key: "connection_id", label: "Slack connection", placeholder: "resolves the credential", provider: "slack" },
      { key: "channel", label: "Channel", placeholder: "#alerts", required: true,
        discover: { resource: "channels" } },
      { key: "username", label: "Bot display name", help: "Requires chat:write.customize on modern Slack apps" },
      { key: "link_names", label: "Link names", type: "boolean" },
      { key: "reply_broadcast", label: "Broadcast thread reply", type: "boolean" },
      { key: "text", label: "Text template", type: "textarea", placeholder: "{title}\n{url}" },
      { key: "thread_ts", label: "Thread ts", placeholder: "{ts} — replies into that thread",
        discover: { resource: "messages", params: { channel: "channel" } } },
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
    description: "Look up one workspace user (by email) or channel (by name). Output: {found, user} or {found, channel}. With Create if missing on, a missed channel is created (created: true).",
    fields: [
      { key: "find", label: "Find", type: "select", options: ["user", "channel"], default: "user" },
      { key: "query", label: "Query", placeholder: "person@example.com or #channel", required: true },
      { key: "create_if_missing", label: "Create if missing", type: "boolean", default: "false",
        placeholder: "channels only: create the channel when none matches" },
      { key: "is_private", label: "Private channel", type: "boolean", default: "false",
        placeholder: "only used when Create if missing is on" },
      { key: "connection_id", label: "Slack connection", placeholder: "resolves the credential", provider: "slack" },
      { key: "credential_id", label: "Credential ID" }
    ]
  },
  {
    type: "slack_update_message",
    label: "Slack: update message",
    icon: SlackLogo,
    description: "Edit one already-posted message (chat.update). A slack trigger envelope carries {channel_id} and {ts}. Output: {ok, channel, ts}.",
    fields: [
      { key: "connection_id", label: "Slack connection", placeholder: "resolves the credential", provider: "slack" },
      { key: "channel", label: "Channel", required: true, placeholder: "{channel_id}", discover: { resource: "channels" } },
      { key: "ts", label: "Message ts", required: true, placeholder: "{ts}",
        discover: { resource: "messages", params: { channel: "channel" } } },
      { key: "text", label: "New text", type: "textarea", required: true, placeholder: "{text} (updated by the workflow)" },
      { key: "credential_id", label: "Credential ID" }
    ]
  },
  {
    type: "slack_add_reaction",
    label: "Slack: add reaction",
    icon: SlackLogo,
    description: "React to one message (reactions.add). already_reacted counts as success so a retried run stays green. Output: {ok, reaction, channel, ts}.",
    fields: [
      { key: "connection_id", label: "Slack connection", placeholder: "resolves the credential", provider: "slack" },
      { key: "channel", label: "Channel", required: true, placeholder: "{channel_id}", discover: { resource: "channels" } },
      { key: "timestamp", label: "Message ts", required: true, placeholder: "{ts}",
        discover: { resource: "messages", params: { channel: "channel" } } },
      { key: "reaction", label: "Reaction", required: true, placeholder: "tada" },
      { key: "credential_id", label: "Credential ID" }
    ]
  },
  {
    type: "slack_schedule_message",
    label: "Slack: send scheduled message",
    icon: SlackLogo,
    description: "Sends one templated message for later (chat.scheduleMessage). Post at takes an ISO 8601 datetime — a missing offset reads as UTC — or epoch seconds. Output: {ok, channel, scheduled_message_id, ts, post_at}.",
    fields: [
      { key: "connection_id", label: "Slack connection", placeholder: "resolves the credential", provider: "slack" },
      { key: "channel", label: "Channel", required: true, placeholder: "{channel_id} or #channel", discover: { resource: "channels" } },
      { key: "text", label: "Text template", type: "textarea", required: true, placeholder: "{title}\n{url}" },
      { key: "post_at", label: "Post at", required: true, placeholder: "2026-10-02T09:00:00Z" },
      { key: "thread_ts", label: "Thread ts", placeholder: "{ts} — schedules the reply into one thread",
        discover: { resource: "messages", params: { channel: "channel" } } },
      { key: "unfurl_links", label: "Unfurl links", type: "boolean", default: "true" },
      { key: "unfurl_media", label: "Unfurl media", type: "boolean", default: "true" },
      { key: "credential_id", label: "Credential ID" }
    ]
  },
  {
    type: "slack_add_reminder",
    label: "Slack: add reminder",
    icon: SlackLogo,
    description: "Sets one reminder (reminders.add). Time takes Slack's natural-language times — in 20 minutes, tomorrow 9am — or epoch seconds; empty = Slack's default (20 minutes). Output: {ok, reminder: {id, time, text}}.",
    fields: [
      { key: "connection_id", label: "Slack connection", placeholder: "resolves the credential", provider: "slack" },
      { key: "text", label: "Reminder text", type: "textarea", required: true, placeholder: "Rotate the {customer} API key" },
      { key: "time", label: "When", placeholder: "in 20 minutes / tomorrow 9am / 1759400000" },
      { key: "credential_id", label: "Credential ID" }
    ]
  },
  {
    type: "slack_dm",
    label: "Slack: send direct message",
    icon: SlackLogo,
    description: "Open (or reuse) the DM channel with one user and post the templated text into it (conversations.open + chat.postMessage). Chain find user by email to target the person an event names. Output: {ok, user, channel, ts}.",
    fields: [
      { key: "connection_id", label: "Slack connection", placeholder: "resolves the credential", provider: "slack" },
      { key: "user_id", label: "User ID", required: true, placeholder: "{steps.find.output.user.id}", discover: { resource: "users", value: "{id}" } },
      { key: "text", label: "Text template", type: "textarea", placeholder: "{title}\n{url}" },
      { key: "unfurl_links", label: "Unfurl links", type: "boolean", default: "true" },
      { key: "unfurl_media", label: "Unfurl media", type: "boolean", default: "true" },
      { key: "credential_id", label: "Credential ID" }
    ]
  },
  {
    type: "slack_create_channel",
    label: "Slack: create channel",
    icon: SlackLogo,
    description: "Create one channel (conversations.create); the name is normalized to what Slack accepts (lowercase, spaces to hyphens, illegal characters dropped). An existing name is an error. Output: {ok, channel: {id, name, is_private}}.",
    fields: [
      { key: "connection_id", label: "Slack connection", placeholder: "resolves the credential", provider: "slack" },
      { key: "name", label: "Channel name", required: true, placeholder: "alerts-{customer}" },
      { key: "is_private", label: "Private channel", type: "boolean", default: "false" },
      { key: "credential_id", label: "Credential ID" }
    ]
  },
  {
    type: "slack_set_topic",
    label: "Slack: set channel topic",
    icon: SlackLogo,
    description: "Set one channel's topic (conversations.setTopic). A slack trigger envelope carries {channel_id}. Output: {ok, channel, topic}.",
    fields: [
      { key: "connection_id", label: "Slack connection", placeholder: "resolves the credential", provider: "slack" },
      { key: "channel", label: "Channel", required: true, placeholder: "{channel_id}", discover: { resource: "channels" } },
      { key: "topic", label: "Topic", type: "textarea", required: true },
      { key: "credential_id", label: "Credential ID" }
    ]
  },
  {
    type: "slack_set_purpose",
    label: "Slack: set channel purpose",
    icon: SlackLogo,
    description: "Set one channel's purpose (conversations.setPurpose). A slack trigger envelope carries {channel_id}. Output: {ok, channel, purpose}.",
    fields: [
      { key: "connection_id", label: "Slack connection", placeholder: "resolves the credential", provider: "slack" },
      { key: "channel", label: "Channel", required: true, placeholder: "{channel_id}", discover: { resource: "channels" } },
      { key: "purpose", label: "Purpose", type: "textarea", required: true },
      { key: "credential_id", label: "Credential ID" }
    ]
  },
  {
    type: "slack_invite_to_channel",
    label: "Slack: invite users to channel",
    icon: SlackLogo,
    description: "Invite one or more workspace users into a channel (conversations.invite). Slack's already_in_channel is absorbed as invited: false so a retried run stays green. Output: {invited, channel, users}.",
    fields: [
      { key: "connection_id", label: "Slack connection", placeholder: "resolves the credential", provider: "slack" },
      { key: "channel", label: "Channel", required: true, placeholder: "{channel_id} or #channel", discover: { resource: "channels" } },
      { key: "users", label: "Users", required: true, placeholder: "{steps.find.output.user.id} (comma-separated ids)",
        discover: { resource: "users" } },
      { key: "credential_id", label: "Credential ID" }
    ]
  },
  {
    type: "slack_pin_message",
    label: "Slack: pin message",
    icon: SlackLogo,
    description: "Pin one message to its channel (pins.add). A slack trigger envelope carries {channel_id} and {ts}. Output: {pinned, channel, timestamp}.",
    fields: [
      { key: "connection_id", label: "Slack connection", placeholder: "resolves the credential", provider: "slack" },
      { key: "channel", label: "Channel", required: true, placeholder: "{channel_id}", discover: { resource: "channels" } },
      { key: "timestamp", label: "Message ts", required: true, placeholder: "{ts}",
        discover: { resource: "messages", params: { channel: "channel" } } },
      { key: "credential_id", label: "Credential ID" }
    ]
  },
  {
    type: "slack_find_message",
    label: "Slack: find message",
    icon: SlackLogo,
    description: "Search workspace messages (search.messages; the token needs the search:read scope). Output: {found, messages: [{ts, channel_id, channel_name, user, text, permalink}], count} — a miss is not an error.",
    fields: [
      { key: "connection_id", label: "Slack connection", placeholder: "resolves the credential", provider: "slack" },
      { key: "query", label: "Query", required: true, placeholder: "deploy postmortem" },
      { key: "count", label: "Max results", type: "number", placeholder: "20" },
      { key: "credential_id", label: "Credential ID" }
    ]
  },
  {
    type: "slack_upload_file",
    label: "Slack: upload file",
    icon: SlackLogo,
    description: "Send one file to a channel (files.uploadV2). Content comes from exactly one of source_url, a staged source_s3 {bucket, key}, or inline content — dropbox/drive read file chain here. Output: {ok, channel, file: {id, name, title, permalink}}.",
    fields: [
      { key: "connection_id", label: "Slack connection", placeholder: "resolves the credential", provider: "slack" },
      { key: "channel", label: "Channel", required: true, placeholder: "{channel_id} or C01ABC2DEF", discover: { resource: "channels" } },
      { key: "filename", label: "File name", required: true, placeholder: "report.pdf" },
      { key: "source_url", label: "Source URL", placeholder: "https://www.googleapis.com/drive/v3/files/{id}?alt=media" },
      { key: "content", label: "Content", placeholder: "inline text — templated, e.g. {trigger.text}" },
      { key: "title", label: "Title", placeholder: "shown in Slack (defaults to the file name)" },
      { key: "initial_comment", label: "Comment", type: "textarea", placeholder: "posted with the file" },
      { key: "thread_ts", label: "Thread ts", placeholder: "{ts} — replies to that message instead of posting top-level",
        discover: { resource: "messages", params: { channel: "channel" } } },
      { key: "content_type", label: "Content type", placeholder: "guessed from the file name" },
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
    type: "telegram_send_document",
    label: "Telegram send document",
    icon: Send,
    description: "Send a file to a chat (Bot API sendDocument). Media comes from exactly one of source_url or a staged source_s3 {bucket, key} object; the filename defaults to the URL's or key's file name.",
    fields: [
      { key: "connection_id", label: "Bot connection", required: true, provider: "telegram" },
      { key: "chat_id", label: "Chat ID", placeholder: "defaults to the triggering chat",
        discover: { resource: "chats" } },
      { key: "source_url", label: "Media URL", placeholder: "https://example.test/report.pdf" },
      { key: "filename", label: "Filename override" },
      { key: "attachment_selection", label: "Attachment selection", type: "select", options: ["all", "single", "first"], default: "all" },
      { key: "overwrite", label: "Overwrite existing file", type: "boolean", default: "false" },
      { key: "strict_conflict", label: "Reject identical file conflicts", type: "boolean", default: "false" },
      { key: "autorename", label: "Autorename on conflict", type: "boolean", default: "true" },
      { key: "caption", label: "Caption", type: "textarea", placeholder: "New mail: {subject}" },
      { key: "timeout_seconds", label: "Timeout (s)", type: "number" }
    ]
  },
  {
    type: "telegram_send_photo",
    label: "Telegram send photo",
    icon: Send,
    description: "Send a photo to a chat (Bot API sendPhoto). Media comes from exactly one of source_url or a staged source_s3 {bucket, key} object; Bot API photos must be JPEG/PNG/GIF/WEBP under 10 MB.",
    fields: [
      { key: "connection_id", label: "Bot connection", required: true, provider: "telegram" },
      { key: "chat_id", label: "Chat ID", placeholder: "defaults to the triggering chat",
        discover: { resource: "chats" } },
      { key: "source_url", label: "Media URL", placeholder: "https://example.test/report.png" },
      { key: "filename", label: "Filename override" },
      { key: "caption", label: "Caption", type: "textarea", placeholder: "New mail: {subject}" },
      { key: "timeout_seconds", label: "Timeout (s)", type: "number" }
    ]
  },
  {
    type: "telegram_send_poll",
    label: "Telegram send poll",
    icon: Send,
    description: "Send a poll to a chat (Bot API sendPoll). Options holds one option per line — 2 to 10 after trimming empty lines; chat_id falls back to the triggering chat like the other sends. Output: {message_id, chat_id, poll: {id, question}}.",
    fields: [
      { key: "connection_id", label: "Bot connection", required: true, provider: "telegram" },
      { key: "chat_id", label: "Chat ID", placeholder: "defaults to the triggering chat",
        discover: { resource: "chats" } },
      { key: "question", label: "Question", required: true, placeholder: "Ship on Friday?" },
      { key: "options", label: "Options", type: "textarea", required: true, placeholder: "Yes\nNo\nNeeds another week" },
      { key: "anonymous", label: "Anonymous voting", type: "boolean", default: "true" },
      { key: "timeout_seconds", label: "Timeout (s)", type: "number" }
    ]
  },
  {
    type: "telegram_edit_message",
    label: "Telegram edit message",
    icon: Send,
    description: "Edit one already-posted message's text (Bot API editMessageText). chat_id falls back to the triggering chat like the other telegram actions; message_id comes from the trigger or an earlier step. Output: {message_id, chat_id}.",
    fields: [
      { key: "connection_id", label: "Bot connection", required: true, provider: "telegram" },
      { key: "chat_id", label: "Chat ID", placeholder: "defaults to the triggering chat",
        discover: { resource: "chats" } },
      { key: "message_id", label: "Message ID", required: true, placeholder: "{message_id}" },
      { key: "text", label: "New text", type: "textarea", required: true, placeholder: "{text} (edited by the workflow)" },
      { key: "timeout_seconds", label: "Timeout (s)", type: "number" }
    ]
  },
  {
    type: "telegram_pin_message",
    label: "Telegram pin message",
    icon: Send,
    description: "Pin one message in a chat (Bot API pinChatMessage). chat_id falls back to the triggering chat; disable_notification pins silently. Output: {pinned, chat_id, message_id}.",
    fields: [
      { key: "connection_id", label: "Bot connection", required: true, provider: "telegram" },
      { key: "chat_id", label: "Chat ID", placeholder: "defaults to the triggering chat",
        discover: { resource: "chats" } },
      { key: "message_id", label: "Message ID", required: true, placeholder: "{message_id}" },
      { key: "disable_notification", label: "Pin silently", type: "boolean", default: "false" },
      { key: "timeout_seconds", label: "Timeout (s)", type: "number" }
    ]
  },
  {
    type: "telegram_ban_member",
    label: "Telegram ban member",
    icon: Send,
    description: "Ban one member from a chat (Bot API banChatMember). chat_id falls back to the triggering chat; user_id renders from the event. Optional until_date bans until an epoch timestamp (empty means forever). Output: {banned, chat_id, user_id}.",
    fields: [
      { key: "connection_id", label: "Bot connection", required: true, provider: "telegram" },
      { key: "chat_id", label: "Chat ID", placeholder: "defaults to the triggering chat",
        discover: { resource: "chats" } },
      { key: "user_id", label: "User ID", required: true, placeholder: "{user_id}" },
      { key: "until_date", label: "Banned until", placeholder: "1798761600 (epoch seconds; empty = forever)" }
    ]
  },
  {
    type: "telegram_unban_member",
    label: "Telegram unban member",
    icon: Send,
    description: "Unban one member of a chat (Bot API unbanChatMember). chat_id falls back to the triggering chat; user_id renders from the event. Output: {unbanned, chat_id, user_id}.",
    fields: [
      { key: "connection_id", label: "Bot connection", required: true, provider: "telegram" },
      { key: "chat_id", label: "Chat ID", placeholder: "defaults to the triggering chat",
        discover: { resource: "chats" } },
      { key: "user_id", label: "User ID", required: true, placeholder: "{user_id}" }
    ]
  },
  {
    type: "email_send",
    label: "Send email",
    icon: Mail,
    description: "Send an email through SES. The sender defaults to the workflow's sender; the body is the text body, the HTML body, or both. Any attachment — or a threading header (In-Reply-To / References) — switches the send to raw MIME.",
    fields: [
      { key: "to", label: "To", required: true, placeholder: "you@example.com or {sender}" },
      { key: "subject", label: "Subject", placeholder: "{subject}" },
      { key: "text", label: "Text body", type: "textarea" },
      { key: "html", label: "HTML body", type: "textarea" },
      { key: "sender", label: "Sender", placeholder: "defaults to the workflow sender" },
      { key: "cc", label: "Cc", placeholder: "comma-separated or {templated}" },
      { key: "bcc", label: "Bcc", placeholder: "comma-separated or {templated}" },
      { key: "reply_to", label: "Reply-To", placeholder: "replies@example.com" },
      { key: "in_reply_to", label: "In-Reply-To", placeholder: "{trigger.message_id}" },
      { key: "references", label: "References", placeholder: "{trigger.message_id} — the thread's chain" },
      { key: "attachments", label: "Attachments (YAML)", type: "textarea",
        placeholder: '- filename: report.pdf\n  source_url: "{link}"' }
    ]
  },
  {
    type: "gmail_send",
    label: "Gmail: send email",
    icon: Mail,
    description: "Send an email from the connection's Gmail mailbox (users.messages.send). Gmail delivers only from the authenticated account, so there is no sender field. The body is the text body, the HTML body, or both.",
    fields: [
      { key: "connection_id", label: "Connection ID", placeholder: "google", required: true },
      { key: "to", label: "To", required: true, placeholder: "you@example.com or {sender}" },
      { key: "subject", label: "Subject", placeholder: "{subject}" },
      { key: "text", label: "Text body", type: "textarea" },
      { key: "html", label: "HTML body", type: "textarea" },
      { key: "cc", label: "Cc", placeholder: "comma-separated or {templated}" },
      { key: "bcc", label: "Bcc", placeholder: "comma-separated or {templated}" }
    ]
  },
  {
    type: "dataops",
    label: "DataOps intake",
    icon: DatabaseZap,
    description: "Push the event into a DataOps intake (url_env or url): attachments and rendered PDFs — or a Dropbox file event's bytes — are staged with sha256 checksums and referenced by s3:// URI.",
    fields: [
      { key: "auth_secret_id", label: "Auth secret ID", placeholder: "dapier/dataops", required: true },
      { key: "url_env", label: "URL env var", placeholder: "DATAOPS_INTAKE_URL" },
      { key: "url", label: "URL (overrides env)" },
      { key: "connection_id", label: "Dropbox connection", placeholder: "dropbox — for file-event intakes", provider: "dropbox" },
      { key: "path", label: "Dropbox file path", placeholder: "{steps.move.output.item.path}" },
      { key: "recipient_route", label: "Dropbox intake route", placeholder: "invoice", help: "Use a route configured in DataOps; defaults to invoice for Dropbox file events." },
      { key: "filename", label: "Filename override" },
      { key: "timeout_seconds", label: "Timeout (s)", type: "number" }
    ]
  },
  {
    type: "dropbox_upload",
    label: "Dropbox upload",
    icon: DropboxLogo,
    description: "Upload the email's stored attachments — or a rendered output file — into a folder in the connection's Dropbox. Output: {uploaded: [paths], already_exists}.",
    fields: [
      { key: "connection_id", label: "Dropbox connection", placeholder: "dropbox", required: true, provider: "dropbox" },
      { key: "source", label: "Source", type: "select", options: ["attachment", "output"], default: "attachment" },
      { key: "folder", label: "Folder", placeholder: "/Invoices", required: true,
        discover: { resource: "folders", value: "{path}" } },
      { key: "filename", label: "Filename override" },
      { key: "overwrite", label: "Overwrite existing file", type: "boolean", default: "false" },
      { key: "strict_conflict", label: "Reject identical file conflicts", type: "boolean", default: "false" },
      { key: "autorename", label: "Autorename on conflict", type: "boolean", default: "true" },
      { key: "skip_existing", label: "Skip when the path already exists", type: "boolean", default: "false" }
    ]
  },
  {
    type: "dropbox_delete",
    label: "Dropbox delete",
    icon: DropboxLogo,
    description: "Delete one file from the connection's Dropbox, defaulting to the triggering event's path — run it after intake succeeds. Output: {deleted: path}.",
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
    type: "dropbox_read_file",
    label: "Dropbox: read file",
    icon: DropboxLogo,
    description: "Download a file from the connection's Dropbox and stage it for later steps. Pair with Amazon S3 (source_s3) to move the bytes into a bucket.",
    fields: [
      { key: "connection_id", label: "Dropbox connection", placeholder: "dropbox", required: true, provider: "dropbox" },
      { key: "path", label: "Path", placeholder: "defaults to the event's file path",
        discover: { resource: "files", value: "{path}" } }
    ]
  },
  {
    type: "dropbox_get_temp_link",
    label: "Dropbox: get temporary link",
    icon: DropboxLogo,
    description: "Mint a direct download link for one file (valid a few hours). Chain it before Amazon S3 and pass {steps.<id>.output.link} as source_url to pull Dropbox bytes into the pipeline.",
    fields: [
      { key: "connection_id", label: "Dropbox connection", placeholder: "dropbox", required: true, provider: "dropbox" },
      { key: "path", label: "Path", placeholder: "defaults to the event's file path",
        discover: { resource: "files", value: "{path}" } }
    ]
  },
  {
    type: "dropbox_create_folder",
    label: "Dropbox: create folder",
    icon: DropboxLogo,
    description: "Create one folder (files/create_folder_v2). The path is the full destination and renders from the event; an existing folder is an error. Output: {folder, item}.",
    fields: [
      { key: "connection_id", label: "Dropbox connection", placeholder: "dropbox", required: true, provider: "dropbox" },
      { key: "path", label: "Folder path", placeholder: "/Invoices/{month}", required: true }
    ]
  },
  {
    type: "dropbox_move",
    label: "Dropbox: move file",
    icon: DropboxLogo,
    description: "Move (or rename) one file or folder (files/move_v2) — a move within the same folder under a new name renames. autorename appends a suffix instead of erroring on an existing destination. Output: {moved, item}.",
    fields: [
      { key: "connection_id", label: "Dropbox connection", placeholder: "dropbox", required: true, provider: "dropbox" },
      { key: "from_path", label: "From path", required: true, placeholder: "{path} from a dropbox trigger",
        discover: { resource: "files", value: "{path}" } },
      { key: "to_path", label: "To path", required: true, placeholder: "/Archive/{filename}" },
      { key: "autorename", label: "Autorename on conflict", type: "boolean", default: "false" }
    ]
  },
  {
    type: "dropbox_copy",
    label: "Dropbox: copy file",
    icon: DropboxLogo,
    description: "Copy one file or folder to a new path (files/copy_v2). autorename appends a suffix instead of erroring on an existing destination. Output: {copied, item}.",
    fields: [
      { key: "connection_id", label: "Dropbox connection", placeholder: "dropbox", required: true, provider: "dropbox" },
      { key: "from_path", label: "From path", required: true, placeholder: "{path} from a dropbox trigger",
        discover: { resource: "files", value: "{path}" } },
      { key: "to_path", label: "To path", required: true, placeholder: "/Archive/{filename}" },
      { key: "autorename", label: "Autorename on conflict", type: "boolean", default: "false" }
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
      { key: "content_type", label: "Content type", placeholder: "defaults to the trigger's mimeType" },
      { key: "key_mode", label: "Object key mode", type: "select", options: ["safe", "exact"], default: "safe" },
      { key: "omit_content_type", label: "Omit Content-Type", type: "boolean", default: "false" },
      { key: "source_s3", label: "Stored source (bucket/key)", type: "json" }
    ]
  },
  {
    type: "s3_find",
    label: "S3: find object",
    icon: S3Logo,
    description: "Find the first object matching a name pattern in a bucket (Find Object). A truncated listing rides out in next_token — feed it back on a repeated run to continue.",
    fields: [
      { key: "credential_id", label: "Credential ID", placeholder: "aws (default)" },
      { key: "bucket", label: "Bucket", placeholder: "datatalks-mailchimp-backup", required: true,
        discover: { resource: "buckets", account: "aws" } },
      { key: "pattern", label: "Name pattern", placeholder: "{name}.pdf", required: true },
      { key: "prefix", label: "Key prefix", placeholder: "reports/2026/",
        discover: { resource: "objects", params: { bucket: "bucket", prefix: "prefix" } } },
      { key: "match", label: "Match", type: "select", options: ["exact", "prefix", "suffix", "contains"], default: "exact" },
      { key: "next_token", label: "Next token", placeholder: "{previous.next_token} — continues a truncated listing" }
    ]
  },
  {
    type: "s3_list_objects",
    label: "S3: list objects",
    icon: S3Logo,
    description: "List a bucket's objects under a prefix (ListObjectsV2; List Files). Output: {bucket, prefix, items, count, truncated, next_token} — each item is {key, size, last_modified}; keys arrive alphabetically, capped at Max items (default 20, up to 100). Feed next_token back in to keep walking a truncated listing.",
    fields: [
      { key: "credential_id", label: "Credential ID", placeholder: "aws (default)" },
      { key: "bucket", label: "Bucket", placeholder: "datatalks-mailchimp-backup", required: true,
        discover: { resource: "buckets", account: "aws" } },
      { key: "prefix", label: "Key prefix", placeholder: "reports/2026/",
        discover: { resource: "objects", params: { bucket: "bucket", prefix: "prefix" } } },
      { key: "max_items", label: "Max items", type: "number", default: "20",
        placeholder: "listing bound — capped at 100" },
      { key: "next_token", label: "Continuation token", placeholder: "{previous.next_token} — walks past the cap" }
    ]
  },
  {
    type: "s3_head_object",
    label: "S3: object metadata",
    icon: S3Logo,
    description: "Read Content-Type, size, modification time and ETag with HEAD; does not download object content.",
    fields: [
      { key: "credential_id", label: "Credential ID", placeholder: "aws (default)" },
      { key: "bucket", label: "Bucket", required: true, discover: { resource: "buckets", account: "aws" } },
      { key: "key", label: "Object key", required: true, discover: { resource: "objects", params: { bucket: "bucket" } } }
    ]
  },
  {
    type: "s3_read_object",
    label: "S3: read object",
    icon: S3Logo,
    description: "Download one object and stage the bytes for the steps that follow (GetObject). The key defaults to the event's object key. Output: {filename, size, content_type, bucket, key, source_bucket, source_key} — pair with a step whose source_s3 takes templates to move the bytes elsewhere.",
    fields: [
      { key: "credential_id", label: "Credential ID", placeholder: "aws (default)" },
      { key: "bucket", label: "Bucket", placeholder: "datatalks-mailchimp-backup", required: true,
        discover: { resource: "buckets", account: "aws" } },
      { key: "key", label: "Object key", placeholder: "defaults to the event's object key",
        discover: { resource: "objects", params: { bucket: "bucket" } } }
    ]
  },
  {
    type: "s3_presign_url",
    label: "S3: presigned URL",
    icon: S3Logo,
    description: "Mint a short-lived presigned download URL for one object (presigned GET; one hour by default, up to seven days). Output: {link, bucket, key, expires_in} — feed link into a follow-up step's source_url to hand a private object to another pipeline. Computed, never sent.",
    fields: [
      { key: "credential_id", label: "Credential ID", placeholder: "aws (default)" },
      { key: "bucket", label: "Bucket", placeholder: "datatalks-mailchimp-backup", required: true,
        discover: { resource: "buckets", account: "aws" } },
      { key: "key", label: "Object key", placeholder: "reports/2026/report.pdf", required: true,
        discover: { resource: "objects", params: { bucket: "bucket" } } },
      { key: "expires_in", label: "Expires in (s)", type: "number", default: "3600",
        placeholder: "seconds — one hour by default, capped at 604800 (SigV4's seven days)" }
    ]
  },
  {
    type: "s3_delete_object",
    label: "S3: delete object",
    icon: S3Logo,
    description: "Delete one object from a bucket (DeleteObject). S3 deletes are idempotent — removing an absent key succeeds. Output: {deleted, bucket, key}.",
    fields: [
      { key: "credential_id", label: "Credential ID", placeholder: "aws (default)" },
      { key: "bucket", label: "Bucket", placeholder: "datatalks-mailchimp-backup", required: true,
        discover: { resource: "buckets", account: "aws" } },
      { key: "key", label: "Object key", placeholder: "tmp/{name}.pdf", required: true,
        discover: { resource: "objects", params: { bucket: "bucket" } } }
    ]
  },
  {
    type: "mailchimp_find_member",
    label: "Mailchimp: find member",
    icon: MailLogo,
    description: "Find one audience member by email. Output: {found, member}; a miss is {found: false, member: null} — or, with Create if missing on, the member is created and the output reports created: true.",
    fields: [
      { key: "credential_id", label: "Credential ID", placeholder: "mailchimp (default)" },
      { key: "list_id", label: "Audience", required: true,
        discover: { resource: "audiences", account: "mailchimp", value: "{id}" } },
      { key: "email", label: "Email", placeholder: "{sender}", required: true,
        discover: { resource: "members", params: { list_id: "list_id" }, account: "mailchimp", value: "{email}" } },
      { key: "create_if_missing", label: "Create if missing", type: "boolean", default: "false" },
      { key: "status", label: "Status if new", type: "select",
        options: ["subscribed", "pending", "unsubscribed", "cleaned"], default: "subscribed",
        placeholder: "only used when Create if missing is on" },
      { key: "merge_fields", label: "Merge fields (JSON)",
        placeholder: '{"FNAME": "{name}"} — only used when Create if missing is on' }
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
    type: "mailchimp_remove_member",
    label: "Mailchimp: remove member",
    icon: MailLogo,
    description: "Permanently remove one audience member by email. A missing email is not an error: the output is {removed: false} — guard with find member when the difference matters.",
    fields: [
      { key: "credential_id", label: "Credential ID", placeholder: "mailchimp (default)" },
      { key: "list_id", label: "Audience", required: true,
        discover: { resource: "audiences", account: "mailchimp", value: "{id}" } },
      { key: "email", label: "Email", placeholder: "{sender}", required: true,
        discover: { resource: "members", params: { list_id: "list_id" }, account: "mailchimp", value: "{email}" } }
    ]
  },
  {
    type: "mailchimp_unsubscribe_member",
    label: "Mailchimp: unsubscribe member",
    icon: MailLogo,
    description: "Unsubscribe one audience member by email — reversible: the member stays on the audience with status unsubscribed, unlike the permanent remove. A missing email is not an error: the output is {unsubscribed: false}.",
    fields: [
      { key: "credential_id", label: "Credential ID", placeholder: "mailchimp (default)" },
      { key: "list_id", label: "Audience", required: true,
        discover: { resource: "audiences", account: "mailchimp", value: "{id}" } },
      { key: "email", label: "Email", placeholder: "{sender}", required: true,
        discover: { resource: "members", params: { list_id: "list_id" }, account: "mailchimp", value: "{email}" } }
    ]
  },
  {
    type: "mailchimp_tag_member",
    label: "Mailchimp: tag member",
    icon: MailLogo,
    description: "Add or remove one tag on an audience member: Add applies the tag, Remove sets it inactive. The member must exist — upsert it first when unsure.",
    fields: [
      { key: "credential_id", label: "Credential ID", placeholder: "mailchimp (default)" },
      { key: "list_id", label: "Audience", required: true,
        discover: { resource: "audiences", account: "mailchimp", value: "{id}" } },
      { key: "email", label: "Email", placeholder: "{sender}", required: true,
        discover: { resource: "members", params: { list_id: "list_id" }, account: "mailchimp", value: "{email}" } },
      { key: "tag", label: "Tag", placeholder: "digest-readers", required: true },
      { key: "tag_action", label: "Operation", type: "select", options: ["add", "remove"], default: "add" }
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
    type: "drive_read_file",
    label: "Drive: read file",
    icon: FileText,
    description: "Download a Drive file and stage it for later steps (Read File); Google-native docs export first (export_as). Pair with Amazon S3 (source_s3) or Slack's upload file to move the bytes on.",
    fields: [
      { key: "connection_id", label: "Google connection", placeholder: "google", required: true, provider: "google" },
      { key: "file_id", label: "File ID", required: true,
        discover: { resource: "files" } },
      { key: "export_as", label: "Export as", placeholder: "application/pdf — for Google-native files" }
    ]
  },
  {
    type: "drive_upload_file",
    label: "Upload file",
    icon: FileText,
    description: "Upload one file into the connection's Drive (Upload File). Content comes from exactly one of source_url, a staged source_s3 {bucket, key}, or inline content. Output: {file_id, name, mime_type, size, webViewLink}.",
    fields: [
      { key: "connection_id", label: "Google connection", placeholder: "google", required: true, provider: "google" },
      { key: "name", label: "File name", placeholder: "report.pdf", required: true },
      { key: "source_url", label: "Source URL", placeholder: "https://www.googleapis.com/drive/v3/files/{id}?alt=media" },
      { key: "content", label: "Content", placeholder: "inline text — templated, e.g. {trigger.text}" },
      { key: "folder_id", label: "Folder ID", placeholder: "uploads into one folder (defaults to the root)",
        discover: { resource: "folders" } },
      { key: "content_type", label: "Content type", placeholder: "application/octet-stream" }
    ]
  },
  {
    type: "drive_copy_file",
    label: "Drive: copy file",
    icon: FileText,
    description: "Copy one Drive file (Drive v3 files.copy); the optional name names the copy, else Drive's \"Copy of …\". Output: {file_id, name, mime_type, size, webViewLink} — the upload's keys, so the file steps chain.",
    fields: [
      { key: "connection_id", label: "Google connection", placeholder: "google", required: true, provider: "google" },
      { key: "file_id", label: "File ID", required: true,
        discover: { resource: "files" } },
      { key: "name", label: "Copy name", placeholder: "defaults to Drive's \"Copy of <original name>\"" }
    ]
  },
  {
    type: "drive_share_file",
    label: "Drive: share file",
    icon: FileText,
    description: "Share one Drive file by creating a permission (Drive v3 permissions.create). Role is reader/commenter/writer; Share with picks user, group, domain, or anyone — a user or group grant needs the grantee's email address. Output: {shared, file_id, permission_id, role, type}.",
    fields: [
      { key: "connection_id", label: "Google connection", placeholder: "google", required: true, provider: "google" },
      { key: "file_id", label: "File ID", required: true,
        discover: { resource: "files" } },
      { key: "role", label: "Role", type: "select", options: ["reader", "commenter", "writer"], default: "reader" },
      { key: "share_type", label: "Share with", type: "select", options: ["user", "group", "domain", "anyone"], default: "user" },
      { key: "email_address", label: "Email address", placeholder: "the user or group to grant — required for those share types" }
    ]
  },
  {
    type: "drive_delete_file",
    label: "Drive: delete file",
    icon: FileText,
    description: "Delete one Drive file (Zapier's Delete File). The default moves the file to the trash (Drive v3 files.update with trashed: true) — recoverable from Drive's trash; Delete permanently calls files.delete instead, which destroys the file outright. Output: {trashed, permanent, file_id, name}.",
    fields: [
      { key: "connection_id", label: "Google connection", placeholder: "google", required: true, provider: "google" },
      { key: "file_id", label: "File ID", required: true,
        discover: { resource: "files" } },
      { key: "permanent", label: "Delete permanently", type: "boolean", default: "false",
        placeholder: "off: trash (recoverable) — on: files.delete destroys it" }
    ]
  },
  {
    type: "drive_move_file",
    label: "Drive: move file",
    icon: FileText,
    description: "Move one Drive file between folders (Drive v3 files.update with addParents/removeParents; Zapier's Move File). At least one of Add to folder / Remove from folder is required; adding keeps the file's other parents. Output: {moved: true, file_id, name, parents} — the resulting parents.",
    fields: [
      { key: "connection_id", label: "Google connection", placeholder: "google", required: true, provider: "google" },
      { key: "file_id", label: "File ID", required: true,
        discover: { resource: "files" } },
      { key: "add_parent", label: "Add to folder",
        discover: { resource: "folders" } },
      { key: "remove_parent", label: "Remove from folder",
        discover: { resource: "folders" } }
    ]
  },
  {
    type: "drive_create_folder",
    label: "Drive: create folder",
    icon: FileText,
    description: "Create one Drive folder (Drive v3 files.create with the folder mimeType). The optional parent folder places it, else it lands at the Drive root. Output: {folder_id, name, url} — file uploads into it via Upload file's folder_id.",
    fields: [
      { key: "connection_id", label: "Google connection", placeholder: "google", required: true, provider: "google" },
      { key: "name", label: "Folder name", placeholder: "Invoices 2026", required: true },
      { key: "parent_folder_id", label: "Parent folder ID", placeholder: "creates inside one folder (defaults to the Drive root)",
        discover: { resource: "folders" } }
    ]
  },
  {
    type: "youtube_subscription_status",
    label: "YouTube: subscription status",
    icon: YouTubeLogo,
    description: "Read authenticated WebSub lease state; signing credentials stay on the server.",
    fields: [{ key: "channel_id", label: "Channel ID", required: true }]
  },
  {
    type: "youtube_subscription_renew",
    label: "YouTube: renew subscription",
    icon: YouTubeLogo,
    description: "Renew an already watched channel with the deployed callback and secret.",
    fields: [{ key: "channel_id", label: "Channel ID", required: true }]
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
    type: "youtube_upload_video",
    label: "Upload video",
    icon: YouTubeLogo,
    description: "Upload one video to the connection's YouTube channel (Upload Video). Content comes from exactly one of source_url, a staged source_s3 {bucket, key}, or inline content; bytes stage in memory, so keep sources modest (~100 MB ceiling). Output: {video_id, title, privacy_status, upload_status, url}. Needs the youtube.upload OAuth scope.",
    fields: [
      { key: "connection_id", label: "YouTube connection", placeholder: "youtube", required: true, provider: "youtube" },
      { key: "title", label: "Title", placeholder: "Deploying dapier: a walkthrough", required: true },
      { key: "source_url", label: "Source URL", placeholder: "https://example.test/talk.mp4" },
      { key: "content", label: "Content", placeholder: "inline text — templated, e.g. {trigger.text}" },
      { key: "description", label: "Description", placeholder: "shown under the video — templated" },
      { key: "tags", label: "Tags", placeholder: "comma-separated, e.g. devops, kubernetes" },
      { key: "category_id", label: "Category ID", placeholder: "YouTube category id, e.g. 22 (People & Blogs)" },
      { key: "privacy_status", label: "Privacy", type: "select", options: ["public", "unlisted", "private"], default: "unlisted" }
    ]
  },
  {
    type: "youtube_add_to_playlist",
    label: "YouTube: add to playlist",
    icon: YouTubeLogo,
    description: "Add one video to a playlist the connection can edit (playlistItems.insert; Add Video to Playlist). Output: {playlist_id, video_id, playlist_item_id, title, position}. A video already in the playlist is YouTube's videoAlreadyInPlaylist error, not a silent duplicate.",
    fields: [
      { key: "connection_id", label: "YouTube connection", placeholder: "youtube", required: true, provider: "youtube" },
      { key: "playlist_id", label: "Playlist ID", required: true,
        discover: { resource: "playlists" } },
      { key: "video_id", label: "Video ID", required: true,
        placeholder: "{trigger.video_id} from a youtube trigger, or a found video's id",
        discover: { resource: "videos" } }
    ]
  },
  {
    type: "youtube_update_video",
    label: "YouTube: update video",
    icon: YouTubeLogo,
    description: "Update one video's title and description (videos.update, part=snippet; Update Video). YouTube replaces the whole snippet on update, so pass category_id when the video's category matters. Output: {updated, video_id, title, description, category_id}.",
    fields: [
      { key: "connection_id", label: "YouTube connection", placeholder: "youtube", required: true, provider: "youtube" },
      { key: "video_id", label: "Video ID", required: true,
        placeholder: "{trigger.video_id} from a youtube trigger",
        discover: { resource: "videos" } },
      { key: "title", label: "Title", required: true,
        placeholder: "the video's new title — YouTube replaces the whole snippet part" },
      { key: "description", label: "Description",
        placeholder: "takes templates — left out, YouTube clears it" },
      { key: "category_id", label: "Category ID",
        placeholder: "e.g. 22 (People & Blogs) — pass it when the video's category matters, or the update clears it" }
    ]
  },
  {
    type: "youtube_remove_from_playlist",
    label: "YouTube: remove from playlist",
    icon: YouTubeLogo,
    description: "Remove one item from a playlist the connection can edit (playlistItems.delete; Remove Video from Playlist). An item already gone is {removed: false}, not an error. Output: {removed, playlist_item_id}. Needs the youtube.force-ssl scope.",
    fields: [
      { key: "connection_id", label: "YouTube connection", placeholder: "youtube", required: true, provider: "youtube" },
      { key: "playlist_item_id", label: "Playlist item ID", required: true,
        placeholder: "{steps.add.output.playlist_item_id} — youtube_add_to_playlist's output, or an item id from a Find Playlist Videos listing" }
    ]
  },
  {
    type: "youtube_create_playlist",
    label: "YouTube: create playlist",
    icon: YouTubeLogo,
    description: "Create an empty playlist on the connection's channel (playlists.insert, part=snippet,status; Create Playlist). Output: {playlist_id, title, url} — the id chains into Add to Playlist.",
    fields: [
      { key: "connection_id", label: "YouTube connection", placeholder: "youtube", required: true, provider: "youtube" },
      { key: "title", label: "Title", placeholder: "Deploying dapier: full episodes", required: true },
      { key: "description", label: "Description", placeholder: "shown on the playlist page — templated" },
      { key: "privacy_status", label: "Privacy", type: "select", options: ["private", "public", "unlisted"], default: "private" }
    ]
  },
  {
    type: "calendar_create_event",
    label: "Google Calendar",
    icon: Calendar,
    description: "Create an event in a calendar (Create Detailed Event)",
    fields: [
      { key: "connection_id", label: "Connection ID", placeholder: "google-calendar", required: true, provider: "google" },
      { key: "calendar_id", label: "Calendar ID", placeholder: "primary", required: true,
        discover: { resource: "calendars" } },
      { key: "summary", label: "Title", placeholder: "Interview with {name}", required: true },
      { key: "start", label: "Starts", placeholder: "2026-10-01T09:00:00 or 2026-10-01", required: true,
        help: "ISO datetime, or a bare YYYY-MM-DD for an all-day event" },
      { key: "end", label: "Ends", placeholder: "2026-10-01T10:00:00 or 2026-10-01", required: true,
        help: "Same shape as Starts — dates with dates, datetimes with datetimes" },
      { key: "timezone", label: "Time zone", placeholder: "Europe/Berlin",
        help: "IANA name for the datetimes; omitted means floating time" },
      { key: "description", label: "Description", type: "textarea", placeholder: "Notes, links, agendas" },
      { key: "location", label: "Location", placeholder: "Room 4 / https://meet.test/x" },
      { key: "attendees", label: "Attendees (JSON)", type: "textarea",
        placeholder: '["a@example.test", "b@example.test"]',
        help: "JSON array of emails (or {email: …} objects), or a comma-separated list" }
    ]
  },
  {
    type: "calendar_quick_add",
    label: "Google Calendar (quick add)",
    icon: Calendar,
    description: "Create an event from one line of text (Quick Add Event)",
    fields: [
      { key: "connection_id", label: "Connection ID", placeholder: "google-calendar", required: true, provider: "google" },
      { key: "calendar_id", label: "Calendar ID", placeholder: "primary", required: true,
        discover: { resource: "calendars" } },
      { key: "text", label: "Event text", placeholder: "Reviewer call tomorrow 10am", required: true }
    ]
  },
  {
    type: "calendar_find_events",
    label: "Google Calendar (find event)",
    icon: Calendar,
    description: "Find events matching a text query, optionally create the first one when nothing matches (Find or Create Event). Output: {found, created, count, event, events}.",
    fields: [
      { key: "connection_id", label: "Connection ID", placeholder: "google-calendar", required: true, provider: "google" },
      { key: "calendar_id", label: "Calendar ID", placeholder: "primary", required: true,
        discover: { resource: "calendars" } },
      { key: "query", label: "Search text", placeholder: "{name}",
        help: "Free-text match across event fields; empty lists the window" },
      { key: "time_min", label: "From", placeholder: "2026-09-01T00:00:00Z", help: "Default: yesterday" },
      { key: "time_max", label: "Until", placeholder: "2026-12-31T23:59:59Z", help: "Default: the end of the next quarter" },
      { key: "create_if_missing", label: "Create if missing", type: "boolean", default: "false",
        help: "Post the event from the fields below when nothing matches" },
      { key: "summary", label: "Create title", placeholder: "Sync with {name}", help: "Only used when Create if missing is on" },
      { key: "start", label: "Create starts", placeholder: "2026-10-01T09:00:00", help: "Only used when Create if missing is on" },
      { key: "end", label: "Create ends", placeholder: "2026-10-01T10:00:00", help: "Only used when Create if missing is on" },
      { key: "timezone", label: "Time zone", placeholder: "Europe/Berlin" },
      { key: "description", label: "Create description", type: "textarea" },
      { key: "location", label: "Create location" },
      { key: "attendees", label: "Create attendees (JSON)", type: "textarea", placeholder: '["a@example.test"]' }
    ]
  },
  {
    type: "calendar_update_event",
    label: "Google Calendar (update event)",
    icon: Calendar,
    description: "Patch the provided fields of one event (Update Event)",
    fields: [
      { key: "connection_id", label: "Connection ID", placeholder: "google-calendar", required: true, provider: "google" },
      { key: "calendar_id", label: "Calendar ID", placeholder: "primary", required: true,
        discover: { resource: "calendars" } },
      { key: "event_id", label: "Event ID", placeholder: "{steps.find.event.event_id}", required: true,
        help: "From find's output, the trigger payload's id, or the event's link tail" },
      { key: "summary", label: "Title" },
      { key: "start", label: "Starts", placeholder: "2026-10-02T09:00:00" },
      { key: "end", label: "Ends", placeholder: "2026-10-02T10:00:00" },
      { key: "timezone", label: "Time zone", placeholder: "Europe/Berlin" },
      { key: "description", label: "Description", type: "textarea" },
      { key: "location", label: "Location" },
      { key: "attendees", label: "Attendees (JSON)", type: "textarea",
        help: "Replaces the attendee list; an empty list clears it only when sent as []" }
    ]
  },
  {
    type: "calendar_delete_event",
    label: "Google Calendar (delete event)",
    icon: Calendar,
    description: "Delete one event from a calendar (Delete Event)",
    fields: [
      { key: "connection_id", label: "Connection ID", placeholder: "google-calendar", required: true, provider: "google" },
      { key: "calendar_id", label: "Calendar ID", placeholder: "primary", required: true,
        discover: { resource: "calendars" } },
      { key: "event_id", label: "Event ID", placeholder: "{steps.find.event.event_id}", required: true }
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
      { key: "sheet_id", label: "Worksheet ID", type: "number", help: "Numeric gid from the sheet URL; overrides Worksheet name" },
      { key: "sheet_name", label: "Worksheet", placeholder: "todo (default Sheet1)",
        discover: { resource: "worksheets", params: { spreadsheet_id: "spreadsheet_id" }, value: "{name}" } },
      { key: "values", label: "Row values (JSON)", type: "json", required: true,
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
    type: "sheets_delete_row",
    label: "Google Sheets (delete row)",
    icon: SheetsLogo,
    description: "Delete one worksheet row, shifting the rows under it up (Delete Spreadsheet Row via batchUpdate). Pairs with sheets_lookup_row's row output.",
    fields: [
      { key: "connection_id", label: "Google connection", placeholder: "google", required: true, provider: "google" },
      { key: "spreadsheet_id", label: "Spreadsheet ID", placeholder: "from the sheet URL", required: true,
        discover: { resource: "spreadsheets" } },
      { key: "worksheet", label: "Worksheet", placeholder: "todo", required: true,
        discover: { resource: "worksheets", params: { spreadsheet_id: "spreadsheet_id" }, value: "{name}" } },
      { key: "row", label: "Row number", type: "number", required: true, placeholder: "{steps.lookup.output.row}",
        discover: { resource: "rows", params: { spreadsheet_id: "spreadsheet_id", worksheet: "worksheet" }, value: "{row}" } }
    ]
  },
  {
    type: "sheets_clear_values",
    label: "Google Sheets (clear values)",
    icon: SheetsLogo,
    description: "Clear a worksheet or A1 range (Clear Spreadsheet Values via values:clear) — cell contents go, formatting and the rows themselves stay. Output: {cleared_range, spreadsheet_id}.",
    fields: [
      { key: "connection_id", label: "Google connection", placeholder: "google", required: true, provider: "google" },
      { key: "spreadsheet_id", label: "Spreadsheet ID", placeholder: "from the sheet URL", required: true,
        discover: { resource: "spreadsheets" } },
      { key: "worksheet", label: "Worksheet", placeholder: "todo (default Sheet1)",
        discover: { resource: "worksheets", params: { spreadsheet_id: "spreadsheet_id" }, value: "{name}" } },
      { key: "range", label: "Range (A1)", placeholder: "todo!A2:Z100 — a bare range is qualified with the worksheet name" }
    ]
  },
  {
    type: "sheets_create_spreadsheet",
    label: "Google Sheets (create spreadsheet)",
    icon: SheetsLogo,
    description: "Create an empty spreadsheet (Create Spreadsheet). Output: {spreadsheet_id, url, worksheet}, plus headers_applied when a header row was written — reference {steps.<id>.output.spreadsheet_id} in a follow-up append step.",
    fields: [
      { key: "connection_id", label: "Google connection", placeholder: "google", required: true, provider: "google" },
      { key: "title", label: "Spreadsheet title", placeholder: "Weekly sync {date}", required: true },
      { key: "headers", label: "Header row (JSON)", type: "textarea",
        placeholder: '["Date", "Task", "Status"]' }
    ]
  },
  {
    type: "sheets_add_worksheet",
    label: "Google Sheets (add worksheet)",
    icon: SheetsLogo,
    description: "Add one worksheet to a spreadsheet (Create Worksheet via batchUpdate addSheet). Output: {sheet_id, title, row_count, column_count, spreadsheet_id} — the title chains into the values actions' Worksheet fields. A tab with the same title is Sheets' 400.",
    fields: [
      { key: "connection_id", label: "Google connection", placeholder: "google", required: true, provider: "google" },
      { key: "spreadsheet_id", label: "Spreadsheet ID", placeholder: "from the sheet URL", required: true,
        discover: { resource: "spreadsheets" } },
      { key: "title", label: "Worksheet title", placeholder: "Archive {date}", required: true },
      { key: "row_count", label: "Rows", type: "number", default: "1000" },
      { key: "column_count", label: "Columns", type: "number", default: "26" }
    ]
  },
  {
    type: "sheets_create_column",
    label: "Google Sheets (create column)",
    icon: SheetsLogo,
    description: "Append one header cell to the worksheet's header row (Create Spreadsheet Column) — the first free column, or the existing one when the name is already there. Output: {spreadsheet_id, worksheet, column, position, cell, created}.",
    fields: [
      { key: "connection_id", label: "Google connection", placeholder: "google", required: true, provider: "google" },
      { key: "spreadsheet_id", label: "Spreadsheet ID", placeholder: "from the sheet URL", required: true,
        discover: { resource: "spreadsheets" } },
      { key: "worksheet", label: "Worksheet", placeholder: "todo (default Sheet1)",
        discover: { resource: "worksheets", params: { spreadsheet_id: "spreadsheet_id" }, value: "{name}" } },
      { key: "column", label: "Column name (header)", placeholder: "Status", required: true,
        discover: { resource: "columns", params: { spreadsheet_id: "spreadsheet_id", worksheet: "worksheet" }, value: "{name}" } }
    ]
  },
  {
    type: "zoom_find_meeting",
    label: "Zoom find meeting",
    icon: Video,
    description: "Find a Zoom meeting by id, or by topic in the scope window (upcoming by default, past with scope: past). With create-if-missing, an upcoming topic miss creates the meeting from the shared create fields (Zapier's Find or Create).",
    fields: [
      { key: "connection_id", label: "Zoom connection", placeholder: "zoom", required: true, provider: "zoom" },
      { key: "meeting_id", label: "Meeting ID",
        discover: { resource: "meetings" } },
      { key: "topic", label: "Topic", placeholder: "used when no meeting id is given" },
      { key: "match", label: "Topic match", type: "select", options: ["contains", "exact"], default: "contains" },
      { key: "scope", label: "Scope", type: "select", options: ["upcoming", "past"], default: "upcoming" },
      { key: "create_if_missing", label: "Create on topic miss", type: "boolean", default: "false" },
      { key: "start_time", label: "Start time (for create)", placeholder: "{trigger.start} — ISO 8601" },
      { key: "duration", label: "Duration (min, for create)", type: "number" },
      { key: "timezone", label: "Timezone (for create)", placeholder: "Europe/Berlin" },
      { key: "agenda", label: "Agenda (for create)" },
      { key: "settings", label: "Settings (JSON, for create)", type: "textarea",
        placeholder: '{"waiting_room": false}' }
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
    type: "zoom_delete_recording",
    label: "Zoom: delete recording",
    icon: Video,
    description: "Delete one meeting's cloud recording (DELETE /meetings/{id}/recordings; Delete Recording). Action picks how: trash (the default) is recoverable from Zoom's trash, permanent destroys the recording and its files. Output: {deleted: true, meeting_id, action}.",
    fields: [
      { key: "connection_id", label: "Zoom connection", placeholder: "zoom", required: true, provider: "zoom" },
      { key: "meeting_id", label: "Meeting ID", required: true,
        discover: { resource: "recordings" } },
      { key: "action", label: "Action", type: "select", options: ["trash", "permanent"], default: "trash" }
    ]
  },
  {
    type: "zoom_create_meeting",
    label: "Zoom: create meeting",
    icon: Video,
    description: "Create a Zoom meeting — scheduled when a start time is given, instant otherwise (Create Meeting). Output: {created, scheduled, meeting: {id, topic, join_url, start_url, passcode, ...}}.",
    fields: [
      { key: "connection_id", label: "Zoom connection", placeholder: "zoom", required: true, provider: "zoom" },
      { key: "topic", label: "Topic", required: true },
      { key: "start_time", label: "Start time", placeholder: "2026-10-01T09:00:00Z — instant meeting when empty" },
      { key: "duration", label: "Duration (minutes)", type: "number", default: "60" },
      { key: "timezone", label: "Time zone", placeholder: "Europe/Berlin" },
      { key: "agenda", label: "Agenda" },
      { key: "settings", label: "Settings (JSON)", type: "textarea",
        placeholder: '{{"join_before_host": true}} — {tokens} expand, literal braces double' }
    ]
  },
  {
    type: "zoom_update_meeting",
    label: "Zoom: update meeting",
    icon: Video,
    description: "Update one Zoom meeting's schedule or metadata — only the fields set are sent, the rest of the meeting stays untouched (PATCH /meetings/{id}). Output: {updated, meeting_id, updated_fields}.",
    fields: [
      { key: "connection_id", label: "Zoom connection", placeholder: "zoom", required: true, provider: "zoom" },
      { key: "meeting_id", label: "Meeting ID", required: true,
        discover: { resource: "meetings" } },
      { key: "topic", label: "Topic", placeholder: "the meeting's new title; left out, Zoom keeps the old one" },
      { key: "start_time", label: "Start time", placeholder: "2026-10-01T09:00:00Z — the reschedule field" },
      { key: "duration", label: "Duration (minutes)", type: "number" },
      { key: "timezone", label: "Time zone", placeholder: "Europe/Berlin" },
      { key: "agenda", label: "Agenda" },
      { key: "settings", label: "Settings (JSON)", type: "textarea",
        placeholder: '{{"join_before_host": true}} — {tokens} expand, literal braces double' }
    ]
  },
  {
    type: "zoom_add_registrant",
    label: "Zoom: add registrant",
    icon: Video,
    description: "Register one person for a meeting that requires registration and get their personalized join link (POST /meetings/{id}/registrants). Output: {registered, meeting_id, registrant_id, join_url} — join_url is unique per registrant, the thing an invite email templates.",
    fields: [
      { key: "connection_id", label: "Zoom connection", placeholder: "zoom", required: true, provider: "zoom" },
      { key: "meeting_id", label: "Meeting ID", required: true,
        discover: { resource: "meetings" } },
      { key: "email", label: "Email", required: true, placeholder: "{trigger.email}" },
      { key: "first_name", label: "First name" },
      { key: "last_name", label: "Last name" }
    ]
  },
  {
    type: "zoom_list_past_participants",
    label: "Zoom: list past meeting participants",
    icon: Video,
    description: "List who attended one past Zoom meeting (GET /past_meetings/{id}/participants) — name, email and join/leave times per attendee, up to 300 across three pages. Output: {participants, count, meeting_id}; pair with the meeting.ended trigger.",
    fields: [
      { key: "connection_id", label: "Zoom connection", placeholder: "zoom", required: true, provider: "zoom" },
      { key: "meeting_id", label: "Meeting ID", required: true,
        discover: { resource: "past_meetings" } }
    ]
  },
  {
    type: "zoom_delete_meeting",
    label: "Zoom: delete meeting",
    icon: Video,
    description: "Delete one Zoom meeting (DELETE /meetings/{id}). A recurring meeting's whole series goes away unless occurrence_id scopes the delete to one occurrence. Output: {deleted, meeting_id}.",
    fields: [
      { key: "connection_id", label: "Zoom connection", placeholder: "zoom", required: true, provider: "zoom" },
      { key: "meeting_id", label: "Meeting ID", required: true,
        discover: { resource: "past_meetings" } },
      { key: "occurrence_id", label: "Occurrence ID",
        placeholder: "recurring meetings only: deletes just this occurrence — left out, the whole series is deleted" }
    ]
  },
  {
    type: "zoom_create_webinar",
    label: "Zoom: create webinar",
    icon: Video,
    description: "Create a Zoom webinar — scheduled when a start time is given, recurring with no fixed time otherwise (Create Webinar). Output: {created, scheduled, webinar: {id, topic, join_url, start_url, passcode, ...}}.",
    fields: [
      { key: "connection_id", label: "Zoom connection", placeholder: "zoom", required: true, provider: "zoom" },
      { key: "topic", label: "Topic", required: true },
      { key: "start_time", label: "Start time", placeholder: "2026-10-01T09:00:00Z — recurring with no fixed time when empty" },
      { key: "duration", label: "Duration (minutes)", type: "number", default: "60" },
      { key: "timezone", label: "Time zone", placeholder: "Europe/Berlin" },
      { key: "agenda", label: "Agenda" },
      { key: "settings", label: "Settings (JSON)", type: "textarea",
        placeholder: '{{"approval_type": 2}} — {tokens} expand, literal braces double' }
    ]
  },
  {
    type: "zoom_update_webinar",
    label: "Zoom: update webinar",
    icon: Video,
    description: "Update one Zoom webinar's schedule or metadata — only the fields set are sent, the rest of the webinar stays untouched (PATCH /webinars/{id}). Output: {updated, webinar_id, updated_fields}.",
    fields: [
      { key: "connection_id", label: "Zoom connection", placeholder: "zoom", required: true, provider: "zoom" },
      { key: "webinar_id", label: "Webinar ID", required: true,
        discover: { resource: "webinars" } },
      { key: "topic", label: "Topic", placeholder: "the webinar's new title; left out, Zoom keeps the old one" },
      { key: "start_time", label: "Start time", placeholder: "2026-10-01T09:00:00Z — the reschedule field" },
      { key: "duration", label: "Duration (minutes)", type: "number" },
      { key: "timezone", label: "Time zone", placeholder: "Europe/Berlin" },
      { key: "agenda", label: "Agenda" },
      { key: "settings", label: "Settings (JSON)", type: "textarea",
        placeholder: '{{"approval_type": 2}} — {tokens} expand, literal braces double' }
    ]
  },
  {
    type: "zoom_find_webinar",
    label: "Zoom: find webinar",
    icon: Video,
    description: "Find a Zoom webinar by id, or by topic in the scope window (upcoming by default, past with scope: past). Output: {found, webinar: {id, topic, start_time, join_url, duration}} — a miss is found: false, not an error.",
    fields: [
      { key: "connection_id", label: "Zoom connection", placeholder: "zoom", required: true, provider: "zoom" },
      { key: "webinar_id", label: "Webinar ID",
        discover: { resource: "webinars" } },
      { key: "topic", label: "Topic", placeholder: "used when no webinar id is given" },
      { key: "match", label: "Topic match", type: "select", options: ["contains", "exact"], default: "contains" },
      { key: "scope", label: "Scope", type: "select", options: ["upcoming", "past"], default: "upcoming" }
    ]
  },
  {
    type: "zoom_add_webinar_registrant",
    label: "Zoom: add webinar registrant",
    icon: Video,
    description: "Register one person for a webinar that requires registration and get their personalized join link (POST /webinars/{id}/registrants). Output: {registered, webinar_id, registrant_id, join_url} — join_url is unique per registrant, the thing an invite email templates.",
    fields: [
      { key: "connection_id", label: "Zoom connection", placeholder: "zoom", required: true, provider: "zoom" },
      { key: "webinar_id", label: "Webinar ID", required: true,
        discover: { resource: "webinars" } },
      { key: "email", label: "Email", required: true, placeholder: "{trigger.email}" },
      { key: "first_name", label: "First name" },
      { key: "last_name", label: "Last name" }
    ]
  },
  {
    type: "zoom_delete_webinar",
    label: "Zoom: delete webinar",
    icon: Video,
    description: "Delete one Zoom webinar (DELETE /webinars/{id}). A recurring webinar's whole series goes away unless occurrence_id scopes the delete to one occurrence. Output: {deleted, webinar_id}.",
    fields: [
      { key: "connection_id", label: "Zoom connection", placeholder: "zoom", required: true, provider: "zoom" },
      { key: "webinar_id", label: "Webinar ID", required: true,
        discover: { resource: "webinars" } },
      { key: "occurrence_id", label: "Occurrence ID",
        placeholder: "recurring webinars only: deletes just this occurrence — left out, the whole series is deleted" }
    ]
  },
  {
    type: "zoom_list_past_webinar_participants",
    label: "Zoom: list past webinar participants",
    icon: Video,
    description: "List who attended one past Zoom webinar (GET /past_webinars/{id}/participants) — name, email and join/leave times per attendee, up to 300 across three pages. Output: {participants, count, webinar_id}; pair with the webinar.ended trigger.",
    fields: [
      { key: "connection_id", label: "Zoom connection", placeholder: "zoom", required: true, provider: "zoom" },
      { key: "webinar_id", label: "Webinar ID", required: true,
        discover: { resource: "webinars" } }
    ]
  },
  {
    type: "render_html_to_pdf",
    label: "Render PDF",
    icon: FileText,
    description: "Queue an html-renderer job: the input field's stored email body — or input_value, a template (e.g. a code step's transformed body) that wins when set — becomes a PDF at output_key in output_bucket (env fallback RENDER_ARTIFACTS_BUCKET). Output: {job_id, output}.",
    fields: [
      { key: "input_field", label: "Input field", placeholder: "html" },
      { key: "input_value", label: "Input value (template)", placeholder: "{steps.clean.output.result.html}" },
      { key: "output_key", label: "Output key", placeholder: "rendered/{event_id}.pdf" },
      { key: "output_bucket", label: "Output bucket", discover: { resource: "buckets", account: "aws" } },
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
  },
  {
    type: "csv_parse",
    label: "CSV: parse",
    icon: Table,
    description: "Parse CSV text into rows for the steps that follow. Content comes from exactly one of content (inline CSV text) or source_s3 {bucket, key} (a staged object — an email attachment, a render output, s3_read_object). Output: {headers, rows, count} — rows are dicts keyed by the header row (header_row on, the default) or plain lists; values stay strings.",
    fields: [
      { key: "content", label: "CSV content", type: "textarea",
        placeholder: "name,amount\n{subject},{amount}" },
      { key: "source_s3", label: "Staged file (S3)",
        placeholder: "{bucket: …, key: …}" },
      { key: "delimiter", label: "Delimiter", placeholder: ", (default)" },
      { key: "header_row", label: "First row is headers", type: "boolean", default: "true" }
    ]
  },
  {
    type: "csv_format",
    label: "CSV: format",
    icon: Table,
    description: "Serialize rows into CSV text (save this output with an s3_upload's content, an email body, …). Rows is a template-rendered JSON array of dicts or of arrays; headers sets the column order (default: the first dict row's keys, insertion order). Output: {csv, count}.",
    fields: [
      { key: "rows", label: "Rows", type: "textarea", required: true,
        placeholder: "[{\"name\": \"{subject}\", \"amount\": \"{amount}\"}]" },
      { key: "headers", label: "Headers", type: "textarea",
        placeholder: "[\"name\", \"amount\"]" },
      { key: "delimiter", label: "Delimiter", placeholder: ", (default)" }
    ]
  }
];

export const connectorCatalog: ConnectorEntry[] = [
  { name: "ai", label: "AI", logo: Sparkles, events: [] },
  { name: "email", label: "Email", logo: MailLogo, events: ["message.received", "bounce.received", "complaint.received"] },
  { name: "youtube", label: "YouTube", logo: YouTubeLogo, events: ["video.published"] },
  { name: "dropbox", label: "Dropbox", logo: DropboxLogo, events: ["file.created", "file.updated", "file.deleted"] },
  { name: "zoom", label: "Zoom", logo: Video, events: ["recording.completed", "recording.transcript_completed", "meeting.started", "meeting.ended", "meeting.registration_created", "webinar.started", "webinar.ended", "webinar.registration_created"] },
  { name: "slack", label: "Slack", logo: SlackLogo, events: ["message.received", "app.mention", "reaction.added", "member.joined"] },
  { name: "telegram", label: "Telegram", logo: TelegramLogo, events: ["message.received", "channel_post.received", "callback_query.received"] },
  { name: "mailchimp", label: "Mailchimp", logo: MailLogo, events: ["subscribe", "unsubscribe", "profile", "upemail", "cleaned", "campaign", "member.new"] },
  { name: "google-sheets", label: "Google Sheets", logo: SheetsLogo, events: ["row.new", "row.updated"] },
  { name: "google-drive", label: "Google Drive", logo: Folder, events: ["file.created", "file.updated", "file.deleted"] },
  { name: "google-calendar", label: "Google Calendar", logo: Calendar, events: ["event.new"] },
  { name: "gmail", label: "Gmail", logo: Mail, events: ["message.received"] },
  { name: "s3", label: "S3", logo: S3Logo, events: ["file.created", "file.updated", "file.deleted"] },
  { name: "rss", label: "RSS", logo: Rss, events: ["item.new"] },
  { name: "renderer", label: "Renderer", logo: FileText, events: ["job.completed"] },
  { name: "schedule", label: "Schedule", logo: Clock, events: ["schedule.triggered"] },
  { name: "poll", label: "Poll", logo: RefreshCw, events: ["item.new"] },
  { name: "custom", label: "Custom", logo: Webhook, events: [] }
];

/** Mirrors each registered Connector's event_info (connectors.registry): the
 * label and one-line description the inspector's Event dropdown shows,
 * keyed "<connector>/<event>". tests/test_designer_catalog.py keeps it in
 * sync with the registry — change descriptions there first. */
export const connectorEventInfo: Record<string, [label: string, description: string]> = {
  "dropbox/file.created": ["File added", "A new file appears in the watched Dropbox folder"],
  "dropbox/file.updated": ["File changed", "A file in the watched Dropbox folder is modified"],
  "dropbox/file.deleted": ["File deleted", "A file is removed from the watched Dropbox folder"],
  "email/message.received": ["Email arrives", "A message reaches one of this workflow's Dapier addresses"],
  "email/bounce.received": ["Email bounced", "A message Dapier sent could not be delivered"],
  "email/complaint.received": ["Spam complaint", "A recipient marked a message Dapier sent as spam"],
  "gmail/message.received": ["Email arrives", "A new message lands in the watched Gmail inbox or label"],
  "google-calendar/event.new": ["Event created", "A new event is added to the watched calendar"],
  "google-drive/file.created": ["File created", "A new file appears in the watched Drive folder"],
  "google-drive/file.updated": ["File changed", "A file in the watched Drive folder is modified"],
  "google-drive/file.deleted": ["File deleted", "A file is removed from the watched Drive folder"],
  "google-sheets/row.new": ["Row added", "A new row appears in the watched worksheet"],
  "google-sheets/row.updated": ["Row changed", "An existing row in the watched worksheet is edited"],
  "mailchimp/subscribe": ["Subscribed", "Someone joins the audience"],
  "mailchimp/unsubscribe": ["Unsubscribed", "Someone leaves the audience"],
  "mailchimp/profile": ["Profile updated", "A subscriber changes their profile fields"],
  "mailchimp/upemail": ["Email changed", "A subscriber changes their email address"],
  "mailchimp/cleaned": ["Address cleaned", "Mailchimp removes an address that keeps bouncing"],
  "mailchimp/campaign": ["Campaign sent", "A campaign is sent to the audience"],
  "mailchimp/member.new": ["New member (poll)", "A polled audience lists a member not seen before"],
  "poll/item.new": ["New item", "A polled API returns an item not seen before"],
  "renderer/job.completed": ["Render finished", "A renderer job finished and its output is ready"],
  "rss/item.new": ["New feed item", "The feed publishes an entry not seen before"],
  "s3/file.created": ["Object added", "A new object lands in the watched S3 bucket or prefix"],
  "s3/file.updated": ["Object changed", "An object in the watched bucket is overwritten"],
  "s3/file.deleted": ["Object deleted", "An object is removed from the watched bucket"],
  "schedule/schedule.triggered": ["On schedule", "The workflow's cron or rate schedule fires"],
  "slack/message.received": ["Message posted", "A message is posted in a channel the app can see"],
  "slack/app.mention": ["App mentioned", "Someone @-mentions the app"],
  "slack/reaction.added": ["Reaction added", "Someone adds an emoji reaction to a message"],
  "slack/member.joined": ["Member joined", "Someone joins a channel"],
  "telegram/message.received": ["Message received", "Someone sends the bot a message, directly or in a group"],
  "telegram/channel_post.received": ["Channel post received", "A post appears in a channel the bot administers"],
  "telegram/callback_query.received": ["Button pressed (callback query)", "Someone taps an inline button on a bot message"],
  "youtube/video.published": ["Video published", "A new video goes live on the channel"],
  "zoom/recording.completed": ["Recording ready", "A cloud recording finishes processing"],
  "zoom/recording.transcript_completed": ["Transcript ready", "A cloud recording's transcript is ready"],
  "zoom/meeting.started": ["Meeting started", "A meeting begins"],
  "zoom/meeting.ended": ["Meeting ended", "A meeting ends"],
  "zoom/meeting.registration_created": ["Meeting registration", "Someone registers for a meeting"],
  "zoom/webinar.started": ["Webinar started", "A webinar begins"],
  "zoom/webinar.ended": ["Webinar ended", "A webinar ends"],
  "zoom/webinar.registration_created": ["Webinar registration", "Someone registers for a webinar"],
};

/** Label and description for one connector event, or null when the
 * catalog has none (custom events). */
export function eventInfo(connector: string, event: string): { label: string; description: string } | null {
  const hit = connectorEventInfo[`${connector}/${event}`];
  return hit ? { label: hit[0], description: hit[1] } : null;
}

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

/**
 * Step-picker grouping — the palette organized by the job a step is hired
 * for, not one flat list:
 *   - "flow" — shape the run itself: branch, filter, loop, wait, digest;
 *   - "ai"   — delegate thinking: LLM completions and worker agents;
 *   - "data" — developer plumbing: HTTP, code, CSV, storage, rendering;
 *   - "app"  — everything else is a product verb; the app comes from the
 *     action type's prefix (slack_* → the Slack connector), reusing its
 *     label and logo. A new prefixed action lands in its app automatically;
 *     a new app-agnostic action must be added to `stepCategories` below —
 *     unlisted ones fall back to "data" so they never vanish from the picker.
 */
export type StepSection = "flow" | "ai" | "data" | "app";

const stepCategories: Record<string, Exclude<StepSection, "app">> = {
  agent: "ai",
  ai_complete: "ai",
  filter: "flow",
  condition: "flow",
  paths: "flow",
  delay: "flow",
  for_each: "flow",
  digest: "flow",
  digest_add: "flow",
  digest_flush: "flow",
  run_workflow: "flow",
  code: "data",
  js: "data",
  csv_parse: "data",
  csv_format: "data",
  storage_get: "data",
  storage_set: "data",
  storage_delete: "data",
  storage_find: "data",
  http_request: "data",
  webhook: "data",
  render_html_to_pdf: "data",
  dataops: "data"
};

/** Action type prefix → connector name; drives the picker's per-app groups. */
const appPrefixes: Array<[RegExp, string]> = [
  [/^slack(_|$)/, "slack"],
  [/^telegram(_|$)/, "telegram"],
  [/^gmail(_|$)/, "gmail"],
  [/^email(_|$)/, "email"],
  [/^dropbox(_|$)/, "dropbox"],
  [/^s3(_|$)/, "s3"],
  [/^mailchimp(_|$)/, "mailchimp"],
  [/^drive(_|$)/, "google-drive"],
  [/^youtube(_|$)/, "youtube"],
  [/^calendar(_|$)/, "google-calendar"],
  [/^sheets(_|$)/, "google-sheets"],
  [/^zoom(_|$)/, "zoom"]
];

/** The connector an action belongs to (slack_… → Slack), or undefined for
    app-agnostic steps. */
export function actionConnector(type: string): ConnectorEntry | undefined {
  const hit = appPrefixes.find(([pattern]) => pattern.test(type));
  return hit ? connectorCatalog.find((entry) => entry.name === hit[1]) : undefined;
}

export function stepSection(type: string): StepSection {
  return stepCategories[type] ?? (actionConnector(type) ? "app" : "data");
}

/** Inside one app, most-wanted verbs first: the app's primary action (its
    bare-named post/send), then writes, then reads, then manage. Ranked off
    the verb after the app prefix — labels read "Slack: find user…",
    "Google Sheets (find row)" — and catalog order breaks ties. */
const verbRanks: Array<[RegExp, number]> = [
  [/^(send|post|create|add|upload|append|invite|share|schedule|quick add)\b/i, 0],
  [/^(find|list|read|look ?up|get|search|download)\b/i, 1]
];

export function actionVerbRank(type: string): number {
  const entry = actionCatalog.find((candidate) => candidate.type === type);
  let verb = entry?.label ?? "";
  const app = actionConnector(type)?.label;
  if (app && verb.toLowerCase().startsWith(app.toLowerCase())) {
    verb = verb.slice(app.length).replace(/^[\s:()]+/, "").replace(/\)$/, "");
  }
  if (!verb.trim()) return 0;
  const hit = verbRanks.find(([pattern]) => pattern.test(verb));
  return hit ? hit[1] : 2;
}
