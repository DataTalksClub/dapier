# Dapier console — design contract

The operator console (`src/web/`) is the only user-facing surface of Dapier. This
document is binding: every change to `app.css` / `index.html` must comply. It exists
because the previous design was technically competent but anonymous — the AI-default
admin panel. The direction that replaces it is **engineered instrument**: a monospace-
first, hairline-ruled control surface that looks like the YAML-driven tool it is.

## The attack (what was wrong and is now banned)

1. Inter-first font stack — no typographic identity.
2. Dark sidebar beside a light canvas — the template admin layout.
3. An 11px uppercase "eyebrow" micro-label above every heading.
4. A muted explainer sentence under nearly every heading.
5. Pastel tint pill badges for every status.
6. The same 5–6px border radius on every element.
7. Icons on primary buttons ("Sign in", "Save credential").
8. The 4-equal-cell SaaS metric row.
9. Heavy uppercase table headers and soft ambient dialog shadows.

**Banned permanently:** Inter-first stacks, gradients, pastel tint chips, border-radius
on boxes (the only exception is the 50% status dot), soft multi-layer shadows,
eyebrow micro-labels, icons inside buttons, glassmorphism.

## Type

Two faces, self-hosted under `/assets/fonts/` (OFL-licensed IBM Plex), with system
fallbacks so the console still renders offline:

- **Instrument (identity, display, data, UI chrome):** IBM Plex Mono (400/500).
  Headings are mono, sized by hierarchy not weight. IDs, table cells, metrics,
  nav, buttons, badges are mono — workflow IDs are YAML filenames, so mono is truthful.
- **Prose (paragraphs, helper text):** IBM Plex Sans (variable). Used only where
  sentences occur.

Scale: 11 / 12 / 13 / 15 / 20 / 28 / 44. Character comes from case and weight
contrast (11px mono labels vs 32px mono numerals), never from decoration.

## Color

Warm paper + warm ink + one signal accent. Status colors are ink-adjacent and appear
as text and glyphs only — never as tinted backgrounds.

| Token | Value | Use |
|---|---|---|
| `--canvas` | `#f1efe8` | page background |
| `--surface` | `#faf9f4` | panels, sidebar |
| `--ink` | `#191713` | text, primary fills, structural rules |
| `--muted` | `#6e6857` | secondary text |
| `--line` | `#d9d4c4` | hairlines |
| `--accent` | `#c2410c` | links, focus, active markers, brand mark |
| `--ok` / `--warn` / `--bad` | `#3d6b4f` / `#96660a` / `#a83c28` | status text/glyphs |

## Structure

- **Radius 0** everywhere except the status dot (50%).
- **Hairlines, not cards.** Sections are separated by 1px `--line` rules; structural
  rules (top of metrics band, under the topbar, table tops) are 1px `--ink`.
- **Shadows are hard:** the only shadow is the dialog's `6px 6px 0` ink offset.
- **Chrome is paper.** The sidebar is `--surface` with ink text; the active nav item
  is an ink block. No dark sidebar.
- Spacing on a 4px grid; row heights 44–64px; content max-width 1280px.

## Components

- **Buttons:** rectangles, mono 12px. Primary: ink fill, bone text, hover → accent.
  Secondary: 1px ink border, transparent, hover → ink fill. No icons inside buttons.
- **Status markers** (ex-badges): mono 11px uppercase, leading glyph (`●` ok,
  `▲` warn, `■` bad, `○` neutral) in the status color; no background, no border.
- **Tables:** header row mono 11px uppercase muted on `--line` hairline; body cells
  12px mono for values, sans only in prose columns; hover row = `#e9e6da`.
- **Inputs:** 1px ink border, paper background, mono input text, 0 radius.
- **Dialogs:** 1px ink border, hard offset shadow, paper background.
- **Focus:** 2px solid `--accent`, offset 2px. **Loading bar:** single ink sweep.
- **Icons** (lucide) only where they carry information: nav, row connectors, status
  actions like copy/close/refresh. Never inside buttons with text labels.
- **Auth gate** (forbidden / server-rendered login shares this treatment): full-bleed
  ink panel with the mono wordmark and an oversized outlined glyph; content on paper.
  No brand-letter tile.
