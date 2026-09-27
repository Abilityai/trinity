# First-Run Overlay (trinity-enterprise#581) — formerly the Onboarding Wizard (ent#52)

**Status:** In progress (ent#581; credential steps content per ent#582). Replaces
the ent#52 wizard and the inline first-run card ladder.

## Problem

Five first-run surfaces from five issues rendered inline in the Dashboard flow —
the hardening guide (#2380), the front desk (ent#319), the activation checklist
(ent#238), the finish-setup card (#2381 / ent#437) and the ent#52 wizard. #2380
stopped them stacking (`.onboarding-stack > * ~ *`), but each still reflowed the
page and dismissing one revealed the next: on a fresh droplet, four dismissals
deep with the dashboard jumping between each.

## Shape

One **blocking overlay**, teleported to `<body>`, over a Dashboard that never
reflows. A 940px two-pane card — rail (the actual step set, progress, "Finish
later") and content (step body that scrolls; a footer with Back · Skip · Continue).
Fixed panel height, so it never resizes between steps. Under 640px the rail
collapses to a progress strip and "Finish later" moves into the footer.

```
Login (or the /setup claim, ent#580 — which now signs straight in)
  → Dashboard mounts <FirstRunOverlay v-model:open>
      waits for: flags (fetched OK) + /api/users/me + /api/onboarding/first-run
      opens when a state-derived step applies and is not skipped, or ?onboarding=1
        welcome  — what will be configured, about how long (constellation + manifest)
        secure   — domain field; Cloudflare Tunnel guidance behind <details>   (#2380)
        email    — sign-in email, fallback only                                 (#2381)
        claude   — REQUIRED; Continue disabled until it completes                (ent#582)
        keys     — optional credentials slot, rides along                        (ent#582)
        agent    — Show me / Make me one (real CreateAgentModal) / Bring mine     (ent#319, ent#52)
        sharing  — usage-sharing consent, one step in the sequence              (ent#437)
        done     — what is left and where; "Done" follows a door's destination
```

## The registry — `components/onboarding/firstRunSteps.js`

Pure module (vitest runs `environment: 'node'`, no mount harness), so every
decision is spec-reachable. Each step has `eligible` (who it is for) and
`pending` (still something to do); `applies = eligible && pending`.

| Step | eligible | pending | Opens the overlay? |
|------|----------|---------|--------------------|
| secure | admin ∧ `hardening_guide_eligible` (provenance) | posture ≠ `https-domain` | yes |
| email | admin | no email bound | yes |
| claude | admin | `!claude_auth_configured` | yes (required) |
| keys | admin | always | **no — rides along** |
| agent | everyone | server `first_run` | yes |
| sharing | admin ∧ not hard-disabled | not enabled ∧ not dismissed | yes |

- **Auto-open** (`isFirstRunOverlayVisible`): required terms `flagsLoaded`
  (the read *succeeded* — `sessions.featureFlagsFailed` exists so a failed fetch
  never reads as "Claude is not configured"), `profileVerified`, `firstRunLoaded`;
  then `forced` → open; `closed` → shut; else any applicable non-ride-along step
  that is not skipped. An established fleet never sees it.
- **Snapshot**: the chassis fixes the rail's membership when it opens
  (`stepsForSession`: applicable steps, or every *eligible* step when forced), so
  a step finished mid-session keeps its row and shows a check.
- **Completion is derived** (`stepState`): done when the predicate went false
  after a refresh, or the step emitted `complete` this session (the only way
  `keys` reads done). Persisted per browser: `trinity_first_run_closed` ("Finish
  later" / "Done") and `trinity_first_run_skipped` (JSON array). A dismissal
  given to a replaced card (`trinity_hardening_guide_dismissed`,
  `trinity-admin-email-nudge-dismissed`, `trinity_front_desk_dismissed`,
  `trinity_onboarding_dismissed_v1`, an unexpired telemetry snooze) reads as a
  skip of the step that absorbed it.
- **Blocking** (`canContinue`): only `claude` — Continue goes live when the step
  emitted `complete` or no longer applies after the refresh. Continue past a
  still-pending optional step records it as skipped.

## Chassis — `FirstRunOverlay.vue`

- `ctx` (read-only prop for every step): `flagsLoaded`, `profileVerified`,
  `firstRunLoaded`, `isAdmin`, `marketplaceInstall`, `tlsPosture`, `hasEmail`,
  `userEmail`, `claudeAuthConfigured`, `telemetryEnabled`,
  `telemetryHardDisabled`, `telemetryDismissed`, `telemetryFirstValue`,
  `firstRun`, `demoAgent`, `forced`, `closed`, `skipped`.
- A step emits `complete` (optionally `{ next, label }` — a route "Done" follows);
  the chassis records it and refreshes every store `ctx` derives from:
  `sessions.loadFeatureFlags(true)`, `auth.fetchUserProfile()`,
  `firstRun.fetchState(true)`.
- Focus trap / scroll lock / keydown lifted from the ent#52 wizard. `Esc` and
  "Finish later" raise the same `ConfirmDialog` (safe action focused; the
  consequence names Settings → Integrations when Claude is still missing). No X,
  no backdrop close. A modal a step opens on top (the real `CreateAgentModal`)
  owns the keyboard.
- `?onboarding=1` and **Settings → General → Re-run setup**
  (`components/settings/FirstRunRerunPanel.vue`) open it over every eligible
  step; closing a forced session strips the param.
- Product funnel events (ent#184, unchanged allow-list): `setup_started` on open,
  `setup_step_credential` on reaching `claude`, `setup_step_create` on a purpose
  pick, `setup_completed` on Done, `setup_dismissed` on Finish later.

## Visual system

`TrinityMark.vue` (the one copy of the logo, shared with `/setup`),
`FirstRunConstellation.vue` (the `/setup` motif, token-derived; nodes = the four
capabilities, lit = done, frozen under `prefers-reduced-motion`),
`schematics/Schematic.vue` (six inline SVGs on `currentColor`, real
`aria-label`s), `FirstRunStepHeader.vue` (owns `id="first-run-title"`), a
`theme()`-built aurora. Semantic tokens only; the new files carry zero raw
non-gray classes and zero hex literals.

## What stayed

`ActivationChecklist.vue` stays inline on the Dashboard (a progress marker over
days, not a gate). `hardeningGuide.js` and `telemetryConsent.js` survive — their
copy is what the `secure` and `sharing` steps speak. The Dashboard empty-state
"Get started" now opens the chassis `CreateAgentModal` directly.

## Tests

- `src/frontend/tests/unit/firstRunSteps.spec.js` — registry, visibility gating,
  established-fleet-hidden, forced reopen, derived completion, skip persistence
  (incl. legacy keys), no duplicate asks, structure.
- `hardeningGuide.spec.js` (copy + `StepSecure.vue` structure),
  `telemetryConsent.spec.js` (copy + `StepSharing.vue`), `firstRunFrontDesk.spec.js`
  (the store).
- e2e: `dashboard-list-view.spec.js` (`?onboarding=1` survives the `/agents`
  redirect and opens the overlay); `auth.setup.js` pre-seeds
  `trinity_first_run_closed`.
