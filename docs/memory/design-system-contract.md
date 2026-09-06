# Trinity Frontend Builder Contract

Load this before writing any code under `src/frontend/`. It is the condensed, binding form of `docs/memory/design-system.md` — read that for recipes and rationale; on conflict, the full doc wins. Tokens live in `src/frontend/tailwind.config.js`.

## Color — token-only

- Use semantic tokens for every color: `status-*` (result of an event), `state-*` (operating mode), `brand-*` (third-party identity), `accent-*` (decoration), `action-*` (interaction). Everything else is gray.
- Never write a raw Tailwind palette class (`bg-green-500`, `text-red-600`) or a hex. CI ratchets raw-color counts down; new code must be at zero.
- Workhorse shades: 500/600 solids; 100 tinted grounds (light); 700 text-on-tint (light); 500/16% grounds + 300 text (dark); 400–500 solid accents (dark).
- `gray-750` (#2a303c) is the custom dark chrome/border shade — use it, don't approximate it.

## Themes — both first-class

- `darkMode: 'class'`. Build and verify light AND dark for everything. No per-theme hardcoded color inside a component — theming lives in the token layer.
- Surfaces: ground gray-50/gray-900 · surface white/gray-800 · chrome gray-100/gray-750 · field white/gray-900 · border gray-200/gray-750 · border-strong gray-300/gray-700.
- Dark ink ladder: primary gray-100 · secondary gray-300 · tertiary gray-400. **gray-500 is the floor** — disabled/decoration only, never meta text (keeps ≥4.5:1 AA on gray-800).
- Light ink: primary gray-900 · secondary gray-600 · tertiary gray-500.
- Interactive accent: action-primary-600 light / 500 dark; hover 700 / 400; focus ring 500@40% / 400@42%.

## Primitives first

- Compose `BaseButton`, `BaseInput`, `BaseSelect`, `BaseToggle`, `BaseTextarea`, `BaseBadge`, `BaseCard`, the modal shell (`ConfirmDialog`), `OverflowTabs`, the bounded data table, and the failed-state pair `LoadFailed` (failed fetch) / `InlineError` (failed verb). Never hand-roll a lookalike — identical pixels from a class string is still a defect.
- BaseButton: 4 variants (primary/secondary/danger/ghost) × 2 sizes (md 13.5 pad 7×14 · sm 12.5 pad 4×10), radius 6px. Disabled = opacity .45. In-flight = 16px spinner + progressive label. Focus ring on all variants.
- BaseInput/BaseSelect: field bg, border-strong, radius 6px, pad 8×11; focus = accent border + 3px ring; errors name the problem, the fix, and an example — never a bare red border.
- BaseTextarea: min-height 84px, `resize: vertical` only, mono variant for prompts/config, same focus/error as inputs.
- BaseBadge: pill, 11.5/550; light token-100 bg + token-700 text; dark token-500/16% + token-300; one fact per badge.
- BaseCard: surface bg, 1px border, radius 8px, padding 16, shadow-sm — the only surface recipe.
- Modal: overlay gray-950/55%, 400px card radius 10 shadow-lg; Esc + click-outside close; focus trapped, **initial focus on the safe action**; destructive confirms restate the consequence.
- Tabs: `OverflowTabs` everywhere — counted "+N more" overflow, re-measured on resize; never wrap or truncate.
- Tables: sticky mono-caps header on chrome, bounded viewport (max-height + internal scroll), tabular-nums right-aligned numbers, stated total ("412 · latest 50 shown"); virtualize/paginate past ~200 rows.

## Type, spacing, radius

- Six type sizes only: 24/700 page title · 18/650 section · 14/550 label · 14/400 body · 12.5 meta · 11 mono caps overline. System-ui stack; `tabular-nums` on all data numbers; mono for machine text.
- 4px grid, five steps: 4 tight · 8 related · 12 grouped · 16 card padding · 24 section.
- Radii: 6px controls · 8–10px surfaces · pills only for badges/toggles/avatars.

## Data loading & motion

- Two loading treatments, decided by the surface (#2540). **Charts** — the scanline beam (gradient halo + ~1.5px glowing core) sweeping a dimmed track at 1.5s ease-in-out alternate; on arrival, one 550ms linear pass wipes the content in behind it via `clip-path`; values flip from `—`. CSS-only. **Use `ScanlineReveal.vue`** (ent#245) — one persistent instance, content swapped in its slot, `:reveal="false"` for error/empty terminals. **Everything else** — pages, panels, lists, message threads — a skeleton placeholder: pulse blocks in the chrome fills, `animate-pulse motion-reduce:animate-none`, `aria-busy` + one `sr-only` line, the loaded surface's own footprint (`SkeletonLoader.vue`, or content-shaped like `portal/PortalSkeleton.vue`), the first arm of the branch chain.
- Key the animation off store state (`loading → loaded`), never a timer. Cache hits skip it entirely.
- Loading means "no data yet", never "fetch in flight".
- First load animates; **scheduled background refresh is invisible**: stale-while-revalidate, in-place value swap, no skeleton re-flash, no spinner, no layout shift, no scroll reset.
- A **failed** refresh keeps the data on screen and raises the stale banner (ent#253). Never overwrite the data with a synthetic empty payload in a `catch` — that turns "the request failed" into "this agent has none", which is a different and much worse claim.
- `prefers-reduced-motion: reduce` → static placeholder, instant reveal.
- No bespoke spinners, and no scanline over a non-chart surface; a skeleton follows the recipe above. The only sanctioned spinner is the 16px in-flight indicator inside BaseButton.

## Principles — one line each

Defaults:
1. Every field ships a working default; the zero-input path runs; defaults are security-safe.
2. Runtime-changeable settings live on a Settings surface; `.env` is bootstrap only.
3. Forms open pre-filled — the user edits a working proposal, never a blank.

Layout stability:
4. Loading/loaded/empty/failed share one footprint; nothing shifts on arrival.
5. Updates preserve scroll, selection, focus, and expansion.
6. Dimensions never oscillate as async data trickles in — size to the stable state.
7. Panels flex to width; no overlap when narrow; wide content scrolls in its own container, never the page.
8. Scrolling is axis-locked — one axis at a time.

Density:
9. Overflowing tabs collapse into a counted "More ▾" menu (OverflowTabs) — everywhere.
10. Any overflowable strip (toolbars, chips, actions) gets a counted overflow menu, not truncation.
11. One primary action per view; the rest demote to secondary/ghost/overflow.

Data loading:
12. Scanline on chart surfaces only; a skeleton placeholder keyed on "no data yet" everywhere else; no bespoke spinners (#2540).
13. First load animates; background refresh is invisible.
14. Loading = "no data yet"; cache hits skip animation; reduced motion honored.

Honest state:
15. Loading ≠ empty ≠ failed ≠ partial — visually distinct, never optimistic success. An empty state requires a fetch that **succeeded and returned zero** (`store.hasLoaded`), never `list.length === 0`; a failed fetch renders `LoadFailed`, never the empty copy (#1926).
16. No dead ends: empty states name the purpose and the next action.
17. Validation errors are named, actionable, with an example; pre-validate client-side where known.
18. Verbs acknowledge in ~100ms and confirm completion; toasts for completed verbs, never errors. A failed verb surfaces an `InlineError` next to its control and persists until dismissed — never `console.error` alone, never `alert()` (#1926).
19. Destructive actions: named verb, restated consequence, safe action focused first.
22. Times are honest: relative for recency, absolute + timezone on hover/detail.
23. Keyboard baseline: visible focus, Esc closes overlays, modal focus trap, tab order = visual order — and handlers are armed at mount, above every `await`, never behind fetched data. A surface that renders as interactive must already be interactive: the input-side twin of #15 (#2200).
24. Encode identity in shape/icon as well as color — never hue alone.
25. Errors say what happened, what it means, what to do — user vocabulary; codes/traces behind a disclosure.
26. Set expectations: non-instant actions say what happens, how long, and where the result appears.
27. After a completed action, offer the natural next step.
28. Unbounded data is contained: bounded viewport, sticky header, stated total — the page never grows without limit.

Consistency:
20. Tokens only; primitives only; both themes always.
21. Domain-scoped stores; single API client; loading flags live in stores, not components.

## PR self-check

Before requesting review, verify:

- [ ] Zero raw palette classes or hexes — semantic tokens only (`npm run check:tokens` passes; baseline not grown)
- [ ] Every button/input/select/toggle/textarea/badge/card/modal/tab/table is a Base* primitive, not hand-rolled
- [ ] Verified in light AND dark; dark meta text is gray-300/400, never gray-500
- [ ] Spacing on the 4px grid; radii 6px controls / 8–10px surfaces; type within the six-size scale
- [ ] Loading/empty/failed states all exist, visually distinct, sharing one footprint — no layout shift on arrival; the empty branch gates on a succeeded fetch (`hasLoaded`), not on list length
- [ ] Every action failure has a user-visible home (`InlineError` near the control); no `console.error`-only catch, no `alert()`, and `Promise.allSettled` bulk helpers report their rejected count
- [ ] Data loading uses the scanline primitive on chart surfaces and a skeleton placeholder on every other first load, keyed off store state; background refresh is invisible (no re-flash, no scroll reset) — no bare `v-if="loading"` gate on a data surface: gate on "no data yet" (`utils/loadingState.js::viewState`), stale refresh = sibling `InlineError` banner; `tests/unit/loadingGateRatchet.spec.js` fails if a file's bare-gate count grows (#1927)
- [ ] `prefers-reduced-motion` handled on any animation touched
- [ ] Empty states name purpose + one next action; errors name problem + fix + example
- [ ] Keyboard pass: visible focus, Esc, focus trap, logical tab order
- [ ] Unbounded sets bounded (internal scroll/pagination/virtualization) with the total stated
- [ ] One primary action on the view; destructive flows restate consequence with safe-action focus
- [ ] Numbers use `tabular-nums`; times show relative + absolute-on-hover
