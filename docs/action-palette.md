# Organizing the palette: triggers & actions

Design proposal, 2026-09-30. Problem first observed on the designer board: the
toolbar is one long flat strip of icon chips — 18 trigger chips, then an
"ACTIONS" label, then 111 action chips plus the Note chip (130 total), with no
search and no grouping beyond the single trigger/action split. The inspector's
Type selector is a flat `<select>` with 111 options in catalog order.

## Current inventory (from `designer/src/catalog.ts`)

| Group | Actions |
|---|---|
| Slack | 15 (`slack` send + 14 `slack_*`) |
| Zoom | 14 |
| Telegram | 9 |
| Google Sheets | 9 |
| Dropbox | 8 |
| Google Drive | 8 |
| YouTube | 7 |
| Amazon S3 | 6 |
| Mailchimp | 5 |
| Google Calendar | 5 |
| Email (SES/Gmail) | 2 + DataOps intake |
| Built-ins | ~20: http_request, webhook, agent, ai_complete, render, run_workflow, filter/condition/paths/delay/for_each, digest ×3, storage ×4, code/js, csv ×2 |

Labels are also inconsistent across the catalog: "Slack: find user by email"
(colon), "Google Sheets (find row)" (parens), "Telegram find chat" (nothing),
bare "Upload file" (Drive) vs "Upload video" (YouTube) vs "Amazon S3" (a whole
product name for one upload action), "Mailchimp" for the upsert action.

## What Zapier, Make, and n8n do that fits here

1. **App first, event second (Zapier).** You never pick from 6,000 actions;
   you pick the app, then the event inside it. Our connector identities
   already exist — the catalog just doesn't use them for actions.
2. **A searchable picker, not a chip strip (Zapier, n8n).** Search is the
   primary mechanism past ~20 items; grouping is for browsing. Our palette has
   neither.
3. **Resource grouping inside an app (Zapier's Slack: Messages / Channels /
   Users / Reminders / Files).** Within a group, actions read as verb +
   resource: "Send message", "Find user", "Add reaction".
4. **Instant vs polling badges (Zapier), triggers separated from actions
   (n8n).** Operators care whether a trigger fires immediately (hooks) or
   waits for a poll. The truth already lives in
   `src/dapier/triggers/hook_triggers.py` vs `poll_sources.py`; the palette
   doesn't show it.
5. **Popular / recent shortcuts (Zapier "Popular", n8n recents).**
6. **"Describe what you want to do" (Zapier AI picker).** Dapier already ships
   a copilot — natural-language action search is cheap to add later.
7. **Consistent naming:** the app comes from the group header; the action
   label is verb + resource. Full "App: label" stays only where there is no
   group context (canvas node titles, search results).

## Proposed model

**1. Data-driven grouping in the catalog.** `catalog.ts` is already the single
place to edit the palette; give entries the metadata instead of hard-coding UI
groups:

```ts
export interface ActionEntry {
  type: string;
  label: string;          // becomes "Send message", not "Slack"
  group: string;          // "slack" | "zoom" | "core" | "logic" | "data" | …
  keywords?: string;      // extra search terms, e.g. "email send ses"
  …
}
```

Groups: one per connector app (slack, telegram, email, zoom, calendar,
sheets, drive, youtube, dropbox, s3, mailchimp), plus built-ins by function —
`core` (http_request, webhook, agent, ai_complete, render, run_workflow,
dataops), `logic` (filter, condition, paths, delay, for_each, digest ×3),
`data` (code, js, csv ×2, storage ×4). Within big apps, optional subgroup
headers only where they earn it: Slack → Messages / Channels / Users; Zoom →
Meetings / Webinars / Recordings / Participants.

**2. Replace the 111-chip action strip with an "Add step" picker.** A button
opens a modal/popover: search box at top (matching label, type, description,
keywords), then results/app groups below, keyboard-navigable. Rows stay
draggable so chip-drag onto the canvas keeps working. The trigger strip stays
(chip count is fine at 18) but gets cluster headers — Messages (email, slack,
telegram, gmail), Files (dropbox, s3, drive), Calendar & meetings (calendar,
zoom), Feeds & data (rss, youtube, sheets, mailchimp), Schedule & generic
(schedule, poll, custom, renderer, ai) — and an instant/poll badge mirroring
the hook/poll registries.

**3. Inspector Type select → `<optgroup>` per group.** Native optgroups are a
one-line change per group and fix the worst flat list without a custom
combobox.

**4. Naming pass.** `label` becomes verb + resource ("Find row", "Send
scheduled message", "Upload file"); the group supplies the app. Search
keywords carry the legacy phrasing so "gmail" still finds it. Canvas node
titles render "App · label" so nothing is ambiguous off-palette.

**5. Later rounds.** Recently used actions pinned at the top of the picker
(localStorage); copilot-powered "describe the step" search; "pairs well with"
chaining hints (read file → upload; find → find-or-create).

## Scope notes

- Entirely presentational: no engine, API, or CLI change (AGENTS.md rule 5 —
  layout/filtering needs no CLI counterpart). `catalog.ts` stays the one file
  to touch when adding an action; grouping rides on the entry itself.
- Implement in `designer/src`, rebuild with `make designer-console`
  (`designer.js` is a build artifact; verify the tail after rebuild — known
  rebuild race).
- Check `tests/test_designer.py` for assertions on palette labels before the
  naming pass; update tests and labels in the same commit.
