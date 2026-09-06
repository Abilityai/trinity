# Trinity Design System

> **Purpose**: The system of record for Trinity's frontend visual and behavioral standard. Written for human contributors **and** AI coding agents: every rule is imperative and checkable. Read this before writing or reviewing anything under `src/frontend/`.
>
> **System of record**: this document + the semantic tokens in `src/frontend/tailwind.config.js` (#67) + the component reference page. The reference page is the rendered visual spec (approved in the design session, 2026-07); it is ultimately re-rendered from the real primitives so it cannot drift. **When code and this document disagree, the document wins and the code migrates.** Epic: #1430.

---

## 1. Color tokens

Every color in the product is a **semantic token**. Raw Tailwind palette classes (`bg-green-500`, `text-red-600`) are migration debt — the CI ratchet (§9) drives them to zero. Never add a new one.

**The semantics rule** — pick the family by *meaning*, not by hue:

> `status-*` = result of an event · `state-*` = operating mode · `brand-*` = third-party identity · `accent-*` = decoration · `action-*` = interaction. **If it isn't one of these, it's gray.**

### Token families

| Token family | Tailwind alias | Meaning | Workhorse shades |
|---|---|---|---|
| `status-success` | green | healthy · completed · passing | 500 `#22c55e` · 600 `#16a34a` |
| `status-warning` | yellow | degraded · needs attention soon | 500 `#eab308` · 600 `#ca8a04` |
| `status-danger` | red | failed · destructive actions | 500 `#ef4444` · 600 `#dc2626` |
| `status-info` | blue | running · queued · neutral notice | 500 `#3b82f6` · 600 `#2563eb` |
| `status-urgent` | orange | escalated · act now | 500 `#f97316` · 600 `#ea580c` |
| `state-autonomous` | amber | operating mode: autonomous loop | 500 `#f59e0b` · 600 `#d97706` |
| `state-locked` | rose | operating mode: locked / restricted | 500 `#f43f5e` · 600 `#e11d48` |
| `brand-claude` | orange | third-party identity: Claude | 500 `#f97316` · 600 `#ea580c` |
| `brand-gemini` | blue | third-party identity: Gemini | 500 `#3b82f6` · 600 `#2563eb` |
| `accent-purple` | purple | decorative highlight, non-status | 500 `#a855f7` · 600 `#9333ea` |
| `action-primary` | indigo | interactive: buttons · links · focus | 500 `#6366f1` · 600 `#4f46e5` |
| `gray` | gray (+ custom 750) | everything else — chrome, text, borders | see §2 |

Each family exposes the full 100–900 ramp; the workhorses above cover almost every use. Key non-workhorse shades that recur in recipes: `100` (light tinted grounds), `300` (dark-theme text on tints), `700` (light-theme text on tints), `400` (dark-theme solid accents).

**`gray-750` = `#2a303c`** is Trinity's custom shade — the dark-theme chrome/border step between gray-700 and gray-800. It is part of the system; primitives depend on it.

- **Do:** `bg-status-success-100 text-status-success-700`
- **Don't:** `bg-green-100 text-green-700` — identical pixels, but invisible to the token layer and dead weight for any future palette swap.

## 2. Both themes are first-class

The app ships `darkMode: 'class'`. Every primitive and pattern is specced, built, and verified in **light and dark**. Components consume tokens only — a component never carries a per-theme hardcoded color; the theme mapping lives in the token layer.

### Surfaces

| Role | Light | Dark |
|---|---|---|
| Ground (page) | gray-50 `#f9fafb` | gray-900 `#111827` |
| Surface (cards, modals, panels) | white `#ffffff` | gray-800 `#1f2937` |
| Chrome (table headers, wells, chips) | gray-100 `#f3f4f6` | gray-750 `#2a303c` |
| Field (input backgrounds) | white `#ffffff` | gray-900 `#111827` |
| Border | gray-200 `#e5e7eb` | gray-750 `#2a303c` |
| Border strong (controls, dividers that must read) | gray-300 `#d1d5db` | gray-700 `#374151` |
| Shadow (sm) | `0 1px 2px rgba(17,24,39,.06), 0 1px 3px rgba(17,24,39,.08)` | `0 1px 2px rgba(0,0,0,.35)` |
| Shadow (lg — modals, toasts) | `0 10px 30px rgba(17,24,39,.18)` | `0 14px 40px rgba(0,0,0,.5)` |

### Text ink — and the dark ink ladder

| Role | Light | Dark |
|---|---|---|
| Primary | gray-900 `#111827` | gray-100 `#f3f4f6` |
| Secondary (meta, descriptions) | gray-600 `#4b5563` | gray-300 `#d1d5db` |
| Tertiary (help text, overlines, de-emphasis) | gray-500 `#6b7280` | gray-400 `#9ca3af` |

**The dark ink ladder** (design session, 2026-07): in dark theme, text steps are **one step lighter than naive inversion** — primary gray-100, secondary gray-300, tertiary gray-400. **gray-500 is the floor**: disabled states and pure decoration only, **never meta text**. This keeps all readable text at ≥4.5:1 AA contrast on gray-800 surfaces.

- **Do (dark):** `dark:text-gray-300` for "Last run 12 min ago".
- **Don't:** `dark:text-gray-500` on anything a user is expected to read.
- **Also don't:** ship a light-theme `text-gray-500` with *no* `dark:` override. It is not neutral — the light tertiary is reused verbatim in dark, where it lands exactly on the floor. This is the quieter half of the breach and the harder half to catch in review (#1922).
- **Enforced, per file:** `npm run check:tokens` holds the ladder over the files a sweep has already cleaned (`INK_LADDER_SWEPT` in `scripts/check-design-tokens.mjs`) — nothing below the floor, no missing `dark:` override, and a per-file cap on floor-level ink that may be lowered but never raised. It also reads FleetGrid's `--gv-*` ink, which states its ladder as hex custom properties that no class scanner can see. Fleet-wide enforcement waits on the raw-color ratchet (#1430); each sweep adds its files to the set and they can never regress.

### Interactive accent (`action-primary`) per theme

| Role | Light | Dark |
|---|---|---|
| Accent | 600 `#4f46e5` | 500 `#6366f1` |
| Accent hover | 700 `#4338ca` | 400 `#818cf8` |
| Accent soft (tinted ground) | 100 `#e0e7ff` | 500 at 16% `rgba(99,102,241,.16)` |
| Focus ring | 500 at ~40% `rgba(99,102,241,.38)` | 400 at ~42% `rgba(129,140,248,.42)` |

Solid status accents follow the same shift: **600 in light, 400–500 in dark** (success `#16a34a`→`#4ade80`, warning `#ca8a04`→`#facc15`, danger `#dc2626`→`#ef4444` with hover `#b91c1c`→`#f87171`, info `#2563eb`→`#60a5fa`, urgent `#ea580c`→`#fb923c`).

## 3. Typography

System UI stack — what the product already ships. **Six sizes, no more.**

| Role | Spec |
|---|---|
| Page title | 24px / 700, letter-spacing −.02em |
| Section heading | 18px / 650 |
| Card title / control label | 14px / 550 |
| Body | 14px / 400, line-height 1.55 |
| Meta | 12.5px / 400, secondary ink |
| Overline label | mono, 11px / 500, uppercase, tracking .12em, tertiary ink |

- `--sans: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", sans-serif`
- `--mono: ui-monospace, "SF Mono", SFMono-Regular, Menlo, Consolas, monospace`
- Numbers in tables, stats, and durations always get `font-variant-numeric: tabular-nums`.
- Overlines are always mono + uppercase + letter-spaced; dense chrome (table headers, stat labels) may drop to 10.5px.
- Mono is also the voice of machine text: cron expressions, slugs, prompts, config (see BaseTextarea).

## 4. Spacing & radius

**4-px base grid, spent in five named steps:**

| Step | Use |
|---|---|
| 4 | tight — icon↔label gaps |
| 8 | related — controls in a row |
| 12 | grouped — items in a list/stack |
| 16 | card padding |
| 24 | section separation |

**Two radii do all the work:** `6px` (rounded-md) on controls — buttons, inputs, selects; `8–10px` (rounded-lg/xl) on surfaces — cards 8px, modals and panels 10px. **Pills (`rounded-full`) are reserved for badges, toggles, and avatars only.** Off-grid spacing and novel radii are review flags.

## 5. Primitives catalog

Shared primitives are the unit of consistency: **compose them, never re-implement them.** A hand-rolled button/input/badge/card is a defect even when it looks identical. All primitives consume tokens only, work in both themes, and are keyboard/focus correct.

**Shipped implementations (#2122)** — the form/surface layer lives in `src/frontend/src/components/base/` (the Input/Select/Textarea field recipe is one shared constant, `base/fieldClasses.js`, so the three cannot drift):

| Primitive | File | Reference adoption |
|---|---|---|
| BaseButton | `components/base/BaseButton.vue` | `ConfirmDialog.vue` actions · TemplateRegistryPanel Save/Reset |
| BaseInput | `components/base/BaseInput.vue` | TemplateRegistryPanel registry URL |
| BaseSelect | `components/base/BaseSelect.vue` | `ResourceModal.vue` memory/CPU |
| BaseToggle | `components/base/BaseToggle.vue` | TemplateRegistryPanel enable switch |
| BaseTextarea | `components/base/BaseTextarea.vue` | `SystemInstallPanel.vue` manifest editor (mono) |
| BaseBadge | `components/base/BaseBadge.vue` | TemplateRegistryPanel status + Default chips |
| BaseCard | `components/base/BaseCard.vue` | `CredentialSetupChecklist.vue` surface |

The dark tinted-ground recipe `token-500 at 16%` is expressible as `token-500/16` because the config extends the opacity scale with `16` — before #2122 those classes silently compiled to **nothing** (16 is not in Tailwind's default scale), so the dark chips that used them shipped without a background. The behavioral/state primitives were already shipped and keep their homes: `ConfirmDialog.vue`, `OverflowTabs.vue`, `LoadFailed.vue`/`InlineError.vue`, `ScanlineReveal.vue`.

### BaseButton

**Anatomy:** inline-flex, 7px icon gap, radius 6px, 1px transparent border, transition 120ms ease (background/border/color).
**Sizes:** md — 13.5/500, padding 7×14 · sm — 12.5/500, padding 4×10 (card footers, table rows).
**Variants:**

| Variant | Recipe (light) | Recipe (dark) |
|---|---|---|
| primary | bg action-primary-600, white text; hover 700 | bg action-primary-500; hover 400 |
| secondary | surface bg, primary ink, border-strong; hover chrome bg | same roles, dark mappings |
| danger | bg status-danger-600, white text; hover 700 | bg status-danger-500; hover 400 |
| ghost | transparent, action-primary text; hover accent-soft bg | same roles, dark mappings |

**States:** disabled — opacity .45, `cursor: not-allowed` · in-flight — inline 16px spinner (2px border, top-colored, 0.7s linear rotation) + progressive label ("Deploying…"); the control acknowledges the press, honest-state rule 18 · focus — `:focus-visible` ring: `0 0 0 2px <surface>, 0 0 0 4px <ring>` on **all** variants, never `outline: none` alone.
**Rules:** one primary per view (principle 11); destructive verbs are named ("Delete agent", never "OK").

- **Do:** `<BaseButton variant="danger">Delete agent</BaseButton>`
- **Don't:** `<button class="bg-red-600 text-white rounded px-3 py-1">OK</button>` — raw palette, unnamed verb, no focus ring.

### BaseInput

**Anatomy:** label above (13/550) → control → help below (12, tertiary ink).
**Recipe:** field bg (white / gray-900), 1px border-strong (gray-300 / gray-700), radius 6px, padding 8×11, 13.5 primary ink.
**States:** focus — border action-primary + 3px ring (`--ring` per theme), `outline: none` only because the ring replaces it · invalid — border status-danger; focus ring `rgba(239,68,68,.25)` · error message — 12.5, danger text, 14px icon, and it **names the problem and the fix with an example** (principle 17): "Invalid cron expression — expected 5 fields, got 4. Example: `0 9 * * 1`".

- **Do:** label + help + named error with example.
- **Don't:** a bare red border with no message — color is not an error message.

### BaseSelect

Same field recipe as BaseInput. `appearance: none`, custom 14px chevron absolutely positioned right 10px (tertiary ink, pointer-events none), padding-right 32px so text never collides. Same focus/error treatment.

- **Do:** reuse the shared select for every dropdown.
- **Don't:** ship a per-view custom dropdown when a select does the job.

### BaseToggle

**Recipe:** 36×20 pill track; 16px white knob inset 2px, travels left 2→18; track border-strong when off, action-primary accent when on; 150ms ease; label 13.5 alongside.
**Rules:** toggles are for **instant-apply binary settings**. Keyboard: focusable, Space toggles, visible focus ring.

- **Do:** "Schedule enabled" — flips immediately, state is the feedback.
- **Don't:** use a toggle for something that requires a Save button — that's a checkbox in a form.

### BaseTextarea

Textareas are a primitive, not a styled `<textarea>` in place (design session, 2026-07).
**Recipe:** BaseInput field recipe + `min-height: 84px`, `resize: vertical` **only** (horizontal resize breaks layout — principles 6/7), line-height 1.5.
**Mono variant:** 12.5 mono for prompts, instructions, and config text — preserves line breaks and indentation.
**States:** identical focus + error treatment to BaseInput.

- **Do:** mono variant for an agent's instructions field.
- **Don't:** `resize: both` or free-height textareas that reflow the page as the user drags.

### BaseBadge

Driven directly by the token families — the badge variant *is* the token family name.
**Recipe:** pill (radius full), 11.5/550, letter-spacing .01em, padding 2.5×9, `white-space: nowrap`; optional 6px status dot in `currentColor`.

| Theme | Ground | Text |
|---|---|---|
| Light | token-100 | token-700 |
| Dark | token-500 at 16% | token-300 |
| Neutral (light) | gray-100 | gray-600 |
| Neutral (dark) | gray-750 | gray-400 |

**Rule:** a badge answers **one** question — status, mode, or identity — never two at once. Two facts = two badges.

- **Do:** `Healthy` (status-success) next to `Claude` (brand-claude) as separate badges.
- **Don't:** invent an unlisted color combination or merge "healthy + autonomous" into one badge.

### BaseCard

**One surface recipe everywhere:** surface bg (white / gray-800), 1px border (gray-200 / gray-750), radius 8px, padding 16, shadow-sm. Stat cards, agent tiles, list panels are **compositions** of it, not new inventions.
**Stat composition:** overline label (mono 10.5 caps) → value 30/700 tabular-nums, letter-spacing −.02em → delta line 12.5 in status color.
**Tile composition:** 34px avatar (radius 7, accent-soft bg, accent text) + name 14/650 + meta 12/tertiary; footer separated by a border, 12px above and below.

- **Do:** build a new panel from BaseCard and override nothing.
- **Don't:** a one-off surface with its own border color, shadow, or 20px padding.

### Modal shell & ConfirmDialog

**One shell for every dialog.**
**Recipe:** overlay gray-950 at 55% (`rgba(10,14,22,.55)`) · card — surface bg, 1px border, radius 10, shadow-lg, `max-width: 400px`, padding 20 · title 15.5/650 · body 13.5 secondary ink · actions right-aligned, 10px gap, **safe choice first** (left), destructive last.
**Behavior:** Esc closes; click-outside closes; focus is trapped; **initial focus lands on the safe action, never the destructive one**. Destructive confirms restate the consequence: "This permanently removes the agent and its workspace. 3 schedules will be cancelled. This cannot be undone." (principle 19).

- **Do:** `Cancel` focused, `Delete agent` as the named danger action.
- **Don't:** "Are you sure? — OK / Cancel" with focus on OK.

### OverflowTabs

The existing `OverflowTabs` component (#1114, `docs/memory/feature-flows/agent-detail-tab-overflow.md`) is the tab primitive — **adopt everywhere**, never a second tab implementation.
**Recipe:** underline style on a 1px bottom border; tab 13.5, padding 9×13; inactive — gray-600 / gray-400, hover to primary ink; active — action-primary text + 2px underline, weight 550. Tabs that don't fit collapse into a **counted** overflow chip ("+3 more" — chrome bg, pill), re-measured on resize.

- **Do:** OverflowTabs with the counted "+N more" menu.
- **Don't:** tabs wrapping to a second row, or silent truncation.

### Data table

**Recipe:** header — chrome bg, mono 10.5 caps tracking .1em, weight 500, tertiary ink, `position: sticky; top: 0` while scrolling · rows ≥40px (padding 10×14), 1px dividers (gray-200 / gray-750), hover row → chrome bg · status cells use BaseBadge · numbers right-aligned, mono 12.5, tabular-nums.
**Bounded viewport (principle 28):** the table scrolls inside a `max-height` + `overflow: auto` container — **the page never grows without limit**; wide tables get a `min-width` and scroll horizontally inside the same container, never the page (principle 7). Below the fold line, state the total: "412 executions · latest 50 shown". Virtualize or paginate past ~200 rows.

- **Do:** bounded container + sticky header + stated total.
- **Don't:** render 5,000 rows into the page and let it scroll for minutes.

### Empty states

**Recipe:** dashed border-strong (gray-300 / gray-700), radius 8, centered, padding 34×20; 40px icon in a chrome circle (tertiary ink); title 14.5/650; **one line of purpose** (13, tertiary); **exactly one primary action**. The dashed border is the signature that distinguishes "nothing yet" from loading or failed (principle 15).

- **Do:** "No schedules yet — Schedules run this agent automatically on a cron cadence. [New schedule]"
- **Don't:** a blank region, or "No data" with no next action (principle 16).

### Failed states — the third member of the triad (#1926)

**Recipe:** the two shipped primitives are `components/LoadFailed.vue` (a failed
*fetch* — owns the surface where the list would be) and `components/InlineError.vue`
(a failed *verb* — sits next to the control that was pressed). Both name what
happened in user vocabulary, offer the next action, and keep the raw error
(status code, server message) behind a "Technical detail" disclosure
(principle 25). `LoadFailed` shares the centered footprint of the sibling
loading/empty blocks so nothing shifts when the state resolves (principle 4).

**The rule the audit found broken across ten surfaces:** an empty state may only
render once a fetch has **succeeded and returned zero**. `list.length === 0` is
not that condition — before the first response it means *loading*, and after a
rejection it means *failed*. Stores therefore expose a `hasLoaded` flag that
flips only on success (`stores/operatorQueue.js`, `stores/notifications.js`),
and components gate on `hasLoaded` + `error`, never on length alone. Failed must
never borrow the empty state's copy: "No templates found" on a network error
sends the user to create a template when the fix is to retry.

**Verb failures are never console-only.** A `console.error` is invisible — the
row snaps back to its server value and the verb looks like it simply did
nothing. Neither is `alert()` acceptable: it blocks the page and leaves no
record once dismissed. Use `InlineError`, and say what did NOT happen ("Nothing
was changed — try again").

**Watch `Promise.allSettled`:** a bulk helper built on it *resolves* even when
every item failed, so a `try/catch` around it reports success on a total
failure. Inspect the settlement and report the count that did not apply.

### Alerts & toasts

**Alert recipe:** tinted ground per token family (light token-100 bg + token-700 text; dark token-500/16% + token-300), radius 8, padding 12×14, 13.5, 16px stroke icon, bold lead sentence, plain-language body. Errors say what went wrong **and how to fix it** ("Execution failed — exit 137 (out of memory). Raise the agent's memory limit in Settings → Resources."). Warnings carry their action inline.
**Toast recipe:** surface bg + border-strong + shadow-lg, radius 8, padding 10×16. Toasts confirm **completed verbs** and include the fact you'd check next ("Schedule created — next run Mon 09:00 UTC"); auto-dismiss ~5s; **never used for errors** (principle 18) — errors persist until acknowledged. Enforced in `composables/useNotification.js` (#1926): a `type: 'error'` notification does not start the dismiss timer and stays until the user closes it, so every toast host renders a dismiss control for it. A failure that belongs next to a control should not be a toast at all — use `InlineError`/`LoadFailed` above.

## 6. Motion — the data-loading standard

The app has **two** data-loading treatments, decided by the surface (design session 2026-07; amended by the operator ruling of 2026-09-06, #2540): the **scanline beam + wipe-in reveal is the chart-loading motion** — `StackedBarChart`, `TrendLineChart`, the grid tile charts, metrics panels — and **every other first load is a skeleton placeholder** keyed on "no data yet": pages, panels, lists, message threads. Bespoke per-component spinners do not survive adoption; a skeleton follows the recipe below, never a hand-rolled one.

### Scanline beam — anatomy

- The loading zone renders its chart **track** at low opacity (~.5; dims to ~.15 once loaded).
- The **beam**: an ~18px vertical gradient halo (`transparent → focus-ring color → transparent`) with an ~1.5px glowing core line (status-info color, `box-shadow: 0 0 7px` glow) running full height.

### Phases

1. **Loading** — the beam sweeps the dimmed track left↔right: `1.5s ease-in-out infinite alternate`, travel −8% → 100%. Headline values show the em-dash (`—`).
2. **Arrival** — one final left→right pass: `550ms linear` (−8% → 102%, beam fades out over the last ~8%), while the chart content is **wiped in behind it** via a synchronized `clip-path: inset(0 100% 0 0) → inset(0 0 0 0)` animation of the same 550ms. Headline values flip from `—` to real numbers at reveal.
3. **Loaded** — chart at full opacity, track dimmed to ~.15, beam gone.

CSS-only: one beam element per zone + two keyframe animations. No JS animation loop.

**Shipped primitive (trinity-enterprise#245):** `src/frontend/src/components/ScanlineReveal.vue`
(phase rules in the pure, unit-tested `utils/scanlinePhase.js`). Wrap the zone, pass
`:loading` ("no data yet") and `:reveal` (false for an error/empty terminal — those snap,
never celebrate); swap loading/loaded content inside the SLOT of one persistent instance
(sibling `v-if` branches remount it and the reveal never plays); the consumer sizes the
zone. During the arrival pass the track is wiped OUT behind the beam (complementary
`clip-path`), so revealed pixels sit on their final background from the first frame — no
end-of-pass background snap. Theme via `--scan-core`/`--scan-track` overrides (the Grid
rides `--gv-*`). Reference adoption: the Grid `AgentTile.vue` chart zones and the
Executions info tile's chart zone (ent#449, via the chassis opt-in `owns-loading` — a tile
may own its LOADING face inside `InfoTile`'s slot; the chassis keeps `error`/`empty`). The
Workspace's stage, thread and briefing adopted it under #2163 and were moved back to
skeletons by #2540 — a conversation is not a chart. Adopting a further CHART surface = the
ent#253 pass; adopting it on a page, list or thread is a violation, and
`tests/unit/portalLoadingTreatment.spec.js` pins the importer set as an allowlist so a new
non-chart adoption fails CI (the two pre-ruling holdovers, `LibrarySkillsSection` and
`onboarding/FinishSetupCard`, are recorded on #1921's sweep and shrink that list as they
convert).

`content-class` (#2163) is the consumer's hook on the primitive's OWN content wrapper —
`.scan-content` is child-owned DOM and `:deep()` is forbidden here, so a zone whose
loaded content must FILL the zone (a full-height flex column, rather than be measured by
it) had no way to say so. Default `''`; sizing the ZONE stays the consumer's job either
way. Note `announce` puts `role="status"` on the zone ROOT, which is an implicit
aria-live region — never pass it for a zone wrapping content that keeps changing (a
transcript, a composer), or every update is re-announced in full.

### Skeleton placeholder — pages, panels, lists, threads (#2540)

The non-chart first load. Two sanctioned forms: `components/SkeletonLoader.vue` (generic
stacked rows / node placeholders — the Dashboard's) and a **content-shaped** placeholder
that mirrors the loaded surface's own footprint (the #2159 sidebar rows, `PortalAgentPage`'s
section blocks, the Workspace's `components/portal/PortalSkeleton.vue` — stage / thread /
briefing). Either way the recipe is:

- pulse blocks in the two chrome fills — `bg-gray-100 dark:bg-gray-800/60` for content,
  `bg-gray-200 dark:bg-gray-800` for heavier "heading" bars — with
  `animate-pulse motion-reduce:animate-none` (a static placeholder under reduced motion);
- `aria-busy="true"` on the placeholder root and ONE `sr-only` "Loading…" line;
- keyed on a **verdict** — `hasLoaded`, `state === 'loading'`, `!historyLoaded` — never a
  fetch-in-flight flag, and never a bare `<x>.loading` path (the #1927 ratchet counts that
  spelling as a bare gate, so `stage.loading` as a `v-if` is a regression even when the
  value is a verdict);
- the same footprint as the loaded state, owned by a wrapper BOTH faces sit inside
  (principle 4) — a stage-shaped placeholder draws the frame the common terminal takes
  (header, thread, composer);
- the placeholder is the FIRST arm of the branch chain, so no terminal arm can render under
  it (the ent#253 lesson holds for skeletons as much as for the beam).

### Rules (all mandatory)

- **Keyed off store state** (`loading → loaded`), **never a timer**. Cache hits resolve instantly and **skip the animation entirely** — no forced beam pass on instant data.
- **Loading means "no data yet", never "fetch in flight".** A surface that already has data is not loading.
- **First load animates; scheduled background refresh is invisible** — stale-while-revalidate: values swap in place, no skeleton re-flash, no spinner, no layout shift, no scroll reset. A refresh either looks deliberate (first load) or isn't noticed at all.
- **No layout shift** at any phase: the zone keeps one fixed footprint through loading → reveal → loaded (principle 4).
- **`prefers-reduced-motion: reduce`** → no beam, no wipe: static placeholder, instant reveal.
- **A failing refresh is honest** (principle 15): never present stale data as fresh — "Refresh failed — showing data from 10:42 · Retry".
- **A failed request never becomes a statement about the subject** (ent#253, the #1926 class one layer down). The three surfaces that pass swept — `MetricsPanel`, `DashboardPanel`, `ObservabilityPanel` — each answered a failed poll by *overwriting* their data with a synthetic "this agent has no metrics / no dashboard / the collector is unavailable", so one transient 502 rewrote a working agent as a misconfigured one and the real values were gone until the next success. The rule: on a refresh failure **keep the data, set an error, raise the stale banner**; the empty state stays reserved for a fetch that succeeded and returned nothing. A `catch` that assigns the data ref is the smell.
- **The gate is auditable, so audit before sweeping**: `scan-loading-gates.mjs` finds bare `v-if="loading"` gates, but a compliant-looking gate can still re-flash (the flag is set by the polled fetch) and a *bare-looking* one can be harmless (the flag is only ever set on mount). ent#253's inventory of all 31 interval-refreshed files — with the verdict and the evidence for each — is the audit comment on trinity-enterprise#253; extend it rather than re-deriving it. **Record consumer-reachability in the same pass**: `grep setInterval` finds code that polls, not surfaces a user can reach, and ent#253 found two of its three flagged panels (`MetricsPanel`, `ObservabilityPanel`) mounted by nothing at all — a defect worth fixing in code but not a live symptom, and the difference decides how loudly it is reported.
- The 16px in-flight spinner inside BaseButton is *action feedback*, not data loading — it is the one sanctioned spinner, and it lives only inside a pressed control.

## 7. UI Construction Principles

The behavioral half of the standard (the visual half is §1–6). Confirmed in the design session, 2026-07. Mechanically checkable principles are enforced by the validation tooling (§9); the rest are review criteria.

### A. Defaults & configuration

1. Every setting and field ships a working default; the zero-input path runs; defaults are safe for security.
2. Anything runtime-changeable belongs on a Settings surface; `.env` is bootstrap only.
3. Forms open pre-filled, never blank — the user edits a working proposal.

### B. Layout stability — no jumpy UI

4. Reserve space: loading, loaded, empty, and failed states share one footprint; nothing shifts when content arrives (#1266).
5. State changes preserve user context — scroll position, selection, focus, and expansion survive updates.
6. Dimensions never oscillate as async data trickles in — size to the stable state.
7. Panels flex to available width; no overlap at narrow widths; wide content scrolls in its own container, never the page.
8. Scrolling is axis-locked — one axis at a time.

### C. Density & progressive disclosure

9. Overflowing tabs collapse into a "More ▾" menu, re-measured on resize (OverflowTabs, #1114) — adopt everywhere.
10. The same rule holds for any overflowable strip (toolbars, chip rows, action rows): a counted overflow menu, never truncation.
11. One primary action per view; every other action demotes to secondary, ghost, or the overflow menu.

### D. Data loading & refresh

12. Two loading treatments, decided by the surface (design session 2026-07; amended 2026-09-06, #2540): the scanline beam + wipe-in reveal on **chart** surfaces only; a skeleton placeholder keyed on "no data yet" on every other first load — pages, panels, lists, threads. No bespoke spinners; no scanline over a non-chart surface.
13. First load animates; background refresh is invisible — stale-while-revalidate, in-place swap.
14. Loading means "no data yet", never "fetch in flight"; cache hits skip the animation; reduced motion is honored.

### E. Honest state & feedback

15. Loading ≠ empty ≠ failed ≠ partial — visually distinct, never optimistic success (#1266).
16. No dead ends: every empty state names the surface's purpose and the next action.
17. Validation errors are named and actionable, with an example of valid input; pre-validate client-side wherever the rule is known (#925).
18. Every verb acknowledges within ~100ms (pressed/in-flight state) and confirms completion — a toast for completed verbs, never for errors.
19. Destructive actions use a named verb, restate the consequence, and focus the safe action first.
20. Color via semantic tokens only; shared primitives; both themes first-class (#1430).
21. Frontend invariants hold: domain-scoped stores, a single API client; loading flags live in stores, not components.
22. Times carry honest context — relative for recency, absolute with timezone on hover or detail (see `docs/TIMEZONE_HANDLING.md`).
23. Keyboard baseline: visible focus everywhere, Esc closes overlays, modals trap focus, tab order matches visual order. Handlers are armed at mount, above every `await` — never behind fetched data. A surface that renders as interactive must already be interactive; this is the input-side twin of #15's honest-state rule for output. (#2200: the Dashboard `/` hotkey registered after `await Promise.allSettled([...5 fetches])` while the fleet painted on `fetchAgents()` alone, so keystrokes were silently dropped for an unbounded window. Guarded by `src/frontend/tests/unit/mountListenerOrdering.spec.js`.)
24. Identity is encoded in form as well as color — classes and states are distinguishable by shape or icon, never by hue alone.
25. Meaningful errors everywhere: every failure surface says what happened, what it means, and what to do — in user vocabulary; stack traces and HTTP codes go behind a details disclosure, never the headline.
26. Expectation setting: non-instant actions state what will happen and roughly how long; multi-step operations show staged progress; say where the result will appear.
27. Post-action next steps: after a completed action, offer the natural next move via static per-flow sequencing. (An adaptive, system-wide suggestion tier is deliberately deferred.)
28. Unbounded data is contained: any surface that can render a large or unbounded set (tables, logs, activity streams, execution lists) gets a bounded viewport — max-height with internal scroll, pagination, or virtualization — and never grows the page itself without limit. Sticky headers stay visible while scrolling; the surface states the total ("412 executions · latest 50 shown") so what's beyond the fold is known, not hidden. Companion of 7 (horizontal) and 4 (stable footprint).

## 8. Do / don't — the shape of a violation

The recurring failure modes, in one place:

| Don't | Do |
|---|---|
| `bg-emerald-500` | `bg-status-success-500` |
| Hand-rolled `<button class="…">` | `BaseButton` |
| `dark:text-gray-500` on meta text | `dark:text-gray-300` (secondary) or `-400` (tertiary) |
| A new skeleton/spinner for a loading chart | The scanline primitive, keyed off store state |
| A scanline beam over a page, list or thread | A skeleton placeholder keyed on `hasLoaded` (§6, #2540) |
| A "Loading…" line or an `animate-spin` on a page | The skeleton recipe (§6), keyed on a verdict |
| Skeleton re-flash on a 30s poll | Stale-while-revalidate, in-place swap |
| Tabs wrapping to two rows | OverflowTabs with "+N more" |
| A table that grows the page unbounded | Bounded viewport + sticky header + stated total |
| Red border as the whole error | Named error + fix + example |
| "Are you sure? OK" | Named verb + consequence + safe-action focus |

## 9. Enforcement

Four layers keep the standard true:

1. **The token ratchet.** `src/frontend/scripts/check-design-tokens.mjs` (`npm run check:tokens`, wired into the frontend build workflow) validates token→palette aliasing and resolvable references, extended with a **raw-color ratchet**: a checked-in baseline (`raw-color-baseline.json`, per-file raw-palette counts) that **only shrinks**. CI fails when any file exceeds its baseline; migrating a file lowers its entry. New code starts at a baseline of zero raw colors — there is no legal way to add one.
2. **The component reference page.** The living catalog renders every primitive and pattern from this document in both themes. It is re-rendered from the real primitives once they exist, so page, code, and doc cannot drift apart. When reviewing UI, compare against the page.
3. **The review checklist.** Every frontend PR is checked against the builder contract's self-check (the condensed companion of this document); the validation playbook sweeps the frontend for the mechanically checkable principles — raw palette, hand-rolled primitives, off-scale spacing, unbounded surfaces — always reading rules from this document and the token file, never from a hardcoded copy. Findings are named and actionable: file, violation, suggested token or primitive.
4. **The loading-gate ratchet (#1927).** `src/frontend/scripts/scan-loading-gates.mjs` counts, per `.vue` file, the bare `v-if`/`v-else-if` gates whose whole expression is a loading flag (`loading`, `loading.queue`, `executionsLoading`, …) — the p13/p14 violation class (#1634, #1926, #1927): such a gate swaps rendered data for a spinner on every background poll. `loading-gate-baseline.json` freezes today's per-file counts and `tests/unit/loadingGateRatchet.spec.js` (part of `npm run test:unit`) fails when any file's count grows, when a new file gains one, or when an entry is stale (a fixed file must lower its entry — `node scripts/scan-loading-gates.mjs src --baseline loading-gate-baseline.json`). The sanctioned shape is `utils/loadingState.js::viewState({ loading, hasLoaded, error, count })` → `loading | failed | empty | ready` + `stale`, with the stale-refresh banner (`InlineError retryable`, copy from `staleBannerMessage`) rendered as a **sibling before** the chain. Freeze, then pay down: the baseline is the sweep's worklist, not its permission slip.
