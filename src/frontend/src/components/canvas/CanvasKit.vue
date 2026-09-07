<template>
  <div class="canvas-kit">
    <slot />
  </div>
</template>

<script setup>
/**
 * The canvas design kit (trinity-enterprise#537) — the ONE stylesheet that
 * makes an agent's `html` / `markdown` blocks look designed without the agent
 * touching CSS. The wrapper element and its rules live together here; every
 * canvas surface renders its blocks inside `<CanvasKit>` through
 * `CanvasPanel.vue`, so the kit reaches Agent Detail, the Workspace agent page
 * and the conversation rail from one place.
 *
 * Why an UNSCOPED `<style>` in an SFC, and not a `.css` file:
 *   - Vue's scoped CSS stamps data attributes on compiled template nodes only;
 *     `v-html` children never get them. The `.canvas-kit` prefix on every
 *     selector IS the scope.
 *   - The raw-colour ratchet (`scripts/scan-raw-colors.mjs`) walks `.vue` /
 *     `.js` and counts hex/rgb literals inside `<style>` blocks; a standalone
 *     `.css` file would be invisible to it. Every colour below is a
 *     `theme()` token with a `.dark` override — the `ScanlineReveal.vue`
 *     precedent — so the kit ships at a raw-colour count of zero.
 *
 * Collapse is keyed on the kit's OWN inline size (`@container`), never the
 * viewport: the Portal rail is ~300px wide on a desktop screen, where a
 * viewport media query never fires and a 2/3 + 1/3 layout would crush.
 *
 * Specificity against `@tailwindcss/typography`: the wrappers keep
 * `.prose prose-sm` so plain markup keeps its typography; typography's rules
 * are `:where()`-wrapped (0,1,0) and lose to `.canvas-kit .ck-*` (0,2,0), and
 * the kit explicitly resets the properties it owns on the elements it wraps
 * (margins, table chrome, tile numerals) so nothing bleeds through.
 *
 * The kit is the **v-html twin** of the primitives — `ck-card` carries
 * BaseCard's radius/padding/border, `ck-chip` BaseBadge's pill recipe,
 * `ck-kpi` / `ck-table` the report tile and table's tokens. Agent markup cannot
 * mount a component, which is the one sanctioned exception to primitives-first
 * (design-system.md § Canvas design kit).
 *
 * Two app-emitted families — `.ck-layout-<template>` and `.ck-slot-<slot>` —
 * draw the starter layouts (`canvasLayouts.js`). They are NOT in
 * `utils/canvasKit.js::KIT_CLASSES`, so an agent cannot fake a region.
 */
</script>

<style>
/* ---------------------------------------------------------------- tokens */
.canvas-kit {
  container-type: inline-size;
  container-name: canvas;
  /* Principle 7: wide content scrolls in its own container, never the page.
     The inline-style allowlist admits `width` up to 9999px, so without this
     one admitted value could widen a customer's whole Workspace page. */
  overflow-x: auto;
  --ck-surface: theme('colors.white');
  --ck-ground: theme('colors.gray.50');
  --ck-chrome: theme('colors.gray.100');
  --ck-border: theme('colors.gray.200');
  --ck-border-strong: theme('colors.gray.300');
  --ck-ink: theme('colors.gray.900');
  --ck-ink-2: theme('colors.gray.600');
  --ck-ink-3: theme('colors.gray.500');
  --ck-accent: theme('colors.action-primary.600');
  --ck-info-bg: theme('colors.status-info.100');
  --ck-info-fg: theme('colors.status-info.700');
  --ck-info-line: theme('colors.status-info.500');
  --ck-success-bg: theme('colors.status-success.100');
  --ck-success-fg: theme('colors.status-success.700');
  --ck-success-line: theme('colors.status-success.500');
  --ck-warning-bg: theme('colors.status-warning.100');
  --ck-warning-fg: theme('colors.status-warning.700');
  --ck-warning-line: theme('colors.status-warning.500');
  --ck-danger-bg: theme('colors.status-danger.100');
  --ck-danger-fg: theme('colors.status-danger.700');
  --ck-danger-line: theme('colors.status-danger.500');
  --ck-neutral-bg: theme('colors.gray.100');
  --ck-neutral-fg: theme('colors.gray.700');
  --ck-neutral-line: theme('colors.gray.400');
}
.dark .canvas-kit {
  --ck-surface: theme('colors.gray.800');
  --ck-ground: theme('colors.gray.900');
  --ck-chrome: theme('colors.gray.750');
  --ck-border: theme('colors.gray.750');
  --ck-border-strong: theme('colors.gray.700');
  --ck-ink: theme('colors.gray.100');
  --ck-ink-2: theme('colors.gray.300');
  --ck-ink-3: theme('colors.gray.400');
  --ck-accent: theme('colors.action-primary.400');
  /* token-500 at 16% grounds + token-300 text — the dark tint recipe (design-system.md §2) */
  --ck-info-bg: color-mix(in srgb, theme('colors.status-info.500') 16%, transparent);
  --ck-info-fg: theme('colors.status-info.300');
  --ck-success-bg: color-mix(in srgb, theme('colors.status-success.500') 16%, transparent);
  --ck-success-fg: theme('colors.status-success.300');
  --ck-warning-bg: color-mix(in srgb, theme('colors.status-warning.500') 16%, transparent);
  --ck-warning-fg: theme('colors.status-warning.300');
  --ck-danger-bg: color-mix(in srgb, theme('colors.status-danger.500') 16%, transparent);
  --ck-danger-fg: theme('colors.status-danger.300');
  --ck-neutral-bg: color-mix(in srgb, theme('colors.gray.500') 16%, transparent);
  --ck-neutral-fg: theme('colors.gray.300');
  --ck-neutral-line: theme('colors.gray.500');
}

/* ---------------------------------------------------------------- layout */
.canvas-kit .ck-grid,
.canvas-kit .ck-grid-2,
.canvas-kit .ck-grid-3,
.canvas-kit .ck-grid-4 {
  display: grid;
  gap: 12px;
  margin: 0 0 12px;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
}
.canvas-kit .ck-grid-2 { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.canvas-kit .ck-grid-3 { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.canvas-kit .ck-grid-4 { grid-template-columns: repeat(4, minmax(0, 1fr)); }
.canvas-kit .ck-span-2 { grid-column: span 2; }
.canvas-kit .ck-span-full { grid-column: 1 / -1; }
.canvas-kit .ck-stack { display: flex; flex-direction: column; gap: 12px; margin: 0 0 12px; }
.canvas-kit .ck-row { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin: 0 0 12px; }
.canvas-kit .ck-grid > *,
.canvas-kit .ck-grid-2 > *,
.canvas-kit .ck-grid-3 > *,
.canvas-kit .ck-grid-4 > *,
.canvas-kit .ck-stack > * { margin: 0; min-width: 0; }

/* Narrow container (the rail, a phone): everything becomes one column. */
@container canvas (max-width: 640px) {
  .canvas-kit .ck-grid-2,
  .canvas-kit .ck-grid-3,
  .canvas-kit .ck-grid-4 { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .canvas-kit .ck-span-2 { grid-column: 1 / -1; }
}
@container canvas (max-width: 400px) {
  .canvas-kit .ck-grid,
  .canvas-kit .ck-grid-2,
  .canvas-kit .ck-grid-3,
  .canvas-kit .ck-grid-4 { grid-template-columns: minmax(0, 1fr); }
}

/* ------------------------------------------------------------------ card */
.canvas-kit .ck-card {
  background: var(--ck-surface);
  border: 1px solid var(--ck-border);
  border-radius: 8px;
  padding: 16px;
  margin: 0 0 12px;
  box-shadow: theme('boxShadow.sm');
  min-width: 0;
}
.canvas-kit .ck-card > :first-child { margin-top: 0; }
.canvas-kit .ck-card > :last-child { margin-bottom: 0; }
.canvas-kit .ck-card-title {
  font-size: 14px;
  font-weight: 550;
  line-height: 1.3;
  color: var(--ck-ink);
  margin: 0 0 4px;
}
.canvas-kit .ck-card-meta {
  font-size: 12.5px;
  color: var(--ck-ink-3);
  margin: 0 0 8px;
}
.canvas-kit .ck-card-body { font-size: 14px; color: var(--ck-ink-2); }

/* --------------------------------------------------------------- section */
.canvas-kit .ck-section {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 4px 12px;
  margin: 24px 0 12px;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--ck-border);
}
.canvas-kit .ck-section:first-child { margin-top: 0; }
.canvas-kit .ck-section-title {
  font-size: 18px;
  font-weight: 650;
  line-height: 1.25;
  color: var(--ck-ink);
  margin: 0;
}
.canvas-kit .ck-section-sub {
  font-size: 12.5px;
  color: var(--ck-ink-3);
  margin: 0;
}

/* ------------------------------------------------------------------- kpi */
/* The v-html twin of ReportKpiTiles.vue — same surface, border, radius, sizes. */
.canvas-kit .ck-kpi {
  background: var(--ck-surface);
  border: 1px solid var(--ck-border);
  border-radius: 8px;
  padding: 8px 12px;
  margin: 0 0 12px;
  min-width: 0;
}
.canvas-kit .ck-kpi > * { margin: 0; }
.canvas-kit .ck-kpi-label {
  font-size: 11px;
  font-weight: 500;
  letter-spacing: 0.05em;
  text-transform: uppercase;
  color: var(--ck-ink-3);
}
.canvas-kit .ck-kpi-value {
  font-size: 24px;
  font-weight: 700;
  line-height: 1.2;
  font-variant-numeric: tabular-nums;
  color: var(--ck-ink);
}
.canvas-kit .ck-kpi-unit {
  font-size: 12.5px;
  font-weight: 400;
  color: var(--ck-ink-3);
  margin-left: 2px;
}
.canvas-kit .ck-kpi-delta {
  font-size: 12.5px;
  font-variant-numeric: tabular-nums;
  color: var(--ck-ink-3);
}
.canvas-kit .ck-kpi-delta.ck-up::before { content: '▲ '; }
.canvas-kit .ck-kpi-delta.ck-down::before { content: '▼ '; }
.canvas-kit .ck-kpi-delta.ck-flat::before { content: '— '; }
.canvas-kit .ck-up { color: var(--ck-success-fg); }
.canvas-kit .ck-down { color: var(--ck-danger-fg); }
.canvas-kit .ck-flat { color: var(--ck-ink-3); }

/* ----------------------------------------------------------------- table */
/* The v-html twin of ReportTable.vue, bounded per principle 28. */
.canvas-kit .ck-table-wrap {
  overflow: auto;
  max-height: 420px;
  border: 1px solid var(--ck-border);
  border-radius: 8px;
  margin: 0 0 12px;
}
.canvas-kit .ck-table {
  width: 100%;
  min-width: 100%;
  border-collapse: collapse;
  font-size: 14px;
  line-height: 1.4;
  margin: 0;
  color: var(--ck-ink-2);
}
.canvas-kit .ck-table thead th {
  position: sticky;
  top: 0;
  background: var(--ck-chrome);
  color: var(--ck-ink-3);
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 11px;
  font-weight: 500;
  letter-spacing: 0.05em;
  text-transform: uppercase;
  text-align: start;
  padding: 6px 12px;
  border-bottom: 1px solid var(--ck-border);
}
.canvas-kit .ck-table td {
  padding: 6px 12px;
  border-bottom: 1px solid var(--ck-border);
  vertical-align: top;
}
.canvas-kit .ck-table tbody tr:last-child td { border-bottom: 0; }
.canvas-kit .ck-num {
  font-variant-numeric: tabular-nums;
  text-align: end;
}

/* --------------------------------------------------------------- callout */
.canvas-kit .ck-callout {
  border-left: 3px solid var(--ck-neutral-line);
  background: var(--ck-neutral-bg);
  color: var(--ck-neutral-fg);
  border-radius: 6px;
  padding: 8px 12px;
  margin: 0 0 12px;
  font-size: 14px;
}
.canvas-kit .ck-callout > :first-child { margin-top: 0; }
.canvas-kit .ck-callout > :last-child { margin-bottom: 0; }
.canvas-kit .ck-callout.ck-info { border-color: var(--ck-info-line); background: var(--ck-info-bg); color: var(--ck-info-fg); }
.canvas-kit .ck-callout.ck-success { border-color: var(--ck-success-line); background: var(--ck-success-bg); color: var(--ck-success-fg); }
.canvas-kit .ck-callout.ck-warning { border-color: var(--ck-warning-line); background: var(--ck-warning-bg); color: var(--ck-warning-fg); }
.canvas-kit .ck-callout.ck-danger { border-color: var(--ck-danger-line); background: var(--ck-danger-bg); color: var(--ck-danger-fg); }

/* ------------------------------------------------------------------ chip */
/* The v-html twin of BaseBadge: pill, 11.5/550, tint ground + tone text. */
.canvas-kit .ck-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  border-radius: 9999px;
  padding: 2px 8px;
  font-size: 11.5px;
  font-weight: 550;
  line-height: 1.4;
  white-space: nowrap;
  background: var(--ck-neutral-bg);
  color: var(--ck-neutral-fg);
}
.canvas-kit .ck-chip.ck-info { background: var(--ck-info-bg); color: var(--ck-info-fg); }
.canvas-kit .ck-chip.ck-success { background: var(--ck-success-bg); color: var(--ck-success-fg); }
.canvas-kit .ck-chip.ck-warning { background: var(--ck-warning-bg); color: var(--ck-warning-fg); }
.canvas-kit .ck-chip.ck-danger { background: var(--ck-danger-bg); color: var(--ck-danger-fg); }
.canvas-kit .ck-chip.ck-neutral { background: var(--ck-neutral-bg); color: var(--ck-neutral-fg); }

/* ---------------------------------------------------------------- figure */
.canvas-kit .ck-figure {
  margin: 0 0 12px;
  padding: 0;
  min-width: 0;
}
.canvas-kit .ck-figure > img,
.canvas-kit .ck-figure > svg { margin: 0; max-width: 100%; border-radius: 6px; }
.canvas-kit .ck-caption {
  font-size: 12.5px;
  color: var(--ck-ink-3);
  margin: 4px 0 0;
}

/* ------------------------------------------------------------------ text */
.canvas-kit .ck-muted { color: var(--ck-ink-3); }
.canvas-kit .ck-small { font-size: 12.5px; }
.canvas-kit .ck-mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.9em; }
.canvas-kit .ck-right { text-align: end; }
.canvas-kit .ck-center { text-align: center; }

/* --------------------------------------------------- starter layouts (app-emitted) */
.canvas-kit .ck-layout {
  display: grid;
  gap: 16px;
  margin-bottom: 16px;
  grid-template-columns: repeat(3, minmax(0, 1fr));
}
.canvas-kit .ck-slot { min-width: 0; }
.canvas-kit .ck-slot > * + * { margin-top: 12px; }
.canvas-kit .ck-slot-grid {
  display: grid;
  gap: 12px;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  align-items: start;
}
.canvas-kit .ck-slot-grid > * + * { margin-top: 0; }

.canvas-kit .ck-layout-dashboard {
  grid-template-areas: 'header header header' 'kpis kpis kpis' 'main main side' 'footer footer footer';
}
.canvas-kit .ck-layout-dashboard > .ck-slot-header { grid-area: header; }
.canvas-kit .ck-layout-dashboard > .ck-slot-kpis { grid-area: kpis; }
.canvas-kit .ck-layout-dashboard > .ck-slot-main { grid-area: main; }
.canvas-kit .ck-layout-dashboard > .ck-slot-side { grid-area: side; }
.canvas-kit .ck-layout-dashboard > .ck-slot-footer { grid-area: footer; }

.canvas-kit .ck-layout-report {
  grid-template-areas: 'header header header' 'summary summary summary' 'body body body' 'figures figures figures' 'appendix appendix appendix';
}
.canvas-kit .ck-layout-report > .ck-slot-header { grid-area: header; }
.canvas-kit .ck-layout-report > .ck-slot-summary { grid-area: summary; }
.canvas-kit .ck-layout-report > .ck-slot-body { grid-area: body; max-width: 72ch; }
.canvas-kit .ck-layout-report > .ck-slot-figures { grid-area: figures; }
.canvas-kit .ck-layout-report > .ck-slot-appendix { grid-area: appendix; }

.canvas-kit .ck-layout-brief {
  grid-template-areas: 'header header header' 'key-points body body';
}
.canvas-kit .ck-layout-brief > .ck-slot-header { grid-area: header; }
.canvas-kit .ck-layout-brief > .ck-slot-key-points { grid-area: key-points; }
.canvas-kit .ck-layout-brief > .ck-slot-body { grid-area: body; max-width: 72ch; }

.canvas-kit .ck-layout-status-board {
  grid-template-areas: 'header header header' 'status status status' 'issues issues next' 'log log log';
}
.canvas-kit .ck-layout-status-board > .ck-slot-header { grid-area: header; }
.canvas-kit .ck-layout-status-board > .ck-slot-status { grid-area: status; }
.canvas-kit .ck-layout-status-board > .ck-slot-issues { grid-area: issues; }
.canvas-kit .ck-layout-status-board > .ck-slot-next { grid-area: next; }
.canvas-kit .ck-layout-status-board > .ck-slot-log { grid-area: log; }

/* Narrow: a layout is one column, in slot order. `display: flex` ignores
   grid-area, so every region above falls into place without a second map. */
@container canvas (max-width: 640px) {
  .canvas-kit .ck-layout { display: flex; flex-direction: column; }
  .canvas-kit .ck-layout > .ck-slot { max-width: none; }
}

/* Reduced motion: the kit animates nothing, and the app's global
   background/border transition is fine — nothing to override here. */
</style>
