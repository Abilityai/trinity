# Mobile Admin PWA (MOB-001)

Standalone mobile-friendly admin page at `/m` for managing Trinity agents from a phone. Installable as a Progressive Web App via "Add to Home Screen".

## Architecture

```
Browser (/m)
├── MobileAdmin.vue (self-contained SPA)
│   ├── Inline admin login (no redirect)
│   ├── Agents tab → GET /api/ops/fleet/status
│   │                GET /api/agents/autonomy-status (merged into agent data)
│   │                GET /api/agents/execution-stats?include_7d=true
│   │                POST /api/agents/{name}/start|stop
│   │                PUT /api/agents/{name}/autonomy
│   │                POST /api/agents/{name}/task (chat)
│   │                GET /api/agents/{name}/chat/sessions
│   │                GET /api/agents/{name}/chat/sessions/{id}
│   │                GET /api/agents/{name}/executions/{id}
│   │                GET /api/agents/{name}/logs
│   ├── Ops tab → GET /api/operator-queue
│   │             POST /api/operator-queue/{id}/respond
│   │             GET /api/notifications
│   │             POST /api/notifications/{id}/acknowledge
│   └── System tab → GET /api/ops/fleet/status
│                     POST /api/ops/emergency-stop
│                     POST /api/ops/fleet/restart
│                     POST /api/ops/schedules/pause|resume
├── mobile-manifest.json (PWA manifest)
├── mobile-sw.js (service worker)
└── icons/ (192, 512, apple-touch)
```

## Key Design Decisions

- **Completely isolated from main UI**: No NavBar, no links to/from main navigation
- **Self-contained auth**: Inline admin password login, component manages its own auth state. Route uses `requiresAuth: false` and handles auth internally
- **No backend changes**: Reuses all existing REST APIs
- **Manual PWA** (no vite-plugin-pwa): Service worker + manifest injected dynamically in `onMounted`, cleaned up in `onUnmounted`. Follows Sparky PWA reference pattern
- **Dark-only**: Forces `dark` class on `<html>`, OLED-friendly `#111827` background
- **401 handling**: `main.js` axios interceptor skips redirect to `/login` when on `/m`
- **Restart All is a real upgrade path (#1860)**: `POST /api/ops/fleet/restart` routes each running agent through the canonical stop→`start_agent_internal` path, so it applies pending config drift and adopts a rebuilt base image (container ids change). A slow fleet can outlast the proxy timeout — the loop keeps running server-side and the outcome lands in the audit log (`fleet_restart`); a second tap while one is in flight gets 409 `fleet_restart_in_progress`. **#1919**: the response `summary` now carries `processed` + `stopped_early` (`"lease_lost_foreign"` when a concurrent caller took the lock mid-run and the loop stopped with a partial fleet — HTTP 200 no longer implies completion). The PWA toast reads only a generic message today and does not surface these fields — client-side follow-up

## Files

| File | Purpose |
|------|---------|
| `src/frontend/src/views/MobileAdmin.vue` | Complete mobile admin SPA (~1100 lines) |
| `src/frontend/src/utils/operatorQueue.js` | The ONE builder of the operator-queue respond payload + controls-kind switch + type labels, shared with the desktop store (#2370) |
| `src/frontend/src/router/index.js` | `/m` route (requiresAuth: false) |
| `src/frontend/src/main.js` | 401 interceptor exclusion for `/m` |
| `src/frontend/public/mobile-manifest.json` | PWA manifest (standalone, portrait, dark) |
| `src/frontend/public/mobile-sw.js` | Service worker (network-first, skip API) |
| `src/frontend/public/icons/trinity-mobile-*.png` | PWA icons (192, 512, apple-touch) |

## Tabs

### Agents Tab
- Agent cards with name, status dot, autonomy badge (AUTO), type badge
- Success rate progress bar per agent (green >=90%, yellow >=50%, red <50%) — uses same `/api/agents/execution-stats` endpoint as desktop timeline view
- Autonomy status fetched from `/api/agents/autonomy-status` and merged (fleet status API doesn't include it)
- Start/stop toggle button per agent
- Tap to expand: autonomy toggle (Auto/Manual), chat button, log viewer
- Chat overlay: full-screen chat with session management, async task polling
- Search/filter by name
- System agents filtered out

### Ops Tab
- Sub-tabs: Queue | Alerts
- Queue: operator queue items answered inline. **#2370:** controls switch on the item **type** (desktop parity) — an *approval* is select an option → restated consequence ("Sending **Deny** to <agent> — it reads this as your decision on its next run") → optional note → `Cancel` + `Send: <option>` (the safe action is first and focused on reveal, p19; nothing sends on one tap, and Enter in the note does not send); a *question* is a text answer + Send; an *alert* is `Got it`. The body comes from `utils/operatorQueue.js::buildQueueResponse` — the decision rides `response` (the option / the typed answer / `acknowledged`), a note rides `response_text` — the same builder the desktop store uses. Before #2370 every option tap POSTed a hard-coded `response: 'approved'` with the tapped option in `response_text` (a Deny was recorded as an approval, a typed answer became a note), the card answered on one tap with no note, and the type line read a nonexistent `request_type` (blank). On success the card is dropped locally before the refetch (`fetchQueue` swallows its own errors); a 5xx/transport failure renders an `InlineError` **inside the card** (p18) and keeps the selection + note for a retry; a 409/400 "no longer pending" refusal goes to the persistent page-level banner (the card is about to leave) with attribution-free copy. Per-card state (`selectedOptions`/`responseTexts`/`respondErrors`/`respondingItems`) is keyed by item id, pruned on every poll and wiped on logout — except for a card whose POST is still outstanding, which is never pruned: `respondingItems` is the in-flight guard `sendQueueResponse` checks, and the `limit: 100` window (ordered status → priority → created_at) is one a busy queue can push an item out of and back into, which would re-enable Send under a live POST and report a recorded answer as not recorded. `fetchQueue` is sequence-guarded on **every** exit — a superseded poll writes neither the list, nor `fetchError.queue`, nor `loading.queue` — so a slow poll that rejects cannot paint a stale-refresh banner over data a newer fetch just proved fresh. Pinned by `tests/unit/operatorQueueResponse.spec.js` + `e2e/mobile-admin-queue-respond.spec.js` (`@smoke`, captures the real POST body; fails on the pre-fix code with `captured POSTs: [{response:'approved',…}]`).
- Alerts: notifications with acknowledge button
- Badge counts on tab and sub-tabs

### System Tab
- Fleet health grid: Total / Running / Stopped / High Context
- Action buttons: Emergency Stop, Fleet Restart, Pause Schedules, Resume Schedules
- Confirmation dialog (bottom sheet pattern) for destructive actions

## Mobile UX

- `touch-action: manipulation` — removes 300ms tap delay
- `-webkit-tap-highlight-color: transparent` — no flash on tap
- `font-size: 16px` on all inputs — prevents iOS auto-zoom
- `env(safe-area-inset-*)` — notch/home indicator handling via explicit `top/bottom/left/right` positioning (not padding, which causes iOS PWA touch coordinate offset)
- `overscroll-behavior: none` — prevents iOS rubber-band bounce
- `visualViewport` API — hides tab bar when keyboard opens
- Pull-to-refresh via touch event handlers
- 15-second auto-polling per active tab. **Background refresh is invisible (#1927, design-system p13/p14):** each dataset (agents / queue / notifications / fleet) carries `loading.*` (fetch in flight), `hasLoaded.*` (first SUCCEEDED fetch) and `fetchError.*`; the templates gate on `utils/loadingState.js::viewState(...)` — "Loading…" only before the first data, a failed first fetch renders `LoadFailed` (never "No agents found" / "No pending items"), a failed poll with data on screen renders a sibling `InlineError` stale banner ("Couldn't refresh … — showing data from HH:MM", Retry/Dismiss) and keeps the rows. `fetchAgents` uses `Promise.allSettled` (fleet + autonomy required, execution stats decorative) and the response-shape normalizer `listFrom` — `/api/operator-queue` returns `{items, count}` and `/api/agents/execution-stats` returns `{agents}`, both previously parsed as arrays (a TypeError on every poll; the Queue sub-tab had always read "No pending items"). `fetchAgents` also marks the fleet summary loaded, since it writes it too.

## PWA

- **Manifest**: standalone display, portrait orientation, start_url `/m`, shortcuts to Agents/Ops tabs
- **Service Worker**: Network-first strategy, caches static assets on success, falls back to cache on network failure. Skips `/api` and `/ws` entirely. `skipWaiting()` + `clients.claim()` for immediate activation
- **iOS**: `apple-mobile-web-app-capable`, `black-translucent` status bar, 180px touch icon
- **Install**: Dynamic injection in `onMounted` — manifest link, meta tags, SW registration. All cleaned up in `onUnmounted` to avoid polluting main UI
