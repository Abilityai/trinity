# First-Run Onboarding Wizard (trinity-enterprise#52)

**Status:** Phase 1 shipped (guided first-agent deploy). Ambient layers tracked as follow-ups.

## Problem

A self-hoster who gets Trinity running and opens the web UI had **no guided path
to first value**. The only in-app onboarding artifact (`OnboardingChecklist.vue`
+ `useOnboarding.js`) was orphaned dead code modeled on the **removed** Process
Engine — mounted nowhere, linking to routes (`/processes/wizard`) that no longer
exist.

The user tension to design around: people who "just want to get things done"
bounce off instructions, but they still need to grasp basic concepts (what an
agent is, chat) to get value. So: **teach by doing, not by reading.**

## Design principles

- **Not a click-through tour.** No mandatory tooltip walkthrough — that's the
  exact thing the "don't make me read" user rage-quits.
- **One question → a running agent.** "What do you want to do?" maps an intent
  to a starter template and deploys a *tailored* first agent, not a blind sample.
- **Soft-guide the one hard gate.** Trinity agents can't think without Claude
  auth, but front-loading every credential recreates the "wall of setup."
  Surface the Claude-auth gate as a non-blocking hint; defer everything else
  (e.g. the GitHub PAT) to just-in-time.
- **Never nags.** Auto-opens once for a fresh, empty install; dismissal is
  remembered.

## Flow

The wizard **drives the real UI** — it does not reimplement agent creation. It
guides the user into the actual create form, then to the one credential the new
agent still needs.

```
Fresh install → admin account (existing SetupPassword.vue) → login
   → Dashboard, zero agents
       → OnboardingWizard auto-opens (once)
           1. intro      — one question, "what do you want it to do?"
                           (a purpose card → prefills a starter template)
           2. create     — opens the REAL CreateAgentModal, template
                           preselected; user creates the agent (the right
                           buttons, not a clone)
           3. credential — "connect a Claude subscription so it can think"
                           → Settings → Integrations → Claude Subscriptions
                           (or "Open chat with <agent>")
```

Dismiss at any step → remembered in `localStorage['trinity_onboarding_dismissed_v1']`.
Re-openable any time from the Dashboard empty state ("Get started") or via
`/?onboarding=1` (re-run / QA preview, ignores the dismissed flag and fleet size).

## Purpose → template mapping

The intent cards map to the real local starter templates shipped in
`config/agent-templates/`. Each card's template existence is verified against
`GET /api/templates` on mount; a missing template falls back to a blank Claude
Code agent so deploy never 404s.

| Purpose card                  | Template id   | Default name |
|-------------------------------|---------------|--------------|
| Research a market or topic    | `local:scout` | `scout`      |
| Advise on strategy            | `local:sage`  | `sage`       |
| Write content & reports       | `local:scribe`| `scribe`     |
| Start from scratch            | `''` (blank)  | `assistant`  |

## Implementation

**Frontend**
- `components/OnboardingWizard.vue` — the guidance overlay. Step `create`
  delegates to the real `CreateAgentModal` (`:initial-template` prefilled,
  `@created`); step `credential` routes to Settings → Integrations. It never
  calls `createAgent` itself.
- `views/Dashboard.vue` — mounts the wizard; auto-opens via
  `maybeAutoOpenOnboarding()` (gated on `!isFleetLoading && agents.length === 0 &&
  !dismissed`, or forced by `?onboarding=1`); empty-state "Get started" re-opens it.
- `stores/sessions.js` — caches `claudeAuthConfigured` from feature-flags
  (decides whether the credential step nudges to connect, or confirms + offers chat).

**Backend**
- `routers/settings.py` `GET /api/settings/feature-flags` gains
  `claude_auth_configured` — `bool(anthropic_api_key)` (DB or env) **or** any
  registered subscription. A boolean only; never the key itself.

**Removed**
- Dead `components/OnboardingChecklist.vue` + `composables/useOnboarding.js`
  (Process-Engine-based, orphaned).

## Follow-ups (separate build issues, from trinity-enterprise#52)

- Phase 1 first-run setup: add a **mandatory** Claude-auth step to the setup
  wizard (currently the wizard only *hints*; nothing blocks).
- Ambient auto-completing checklist on real concepts (created → chatted →
  scheduled → shared), non-blocking, dismissible.
- Contextual empty-state guidance on other key views.
- Just-in-time GitHub PAT prompt (only when a GitHub-template agent needs it).
