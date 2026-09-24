# Dapier console — design contract

The operator console (`src/web/`) and its satellite page (`/auth/error`, rendered
in `src/admin.py`) are the only user-facing surfaces of Dapier. This document is
binding: every change to `app.css` / `index.html` must comply. It exists because
the previous design was technically competent but anonymous — the AI-default
admin panel. The direction that replaces it is **the operator's ledger**: warm
paper, near-black ink, hairline rules, a dark rail for navigation, one deep
control green, and IBM Plex as the voice of the machine.

## The attack (what was wrong and is now banned)

1. Inter-first font stack — no typographic identity.
2. Pastel tint pill badges for every status.
3. One soft border radius shared by every element.
4. An 11px uppercase "eyebrow" micro-label above every heading.
5. Marketing explainer sentences under every heading (one *informational* `.sub`
   line per section is allowed — "Read from `workflows/*.yaml`" — filler is not).
6. Icons inside labelled buttons ("Sign in", "Save credential").
7. The 4-equal-cell SaaS metric card row.
8. Heavy uppercase table headers.
9. Gradients, glassmorphism, decoration that carries no data.

**Banned permanently:** Inter-first stacks, CDN/webfont requests, gradients,
pastel tint chips on statuses, eyebrow micro-labels, icons inside labelled
buttons, more than one shadow idiom per surface. The dark rail and the dialog's
ambient shadow are *committed* choices of this direction — listed here so they
are not "fixed" back into anonymity by a future pass.

## Type

Two faces, self-hosted under `/assets/fonts/` (OFL-licensed IBM Plex), with
system fallbacks so the console still renders offline. The `/auth/error` CSP
permits no external requests, so that page declares the same stacks and falls
back to the system faces:

- **IBM Plex Mono (400/500)** — the voice of machine truth: workflow IDs,
  execution IDs, triggers, scopes, metric values, timestamps, code, the
  environment line. Workflow IDs are YAML filenames, so mono is truthful.
- **IBM Plex Sans (variable, 400–700)** — prose: headings, labels, helper
  sentences, table prose cells.

Fallbacks: `ui-monospace, "SF Mono", Menlo, Consolas, monospace` /
`ui-sans-serif, system-ui, "Segoe UI", sans-serif`.

Scale in use: 11 / 11.5 / 12 / 12.5 / 13 / 13.5 / 15 / 16 / 19 / 24 / 34.
Character comes from the sans/mono split — sans states intent, mono quotes the
machine — never from decoration. Machine values wrap at token separators
(`:`, `.`, `/`, `_`) via `<wbr>`, never mid-token.

## Color

Warm paper + warm ink + one deep control green. Status colors are ink-adjacent
and appear as a dot plus a word — never as tinted chips.

| Token | Value | Use |
|---|---|---|
| `--paper` | `#f4f1e9` | page canvas |
| `--surface` | `#fdfcf7` | cards, inputs, dialogs |
| `--ink` | `#231f17` | primary text |
| `--ink-soft` | `#514b3e` | secondary text |
| `--muted` | `#6e6656` | tertiary text, helper lines |
| `--line` / `--line-strong` | `#ddd5c4` / `#c9c0aa` | hairlines / structural rules |
| `--rail` / `--rail-ink` / `--rail-muted` | `#211c14` / `#ece7da` / `#a89f8c` | dark nav rail |
| `--accent` (+`-deep`/`-soft`/`-line`) | `#166a44` (`#0f5233`/`#e7ede0`/`#ccd8c2`) | primary actions, links, active nav, loading |
| `--ok-dot` | `#1d7a4f` | status: ok |
| `--run-dot` / `--run-text` | `#a5730f` / `#7d5a0d` | status: in progress |
| `--red` (+`-deep`/`-soft`/`-line`) | `#9a3b24` (`#8c3018`/`#f6e6de`/`#e6c9bb`) | status: failed, errors |
| `--off-dot` | `#b5ac99` | status: disabled/neutral |

The sanctioned tint pair: `--accent-soft` / `--red-soft` for the notice banner
and error callouts — feedback surfaces, never decoration.

## Structure

- **Radius system:** 4px controls (buttons, inputs, callouts, copy fields),
  3px inline code chips, 6px dialogs, 50% dots. Nothing else.
- **Hairlines, not cards.** Sections separate by 1px `--line`; structural rules
  (under the topbar, under table headers, the metrics baseline) are 1px
  `--line-strong`.
- **One shadow idiom per surface:** the dialog's ambient
  `0 24px 64px rgba(33,28,20,.22)`; the mobile drawer's while open. Nothing else.
- **Dark rail, paper canvas.** The rail is `--rail` with a 2px green active
  marker; the canvas is `--paper`. The rail is navigation, never content.
- Table rows ~46px; content column unbounded within `main`'s 32px gutters.

## Components

- **Status language:** one pattern everywhere — 7px dot + lowercase word
  (`.status ok/run/err/off`). No chips, no pills, no background.
- **Buttons:** 40px tall. Primary: `--accent` fill, paper text, hover
  `--accent-deep`. Secondary: 1px `--line-strong`, transparent, hover wash.
  Text links: `--accent`, underline on hover. No icons inside labelled buttons.
- **Metrics:** an inline baseline row (sans label + mono value), not cards.
- **Tables:** sentence-case 11.5px muted headers on a `--line-strong` rule;
  13px body cells; mono for machine values; row hover `rgba(35,31,23,.035)`;
  clickable rows open dialogs.
- **Inputs:** 1px `--line-strong`, `--surface` fill, 4px radius; mono for
  machine input (`.mono-input`); focus = 2px `--accent` outline, offset 0.
- **Dialogs:** `--surface` card, 1px `--line-strong`, 6px radius, ambient
  shadow; header / body / actions bands with hairlines between.
- **Icons** (lucide) only where they carry information: nav, refresh / menu /
  logout / close / copy, the text-link arrow. Never inside labelled buttons.
- **Login, forbidden and `/auth/error`:** paper canvas, centered 400px panel —
  Plex Sans wordmark 34px, muted sub-line, hairline-topped content block, mono
  foot line `eu-west-1 · single-operator control plane`. No brand-letter tile,
  no split-screen brand panel.
- **Loading:** a single 2px `--accent` band pulsing across the top. **Mobile
  (≤560):** tables become stacked ledger rows — identity first, then labelled
  slots (`data-label`); the rail becomes a drawer.
