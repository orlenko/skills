---
name: Agent Observer
description: A calm, evidence-led operations desk for supervising agent work.
colors:
  page: "oklch(97.2% 0.009 78)"
  surface: "oklch(99.1% 0.006 78)"
  surface-muted: "oklch(94.8% 0.013 76)"
  surface-selected: "oklch(93.6% 0.027 230)"
  ink: "oklch(25% 0.022 52)"
  muted: "oklch(46% 0.022 55)"
  faint: "oklch(64% 0.017 62)"
  rule: "oklch(84% 0.018 68)"
  strong-rule: "oklch(63% 0.024 58)"
  accent: "oklch(47% 0.105 238)"
  accent-soft: "oklch(94.2% 0.032 232)"
  model: "oklch(43% 0.09 178)"
  model-soft: "oklch(94.2% 0.026 178)"
  healthy: "oklch(43% 0.09 147)"
  healthy-soft: "oklch(94% 0.025 147)"
  warning: "oklch(52% 0.105 82)"
  warning-soft: "oklch(95% 0.045 87)"
  danger: "oklch(44% 0.15 27)"
  danger-soft: "oklch(94% 0.035 28)"
  focus: "oklch(51% 0.13 242)"
typography:
  display:
    fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif"
    fontSize: "1.5rem"
    fontWeight: 700
    lineHeight: 1.1
    letterSpacing: "-0.025em"
  headline:
    fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif"
    fontSize: "1.15rem"
    fontWeight: 700
    lineHeight: 1.2
    letterSpacing: "-0.012em"
  title:
    fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif"
    fontSize: "0.98rem"
    fontWeight: 700
    lineHeight: 1.25
  body:
    fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif"
    fontSize: "0.925rem"
    fontWeight: 400
    lineHeight: 1.45
  label:
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"
    fontSize: "0.7rem"
    fontWeight: 400
    lineHeight: 1.3
    letterSpacing: "0.035em"
rounded:
  control: "0.45rem"
spacing:
  xs: "0.25rem"
  sm: "0.5rem"
  md: "0.8rem"
  lg: "1.2rem"
  xl: "1.7rem"
components:
  button-primary:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.surface}"
    typography: "{typography.body}"
    rounded: "{rounded.control}"
    padding: "0.5rem 0.72rem"
  button-quiet:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    typography: "{typography.label}"
    rounded: "{rounded.control}"
    padding: "0.36rem 0.54rem"
  input:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    typography: "{typography.body}"
    rounded: "{rounded.control}"
    padding: "0.57rem 0.7rem"
  navigation-active:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.surface}"
    typography: "{typography.body}"
    rounded: "{rounded.control}"
    padding: "0.55rem 0.62rem"
  badge-observed:
    backgroundColor: "{colors.accent-soft}"
    textColor: "{colors.accent}"
    typography: "{typography.label}"
    rounded: "{rounded.control}"
    padding: "0.12rem 0.32rem"
  badge-model:
    backgroundColor: "{colors.model-soft}"
    textColor: "{colors.model}"
    typography: "{typography.label}"
    rounded: "{rounded.control}"
    padding: "0.12rem 0.32rem"
---

# Design System: Agent Observer

## Overview

**Creative North Star: "The Quiet Operations Desk"**

Agent Observer should feel like a dependable instrument placed beside a bank of
terminals: warm enough for prolonged use, exact enough to trust, and quiet until
human judgment is genuinely useful. Its restrained surfaces and typographic
density support quick scanning without turning supervision into surveillance.

The system leads with attention, then exposes evidence and diagnostics through
progressive disclosure. It explicitly rejects the visual language of a generic
infrastructure-monitoring wall, a chat transcript inbox, and a celebratory
project-management board.

**Key Characteristics:**

- Warm, low-chroma paper surfaces with one cool observed-fact accent.
- Strong information hierarchy without decorative illustration or avatars.
- Dense ledgers, familiar controls, and evidence revealed in place.
- Responsive structure that yields navigation and detail space to the primary task.
- Color, labels, and provenance working together so meaning never depends on hue alone.

## Colors

The palette is restrained: warm neutrals carry the interface, cool blue marks
observed facts, teal identifies model review, and semantic yellow or red appears
only when system state truly warrants it.

### Primary

- **Instrument Blue** (`colors.accent` and `colors.accent-soft`): observed requests,
  selected attention, and primary interactive emphasis.

### Secondary

- **Review Teal** (`colors.model` and `colors.model-soft`): model-suggested loose
  ends. It must remain visibly distinct from observed evidence.
- **Healthy Green** (`colors.healthy` and `colors.healthy-soft`): live service and
  successful-state confirmation.
- **Watchful Amber** (`colors.warning` and `colors.warning-soft`): degraded but
  recoverable Observer state.
- **Failure Red** (`colors.danger` and `colors.danger-soft`): actual errors only.

### Neutral

- **Paper Ground** (`colors.page`): the page canvas.
- **Instrument Surface** (`colors.surface`): controls and inspector surfaces.
- **Quiet Wash** (`colors.surface-muted`): hover and secondary separation.
- **Deep Ink** (`colors.ink`): primary text and the strongest active state.
- **Warm Graphite** (`colors.muted` and `colors.faint`): metadata and receding copy.
- **Hairline Rules** (`colors.rule` and `colors.strong-rule`): structural divisions.

### Named Rules

**The Evidence Color Rule.** Blue is reserved for observed evidence and direct
input requests. Teal never impersonates observation.

**The Alarm Rationing Rule.** Amber and red are scarce. Staleness alone is neutral;
red means an actual failure, never ordinary attention.

## Typography

**Display Font:** Native system sans (`typography.display`)

**Body Font:** Native system sans (`typography.body`)

**Label/Mono Font:** Native UI monospace (`typography.label`)

**Character:** The system stack should disappear into the task. Compact bold
headings establish hierarchy; restrained monospace labels make provenance,
timestamps, providers, and machine state feel exact without making the whole
dashboard resemble a terminal.

### Hierarchy

- **Display:** Product identity in the top bar only.
- **Headline:** Workspace and major inspector headings.
- **Title:** Project names, attention claims, and section titles.
- **Body:** Explanations and rendered conversation context, capped near 70 characters
  per line where the content is prose rather than ledger data.
- **Label:** Eyebrows, badges, timestamps, host labels, and provenance. Uppercase is
  reserved for structural labels, never ordinary copy.

### Named Rules

**The Two-Voice Rule.** Human-readable meaning uses the system sans; compact
machine provenance uses monospace. Never render entire messages in monospace.

## Elevation

The interface is flat by default. Tonal shifts and one-pixel rules establish
structure; the single ambient shadow token is reserved for floating option lists
and transient feedback that genuinely sits above the document.

### Shadow Vocabulary

- **Ambient Overlay** (`--shadow`): project autocomplete and toast surfaces only.

### Named Rules

**The Flat Instrument Rule.** Ledgers, inspector sections, notices, and navigation
stay on the document plane. If a static region casts a shadow, remove it.

## Components

### Buttons

- **Shape:** Gently curved, compact controls (`rounded.control`).
- **Primary:** Deep ink surface with light text; reserved for starting a workflow.
- **Hover / Focus:** Hover changes tone without movement. Keyboard focus uses the
  dedicated focus color and a visible three-pixel outline.
- **Quiet:** Surface-colored, thin-rule controls for rescans, disclosure, removal,
  and other secondary actions.

### Chips

- **Style:** Compact mono labels with soft semantic backgrounds and full text labels.
- **State:** Solid borders identify observed facts; dashed treatment identifies model
  review. Color is always accompanied by wording such as “Observed” or “Model review.”

### Cards / Containers

- **Corner Style:** Gently curved only where a bounded surface is functionally useful.
- **Background:** Page and surface tokens; selected ledger rows use the cool selected wash.
- **Shadow Strategy:** Flat except for overlays.
- **Border:** One-pixel rules, never colored side stripes.
- **Internal Padding:** Compact and rhythmically varied; nested cards are prohibited.

### Inputs / Fields

- **Style:** Surface background, one-pixel rule, compact native typography, and the
  shared control radius.
- **Focus:** The global visible focus outline; placeholder text uses the faint neutral.
- **Error / Disabled:** Errors use failure red with direct copy. Disabled controls retain
  readable labels and reduce opacity without disappearing.

### Navigation

- **Style:** Muted text at rest, a quiet neutral hover, and deep ink for the active view.
  Counts remain secondary monospace metadata.
- **Responsive behavior:** At the detail-stacking breakpoint, the project-view rail is
  collapsible and initially yields its width to the ledger. “Show views” and “Hide views”
  remain keyboard-accessible, persist the operator's choice, and never hide the rail on
  wide layouts.

### Attention Ledger

Rows are the product's signature component. They prioritize session name, actionable
claim, and activity age; branch, provider, host, and provenance recede. Selection uses
a calm blue wash, not an alarm color. Dismissal is always a prominent row-level action.

## Do's and Don'ts

### Do:

- **Do** lead every screen with unresolved human attention, then activity and diagnostics.
- **Do** distinguish observed facts, derived facets, and model suggestions in both words and color.
- **Do** preserve keyboard focus, disclosure state, navigation preference, and scroll position across polling.
- **Do** use progressive disclosure so five to ten projects remain calm at a glance.
- **Do** collapse structural navigation before compressing the attention ledger at narrow widths.
- **Do** keep exact evidence selectable and rendered as readable Markdown.

### Don't:

- **Don't** resemble a generic infrastructure-monitoring wall.
- **Don't** turn the product into a chat transcript inbox or a celebratory project-management board.
- **Don't** use decorative agent avatars, gamified activity, or noisy equal-weight alert cards.
- **Don't** use invented certainty or make model inference look as authoritative as source-backed facts.
- **Don't** use red or amber for ordinary attention, selection, quiet work, or staleness alone.
- **Don't** use colored side-stripe borders, gradient text, glassmorphism, nested cards, or decorative motion.
- **Don't** hide the primary attention claim behind provider lifecycle language or raw serialized output.
