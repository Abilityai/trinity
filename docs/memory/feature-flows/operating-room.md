# Operations (OPS-001)

> **Status**: Implemented (Phases 1-4)
> **Updated 2026-09-25 (trinity-enterprise#611, PR B)**: agents RAISE asks natively — `POST /api/agents/{name}/operator-queue` and MCP `ask_operator` (self-acting) into `ask_service.raise_ask`: named 422s, replay of the first receipt, a 15-minute deadline floor, a re-ask linked to the agent's own expired ask (`supersedes_expired`, 422 `reask_requires_link` without it), role addressing, an atomic per-agent depth cap. The row is outside the file contract; the poller skips a file entry re-using a native id, counts only what it inserted, and logs the file channel's deprecation. The prompt leads with `ask_operator`. See [Raising an ask](#raising-an-ask-trinity-enterprise611-pr-b).
> **Updated 2026-09-25 (trinity-enterprise#611, PR A)**: how an ask ENDS (OPS-001-ENDINGS). Every ending — the operator answer, the Workspace answer, single cancel, bulk cancel, expiry — goes through one sink, `services/ask_service.py` (compare-and-set → one audit row per transition → one thin WS trigger → ending observers). An endings ledger on the row (`disposition` answered|cancelled|expired, `disposed_at`, `disposed_by` person|timeout, `disposed_by_email`, `disposition_reason`, `batch_id`; migration `operator_queue_ask_object` / Alembic `0076`, which also adds the six agent-raised-ask columns PR B writes). Only a person ends an ask (403 `person_required` for agent/system/other keys). An answer after `expires_at` is 409 `expired`. The ent#329 wake fires on ANY ending (new trigger `operator_ending` for cancel/expiry). An agent reads its own ask back by `request_id` (`GET /api/agents/{name}/operator-queue/{request_id}`, MCP `get_my_ask`), and every composed turn lists the endings of the last 24 h. Surfaces show who and when. See "Endings" below.
> **Updated 2026-09-23 (#2915)**: the file sync tells the truth (OPS-001-HONEST). Eight nullable columns record what the poller last established about the agent's file entry (`sync_state` / `sync_detail` / `sync_updated_at` / `last_confirmed_at`), whether an answer reached it (`delivery_state` / `delivery_detail` / `delivery_updated_at`) and whether the human answered a diverged item knowingly (`divergence_acknowledged_at`); the write-back delivers only into a matching, still-pending entry, re-reads before writing and writes with `if_match` (the agent server's `PUT /api/files` is atomic and answers 412); `POST …/respond` and the portal answer return **409 `item_diverged`** unless `acknowledge_divergence` is set; every list/get carries server-computed `aging` / `aged_since` and the list carries `undelivered_count` / `closed_by_filer_count`; one `operator_queue_sync` WS trigger per cycle; audit rows for the accountability transitions only. See "Sync honesty" below.
> **Updated 2026-06-11 (#1017)**: "Clear All" -- per-tab bulk clear on the operator tabs. Two new endpoints (`POST /api/operator-queue/bulk-cancel`, `POST /api/operator-queue/clear-resolved`), a new `cleared_at` hide column (clear-resolved hides, never deletes), a new WS event (`operator_queue_cleared`), a respond-race 409, and terminal-status (cancelled/expired) write-back to agent queue files.
> **Updated 2026-06-09 (#1109)**: Frontend IA refactor -- "Operating Room" renamed to **Operations** and extended from 3 to **5 tabs** (added Health + Executions). View `OperatingRoom.vue` -> `Operations.vue`, route `/operating-room` -> `/operations`. NavBar "Health"/"Ops"/"Executions" links collapsed into one "Operations" link. Legacy routes redirect.
> **Requirements**: [OPERATOR_QUEUE_OPERATING_ROOM.md](../../requirements/OPERATOR_QUEUE_OPERATING_ROOM.md)
> **Tests**: `tests/test_operator_queue.py`, `tests/test_ops_clear_all.py` (#1017)

---

## Overview

Operations is the unified command center for fleet operator-facing surfaces: agent-to-operator communication (the operator queue + notifications) plus fleet Health monitoring and the fleet Executions list. Agents communicate through a standardized file-based protocol (`~/.trinity/operator-queue.json`), which the platform syncs to a database and presents as actionable cards. The page is a single **5-tab** interface (`Operations.vue`).

The five tabs cover:
- **Needs Response** -- Operator queue items requiring action (approvals, questions, alerts)
- **Notifications** -- Agent notifications with filtering, bulk actions, and acknowledgement (formerly the standalone Events page)
- **Health** (admin-only) -- Fleet monitoring, rendered by `MonitoringPanel.vue` (extracted from the deleted `views/Monitoring.vue`). Tab is admin-gated; see below.
- **Executions** -- Fleet execution list, rendered by `ExecutionsPanel.vue` (extracted from the deleted `views/Executions.vue`). Per-execution detail route `/agents/:name/executions/:executionId` (`ExecutionDetail.vue`) is unchanged.
- **Resolved** -- Completed operator queue items

`VALID_TABS = ['needs-response', 'notifications', 'health', 'executions', 'resolved']`.

Three operator queue request types:
- **Approval** -- Agent needs a yes/no or multi-choice decision
- **Question** -- Agent needs freeform guidance
- **Alert** -- Agent is reporting a situation (acknowledgement only)

## User Story

As an operator, I want a single inbox where I can see and respond to all agent requests and notifications so that I can manage my fleet efficiently without switching between pages.

---

## Entry Points

- **UI**: `src/frontend/src/views/Operations.vue` -- `/operations` route (5 tabs via `?tab=` query param). Renders operator-queue tabs plus `MonitoringPanel.vue` (Health) and `ExecutionsPanel.vue` (Executions).
- **API**: `GET /api/operator-queue` -- List queue items
- **API**: `POST /api/operator-queue/{id}/respond` -- Submit response
- **API**: `GET /api/operator-queue/stats` -- Queue statistics
- **API** (#1017): `POST /api/operator-queue/bulk-cancel` / `POST /api/operator-queue/clear-resolved` -- per-tab "Clear All" bulk actions
- **API**: `GET /api/notifications` -- List agent notifications (used by Notifications tab)
- **API**: `GET /api/monitoring/status` -- Fleet health (Health tab, admin-only)
- **API**: `GET /api/executions` / `GET /api/executions/stats` -- Fleet execution list + stats (Executions tab)
- **MCP** (#1101): `list_operator_queue` (broad or scoped by `agent_name`) and `get_operator_queue_item` -- read-only triage surface over the queue for agents and external Claude Code clients (`src/mcp-server/src/tools/operator_queue.ts`). Agent-scoped keys are gated in the MCP layer to `{self} ∪ permitted` (the backend resolves an agent key to its owner, so agent-to-agent gating cannot live in the REST layer). `respond_to_operator_queue` (#1104) proxies the respond route — since trinity-enterprise#611 only with a person's user-scoped key (the backend refuses agent- and system-scoped keys, 403 `person_required`).
- **MCP / API** (trinity-enterprise#611): `get_my_ask` → `GET /api/agents/{name}/operator-queue/{request_id}` -- an agent reads back ITS OWN ask by the `request_id` it chose (self-acting: the agent comes from the key); a redacted projection that survives Clear All.
- **MCP / API** (trinity-enterprise#611, PR B): `ask_operator` → `POST /api/agents/{name}/operator-queue` -- an agent raises an ask AS ITSELF and gets a receipt; the native replacement for the queue file.
- **NavBar**: Single "Operations" link (`to="/operations"`, active when `$route.path === '/operations'`) with one combined badge `combinedOpsCount = operatorQueueStore.pendingCount + notificationsStore.pendingCount`. Replaces the former separate Health (`/monitoring`), Ops (`/operating-room`), and Executions (`/executions`) links + their badges.
- **Legacy redirects** (all preserve bookmarks): `/operating-room` (FUNCTION form, preserves `?tab=` query) -> `/operations`; `/monitoring` -> `/operations?tab=health`; `/executions` -> `/operations?tab=executions`; `/events` -> `/operations?tab=notifications`
- **Agent**: raises asks with `ask_operator`; the queue file `~/.trinity/operator-queue.json` inside the container is the fallback for two releases

---

## Frontend Layer

### Components

| File | Lines | Purpose |
|------|-------|---------|
| `src/frontend/src/views/Operations.vue` | -- | Main page -- 5-tab layout (Needs Response / Notifications / Health / Executions / Resolved), `?tab=` deep linking, combined subtitle, refresh button, polling lifecycle. Container is `max-w-7xl`; narrow operator card-feed tabs re-constrain to `max-w-3xl mx-auto`; Health/Executions panels own their `max-w-7xl` width. Tabs toggle by `v-if` (not v-show) so each panel's store-owned polling tears down on tab-leave. Operator-queue polling (`operatorQueueStore.startPolling(10000)`) and `agentsStore.fetchAgents()` run once at container level (queue polling drives Needs Response/Resolved feeds AND the NavBar badge). `isAdmin = computed(() => authStore.role === 'admin')` gates the Health tab. **Clear All** (#1017): per-tab button (operator tabs only via `isOperatorTab`, hidden when `clearableCount === 0`, `data-testid="ops-clear-all"`, lines 94-106) opens a `ConfirmDialog` (variant danger) with tab-specific blast-radius copy (`clearConfirmTitle`/`clearConfirmMessage`, lines 283-298); `confirmClearAll()` (lines 300-318) routes: needs-response -> `operatorQueueStore.bulkCancel(openItems ids)`, notifications -> `notificationsStore.dismissAll()`, resolved -> `operatorQueueStore.clearResolved()`. |
| `src/frontend/src/components/MonitoringPanel.vue` | -- | Health tab -- fleet monitoring content extracted from the deleted `views/Monitoring.vue`. Rendered `v-if="activeTab === 'health' && isAdmin"`. |
| `src/frontend/src/components/ExecutionsPanel.vue` | -- | Executions tab -- fleet execution list extracted from the deleted `views/Executions.vue`. Includes the "N running now" strip (the running-count badge that used to live on the NavBar Executions link). |
| `src/frontend/src/components/operator/QueueCard.vue` | 1-253 | Expandable card -- `AgentAvatar` component (with real avatar images from agents store), markdown body, inline response controls |
| `src/frontend/src/components/operator/ResolvedCard.vue` | 1-82 | Compact resolved item -- `AgentAvatar` component (with real avatar images), checkmark + response text for responded/acknowledged items; for response-less terminal items (cancelled/expired, #1017) renders a gray X badge "Cancelled"/"Expired" instead (`isTerminalWithoutResponse`, lines 17-40, 59-61) |
| `src/frontend/src/components/operator/NotificationsPanel.vue` | 1-541 | Notification list with agent/type/priority/status filters, bulk actions (acknowledge/dismiss), stats row, expandable messages, empty state |
| `src/frontend/src/components/operator/QueueItemDetail.vue` | 1-286 | Detail panel -- full item view with response controls (approval/question/alert) |
| `src/frontend/src/components/NavBar.vue` | -- | Single "Operations" nav link with combined badge count (queue + notifications); imports operatorQueue and notifications stores only |

### Route

```
/operations                          -> Operations.vue (route name 'Operations', meta: { requiresAuth: true })
/operations?tab=needs-response       -> Needs Response tab (default)
/operations?tab=notifications        -> Notifications tab
/operations?tab=health               -> Health tab (admin-only; non-admins coerced to default)
/operations?tab=executions           -> Executions tab
/operations?tab=resolved             -> Resolved tab
/operating-room                      -> REDIRECT to /operations (FUNCTION form, preserves ?tab= query)
/monitoring                          -> REDIRECT to /operations?tab=health
/executions                          -> REDIRECT to /operations?tab=executions
/events                              -> REDIRECT to /operations?tab=notifications
```

Registered in `src/frontend/src/router/index.js`:
```javascript
{
  path: '/operations',
  name: 'Operations',
  component: () => import('../views/Operations.vue'),
  meta: { requiresAuth: true }   // NOT requiresAdmin -- Health is gated at the tab level
}
```

Legacy redirects (`src/frontend/src/router/index.js`), all preserving bookmarks:
```javascript
{ path: '/monitoring', redirect: { path: '/operations', query: { tab: 'health' } } },
{ path: '/executions', redirect: { path: '/operations', query: { tab: 'executions' } } },
{ path: '/events', redirect: '/operations?tab=notifications' },
// FUNCTION form so existing ?tab= deep links survive the rename
{ path: '/operating-room', redirect: to => ({ path: '/operations', query: to.query }) },
```

**Admin gating of the Health tab**: the `/operations` route is `requiresAuth` only -- non-admins still reach Ops/Executions. Health is gated at the tab level: the tab button is `v-if="isAdmin"`, the panel render is `v-if="activeTab === 'health' && isAdmin"`. A non-admin landing on `?tab=health` (e.g. via the `/monitoring` redirect) is coerced to the default `needs-response` tab by `resolveTab()`. A `watch(isAdmin)` bounces off health if the role resolves async to non-admin, and re-selects health for an admin deep-link once the role confirms.

Tab selection uses `?tab=` query parameter. `Operations.vue` reads `route.query.tab` on mount and validates against `VALID_TABS = ['needs-response', 'notifications', 'health', 'executions', 'resolved']`. Defaults to `'needs-response'` if invalid, missing, or `health` for a non-admin. Tab switches call `switchTab()` -> `router.replace()` to update the URL without navigation.

### State Management

**Primary Store**: `src/frontend/src/stores/operatorQueue.js` (239 lines) -- Manages the operator queue (Needs Response + Resolved tabs)

**Additional Stores** (used by Notifications tab; see its own flow doc for full details):
- `src/frontend/src/stores/notifications.js` (315 lines) -- Manages notification list, filters, bulk actions, pending count. Used by `NotificationsPanel.vue`. Key getters: `pendingCount`, `hasUrgentPending`.
- `src/frontend/src/stores/agents.js` -- Provides agent data including `avatar_url`. Fetched on mount by `Operations.vue` (once, at container level) if not already loaded. Used by `QueueCard.vue` and `ResolvedCard.vue` to resolve agent avatar images.

**Operator Queue Store State:**
- `items` (ref) -- Array of queue items from backend API
- `expandedItemId` (ref) -- Currently expanded card (null = none)
- `autoExpandArmed` (ref, #1927) -- the landing rule's arming bit: `true` at store creation, consumed by an auto-expand, cleared by ANY human `toggleExpand`, re-armed (store-internal `watch`) whenever `openItems` drains to zero
- `activeTab` (ref) -- Tab state (note: the 5-tab switching is managed locally in Operations.vue, not in the store)
- `loading` (ref) -- Loading state
- `error` (ref) -- Error message

**Getters (computed):**
- `openItems` -- Pending items sorted by priority order (critical=0, high=1, medium=2, low=3), then by created_at ascending
- `resolvedItems` -- Items with status in `RESOLVED_STATUSES = ['responded', 'acknowledged', 'cancelled', 'expired']` (cancelled/expired added by #1017 -- previously cancelled items vanished from the UI entirely), sorted by `responded_at || created_at` descending (lines 53-61)
- `pendingCount` -- Count of items with status=pending (drives NavBar badge)
- `criticalCount` -- Count of pending items with priority=critical (drives badge color: red+pulse vs orange)
- `openItemsByAgent` -- Open items grouped by agent_name
- `getProfile(agentName)` -- Returns `{initials, color, role}` using deterministic hash of agent name against 8 Tailwind colors (legacy; `QueueCard` and `ResolvedCard` now use `AgentAvatar` component with real avatar images instead)

**Actions:**
- `fetchItems()` -- `GET /api/operator-queue?limit=200` with auth header (line 87-102)
- `respondToItem(id, response, responseText)` -- `POST /api/operator-queue/{id}/respond` with a body built by `utils/operatorQueue.js::queueResponseBody` (#2370 — the decision rides `response`, the note rides `response_text`, trimmed, empty → `null`; the ONE builder shared with `/m`), optimistic local update mirroring that body, auto-advance to next open item. On **409** (item left 'pending' under us, e.g. another operator's bulk-cancel landed first, #1017) **or 400** (already terminal) — `respondRefusedAsNotPending` — sets `error` to the shared attribution-free `QUEUE_RESPONSE_NOT_RECORDED` copy and refetches (`stores/operatorQueue.js` ~line 116-158; 404 "row gone" is treated the same). The refetch is **awaited and the copy assigned after it**: `fetchItems()` nulls `error` in its own synchronous prologue, so assigning first and calling it un-awaited destroyed the notice in the same tick. That was survivable while 409 was the only refused status, but `respondRefusedAsNotPending` also takes 400 and 404 — which previously fell through to the generic `apiErrorMessage` branch and *did* report — so the ordering had to be fixed with the widening or two statuses would have gone silent. A failed refetch's own message is deliberately overwritten by the not-recorded copy. Remaining under **#2377**: `Operations.vue` still renders `error` only as the `:detail` of the "Couldn't refresh the queue" banner, and `InlineError` puts `detail` inside a **collapsed `<details>` disclosure** — so the operator sees a headline about *refreshing* and must expand "Technical detail" to learn their answer was not recorded; the next 15s poll then clears it. Verified against the running stack: with the fix, the banner renders and the copy is one click away; without it, no banner renders at all. This is the fetch-error field carrying a verb outcome and it needs its own field/surface (#2377's own AC — of which "the store test asserts the notice is observable after a 409 and a 400" is now satisfied here)
- `bulkCancel(ids)` -- `POST /api/operator-queue/bulk-cancel` via shared `api.js` client; refetches; returns `{cancelled, skipped}` (#1017, line 141-152)
- `clearResolved(agentName = null)` -- `POST /api/operator-queue/clear-resolved` via `api.js`; refetches; returns `{cleared}` (#1017, line 154-166)
- `acknowledgeItem(id)` -- Shorthand that calls `respondToItem(id, 'acknowledged', '')` (line 168-170)
- `toggleExpand(id)` -- Toggle expandedItemId AND disarm auto-expand (a human took control — #1927)
- `maybeAutoExpand()` (#1927) -- expand the first open item iff armed and no OPEN item is already expanded (`utils/loadingState.js::decideAutoExpand` checks membership of `expandedItemId` in the open set, so an id answered while the operator was away never blocks the rule); consumes the armed bit. Called by `Operations.vue`'s `watch(() => store.openItems, …, { immediate: true })`
- `handleWebSocketEvent(data)` -- Handles real-time updates from WebSocket; `operator_queue_cleared` triggers a full `fetchItems()` refetch (line 177-200)
- `startPolling(interval)` -- Begin polling with initial fetch + setInterval (default 15s, called with 10s from Operations.vue at container level) (line 203-207)
- `stopPolling()` -- Clear poll timer (line 209-214)

### API Calls

```javascript
// List items (fetchItems)
await axios.get('/api/operator-queue', {
  params: { limit: 200 },
  headers: authStore.authHeader
})

// Respond to item (respondToItem) — body from utils/operatorQueue.js (#2370)
const body = queueResponseBody(response, responseText)  // { response, response_text: trimmed || null }
await axios.post(`/api/operator-queue/${id}/respond`, body, { headers: authStore.authHeader })

// Clear All on Needs Response tab (bulkCancel, #1017) — via shared api.js client
await api.post('/api/operator-queue/bulk-cancel', { ids })   // -> {cancelled, skipped}

// Clear All on Resolved tab (clearResolved, #1017)
await api.post('/api/operator-queue/clear-resolved', { agent_name: agentName })  // -> {cleared}
```

### WebSocket Events

Handled in `src/frontend/src/utils/websocket.js:147-149`:

Events are keyed by `type` (not `event`), dispatched from the `default` case in the WebSocket message handler:

```javascript
if (data.type === 'operator_queue_new' ||
    data.type === 'operator_queue_responded' ||
    data.type === 'operator_queue_cancelled' ||   // trinity-enterprise#611
    data.type === 'operator_queue_acknowledged' ||
    data.type === 'operator_queue_cleared' ||
    data.type === 'operator_queue_sync') {   // #2915
  operatorQueueStore.handleWebSocketEvent(data)
}
```

Event handling in the store (`handleWebSocketEvent`, line 177-200):
- `operator_queue_new` -- Triggers full `fetchItems()` refetch to get complete item data
- `operator_queue_responded` / `operator_queue_cancelled` (trinity-enterprise#611) -- an ask ended; the trigger is thin (`{id, agent_name}` only, #918) and agent-keyed so the ent#467 `/ws` filter scopes it -- triggers full `fetchItems()` refetch (the old responded payload carried the responder's email and the answer, patched locally)
- `operator_queue_acknowledged` -- Updates item status to 'acknowledged' locally
- `operator_queue_cleared` (#1017) -- Bulk clear by an operator (any browser tab/user) -- triggers full `fetchItems()` refetch of authoritative state
- `operator_queue_sync` (#2915) -- the poller changed some row's sync/delivery state this cycle; ONE thin trigger per cycle (no payload, #918) -- triggers full `fetchItems()` refetch

### Response Controls by Type

| Type | UI Control (QueueCard.vue) | Submit Action |
|------|---------------------------|---------------|
| Approval | Option buttons (green/red/blue border) + optional text input + Send (line 99-133) | `respondToItem(id, selectedOption, note)` |
| Question | Textarea + "Send Answer" button (line 136-156) | `respondToItem(id, answerText, '')` |
| Alert | "Got it" button (line 159-166) | `acknowledgeItem(id)` |

The type pill text comes from `utils/operatorQueue.js::queueTypeLabel` (`Needs approval` / `Question` / `Heads up`), shared with `/m` (#2370). **The `/m` mobile admin mirrors this table** — approval: select → restated consequence → optional note → `Cancel` + `Send: <option>` (nothing sends on one tap); question: text + Send; alert: `Got it` — and builds the identical body via `buildQueueResponse` (see [mobile-admin-pwa.md](mobile-admin-pwa.md)). The Workspace asks panel posts to its own endpoint/model and is **not** yet a consumer of the shared builder (#2375).

**An approval's decision must be one it OFFERED (#2376).** `OperatorResponse.response`
is a bare `str` and every layer passed it through verbatim — router, DB write,
and the write-back into the agent's own queue file — so no layer checked
membership. #2370 recorded `response: "approved"` against
`options: ["Approve", "Deny"]` for five months without a single 4xx, and the
agent read back a decision string it never offered.
`services/operator_queue_choices.py` is the one rule: for `type == "approval"`
with a usable options list, a non-empty `response` outside it is a named **422**
`response_not_an_offered_option` **carrying the offered list** — a refusal that
does not name agent-authored strings leaves the operator guessing. Matching is
**exact**: only the agent knows whether `approve` and `Approve` mean the same
thing to it, so normalising here would answer that on its behalf. Exempt, all
spelled as the same absence rather than as special cases at the call site:
questions, alerts, approvals that offered nothing, a non-list or non-string
blob, and one whose options were replaced by the #1632 size-cap marker (that
placeholder RECORDS that the choices were dropped, so it is neither binding nor
selectable). A text-only answer is untouched — an empty `response` means the
field was never filled in, not that it is wrong. The rule sits at the SINK
rather than in a router because this is the approval channel for irreversible
actions (#1402, ent#329) with four producers today; both writers
(`routers/operator_queue.py`, `client_portal/asks/service.py`) call it, and an
AST caller-parity guard (`tests/unit/test_2376_approval_response_membership.py`,
the #1677 shape) fails CI if a third writer appears without it.

### NavBar Badge

`src/frontend/src/components/NavBar.vue`:

- Single "Operations" link (`to="/operations"`, active when `$route.path === '/operations'`). Replaces the former separate Health (`/monitoring`), Ops (`/operating-room`), and Executions (`/executions`) links (#1109).
- Imports two stores: `useOperatorQueueStore`, `useNotificationsStore`
- **Combined count**: `combinedOpsCount = operatorQueueStore.pendingCount + notificationsStore.pendingCount`
- Badge shows `combinedOpsCount` with max display of "99+"
- **Critical detection**: `hasCriticalOpsItem = operatorQueueStore.criticalCount > 0 || notificationsStore.hasUrgentPending`
- Color: `bg-red-500 animate-pulse` when `hasCriticalOpsItem`, otherwise `bg-orange-500`
- Badge hidden when `combinedOpsCount === 0`
- NavBar starts polling for notifications (60s) on mount, stops on unmount
- **Removed** (#1109): the separate Executions running-count badge (running count now lives inside the Executions tab's "N running now" strip). Earlier: standalone Events bell icon and Alerts bell icon.

### UX Behaviors

1. **5-tab layout** -- Needs Response, Notifications, Health (admin-only), Executions, Resolved. Operator card-feed tabs show a count badge when > 0. Tabs toggle by `v-if` so each panel's store-owned polling tears down on tab-leave.
2. **Deep linking** -- `?tab=` query parameter selects the active tab; `switchTab()` updates URL via `router.replace()` (line 168-170)
3. **Dynamic subtitle** -- Shows combined summary like "3 pending responses, 2 notifications" or "All clear" (computed `subtitle`, line 152-165)
4. **Auto-expand first item** on page load -- store rule `maybeAutoExpand()` (#1927), evaluated by `Operations.vue` immediately and on every open-set change; once per armed episode, so a poll delta / WS arrival / remount never re-expands a card the operator collapsed (design-system p5); re-arms when the queue drains to zero. (Before #1927 a bare `watch(openItems.length)` re-expanded on every delta.)
5. **Auto-advance** after responding -- next open item expands automatically (store `respondToItem` line 119-122)
6. **Collapse on click** -- X button in QueueCard header (`@click.stop="store.toggleExpand(item.id)"` line 52)
7. **Context collapsible** -- "Show details" toggle in QueueCard (line 70-94)
8. **Combined badge in NavBar** -- Orange when any items pending, red+pulse when critical queue items or urgent notifications
9. **Polling fallback** -- 10s interval from Operations.vue (container level) for queue items, 60s for notifications (via NavBar). Health/Executions panels own their own polling, torn down on tab-leave.
10. **Form reset** -- `watch(isExpanded)` in QueueCard resets selectedOption, responseText, showContext on collapse (line 191-197)
11. **Manual refresh** -- Refresh button next to tabs (`Operations.vue`). Shows spinning icon (`animate-spin`) while `store.loading` is true. Calls `store.fetchItems()` and `notificationsStore.fetchPendingCount()`. Disabled during loading. Positioned via `ml-auto` to sit at the right edge of the tab bar.
12. **Notifications tab features** -- Agent/type/priority/status filters, show-dismissed toggle, bulk acknowledge/dismiss, select-all, expandable messages, load-more pagination, stats cards (pending/acknowledged/total/agents)
13. **Clear All** (#1017) -- Per-tab button on operator tabs only (Needs Response / Notifications / Resolved; hidden on Health/Executions and when the tab has nothing to clear). Always confirms via `ConfirmDialog` with blast-radius copy (e.g. Needs Response: "agents waiting on them will be told their requests were cancelled... affects all operators of these agents"). Needs Response sends only the **rendered** `openItems` ids, so a sync-loop race can never cancel items the operator never saw.

---

## Backend Layer

### Registration

**`src/backend/main.py`**:
- Router import: line 70 -- `from routers.operator_queue import router as operator_queue_router, set_websocket_manager as set_operator_queue_ws_manager`
- Sync service import: line 82 -- `from services.operator_queue_service import operator_queue_service, set_websocket_manager as set_opqueue_sync_ws_manager`
- WebSocket manager injection: line 193-194 -- `set_operator_queue_ws_manager(manager)` and `set_opqueue_sync_ws_manager(manager)`
- Router registration: line 357 -- `app.include_router(operator_queue_router)`
- Service start (lifespan): line 254-258 -- `operator_queue_service.start()`
- Service stop (lifespan): line 290-294 -- `operator_queue_service.stop()`

### Endpoints

**Router**: `src/backend/routers/operator_queue.py` (311 lines)

Prefix: `/api/operator-queue`, Tags: `["operator-queue"]`

All endpoints require JWT authentication via `get_current_user` dependency. Per-caller agent scoping comes from `_accessible_set()` (line 60-70): returns `None` for admins (no filter) or a `Set[str]` of accessible agent names (possibly empty) for regular users — the tri-state contract is threaded down into the DB layer for the bulk endpoints.

| Method | Path | Handler | Line | Description |
|--------|------|---------|------|-------------|
| GET | `/api/operator-queue` | `list_queue_items()` | 83-106 | List with filters: status, type, priority, agent_name, since, limit (1-500, default 100), offset. Rows hidden by Clear All are excluded (`cleared_at IS NULL` default in `list_items`, #1017) |
| GET | `/api/operator-queue/stats` | `get_queue_stats()` | 109-115 | Counts by status/type/priority/agent, avg response time, responded today |
| POST | `/api/operator-queue/bulk-cancel` | `bulk_cancel_queue_items()` | — | (#1017) Cancel a list of still-pending items in one call. Body `{ids: [...], reason?}` (1-500 ids; `reason` ≤ 500, #611); ids are deduped order-preserving so the `skipped` count is honest. Only listed ids are touched; non-pending/inaccessible ids are skipped. **A person only** (403 `person_required`, #611). Through the ask sink: one `batch_id` stamped on exactly the rows this sweep flipped, ONE `bulk_cancel` audit row (the ids actually cancelled + `batch_id` + `has_reason`), ONE `operator_queue_cleared` trigger (`scope`, `count` — no operator email), ONE ending event to the observers. Returns `{cancelled, skipped, batch_id}` |
| POST | `/api/operator-queue/clear-resolved` | `clear_resolved_queue_items()` | 158-204 | (#1017) **Hide** terminal items (`acknowledged`/`cancelled`/`expired`) by setting `cleared_at` — NOT a DELETE (a delete would be resurrected by the 5s sync loop; see DB layer). `responded` rows are kept visible so the sync write-back can still deliver the answer. Actual row deletion is deferred to the retention sweep (#1142). Body `{agent_name?}` (403 if inaccessible). Idempotent — empty match returns `{cleared: 0}`. Audit-logged (`clear_resolved`), broadcasts `operator_queue_cleared` (`scope: "resolved"`) when `cleared > 0` |
| GET | `/api/operator-queue/{item_id}` | `get_queue_item()` | 207-218 | Single item by ID; 404 if not found (does NOT filter on `cleared_at` — hidden items remain fetchable by id) |
| POST | `/api/operator-queue/{item_id}/respond` | `respond_to_queue_item()` | — | Submit operator response. **A person only** (403 `person_required`, checked before the row is read, #611). Validates status=pending (400), divergence (409 `item_diverged`, #2915), the offered option (422, #2376 — enforced by the ask sink); the sink's CAS writes the answer + ledger; 409 on a lost race (#1017) and 409 `expired` when the deadline passed before the poller swept (#611); audit `answered`, thin `operator_queue_responded`, the ent#329 resume |
| POST | `/api/operator-queue/{item_id}/cancel` | `cancel_queue_item()` | — | Cancel pending item. **A person only** (#611). Optional body `{reason}` (≤ 500). Validates status=pending (400); the sink's CAS writes the cancel + ledger; 409 on a lost race (was a silent 200); audit `cancelled` (`has_reason`, never the text), thin `operator_queue_cancelled`, the ending wake |
| GET | `/api/agents/{name}/operator-queue/{request_id}` | `get_my_ask()` (`agent_router`) | — | (#611) The agent's OWN ask by its own `request_id` — only as itself (`get_self_acting_agent`: agent key == name, system key == `trinity-system`; any other principal or name → one uniform 403 `agent_identity_required`, then `AuthorizedAgent`'s uniform 404). Ignores `cleared_at`. Redacted projection (`_READBACK_FIELDS`: no person email, no `resolved_to`) |
| POST | `/api/agents/{name}/operator-queue` | `raise_my_ask()` (`agent_router`) | — | (#611 PR B) The agent raises an ask as itself (the same `get_self_acting_agent` gate). Body `OperatorAskCreate` (`extra="forbid"`) into `ask_service.raise_ask`; **201** with the receipt, **200** replaying the first receipt, named 422 / 429 refusals as `{code, message, …}` — see [Raising an ask](#raising-an-ask-trinity-enterprise611-pr-b) |
| GET | `/api/operator-queue/agents/{agent_name}` | `get_agent_queue_items()` | 296-311 | Items for specific agent with optional status filter, limit (1-500, default 50) |

**Route ordering**: the static `/bulk-cancel` and `/clear-resolved` routes are registered BEFORE the `/{item_id}` catch-all (Architectural Invariant #4).

**Request models** (line 36-53):
```python
class OperatorResponse(BaseModel):
    response: str
    response_text: Optional[str] = None

class BulkCancelRequest(BaseModel):          # #1017
    ids: List[str] = Field(..., min_length=1, max_length=500)

class ClearResolvedRequest(BaseModel):       # #1017
    agent_name: Optional[str] = None
```

**Respond endpoint flow** (line 221-270):
1. Fetch existing item from DB
2. Validate item exists (404 if not)
3. Validate item status is "pending" (400 if not)
4. Call `db.respond_to_operator_queue_item()` with response, user ID, user email
5. **Race check** (#1017, line 252-256): if the DB layer returned the `_status_conflict` marker (item left 'pending' between step 3 and the UPDATE, e.g. a bulk-cancel landed) -> 409 "Item is no longer pending (now '{status}') — response was not recorded" instead of a silent 200
6. Broadcast `operator_queue_responded` WebSocket event via `_websocket_manager`
7. Return updated item

**WebSocket broadcast payload** (line 259-268):
```json
{
  "type": "operator_queue_responded",
  "data": {
    "id": "<item_id>",
    "agent_name": "<agent_name>",
    "responded_by_email": "<user_email>",
    "response": "<response_text>"
  }
}
```

**`operator_queue_cleared` broadcast payload** (#1017, line 146-153 / 195-202 — one event per bulk operation, not per item):
```json
{
  "type": "operator_queue_cleared",
  "data": {
    "scope": "pending|resolved",
    "count": 7,
    "cleared_by": "<user_email>"
  }
}
```

### Sync Service

**File**: `src/backend/services/operator_queue_service.py` (290 lines)

Background async service that bridges agent containers and the database. Global singleton instance at line 290.

**Constants**:
- `QUEUE_FILE_PATH = ".trinity/operator-queue.json"` (line 30)
- `DEFAULT_POLL_INTERVAL = 5` seconds (line 31)

**Poll cycle** (`_poll_cycle`):
0. **Leader gate (#1632)**: acquires/refreshes the `opqueue:leader` Redis lease (SET NX, TTL `max(3×poll-interval, 30s)` — a floor so one slow-writing agent's cycle can't expire the lease mid-cycle and flap leadership — own-lease-only refresh, fail-open to leader on Redis down — mirror monitoring #1464). A non-leader worker **returns immediately**, so under `--workers 2` only one worker syncs → no double-charge of the ingestion rate limiter, no double-broadcast of the flood alert, no double-scan of agent files.
1. Gets running agents via `list_all_agents_fast()` (lazy import from `services.docker_service`)
2. Filters to only `status == "running"` agents
3. Calls `db.mark_operator_queue_expired()` for items past `expires_at`
4. Concurrently syncs each agent via `asyncio.gather(*tasks)`

**Agent sync** (`_sync_agent`):
1. Creates `AgentClient(agent_name)` and reads `~/.trinity/operator-queue.json` via `client.read_file()` with 5s timeout
2. **Restart resilience**: If the file does not exist (e.g., container restart wiped filesystem), the service no longer returns early. Instead it creates an empty `queue_data = {"$schema": "operator-queue-v1", "requests": []}` and continues, tracking `file_exists = False`. This allows responded items in the DB to be reconstructed and delivered back to the agent.
3. **Oversized-file guard (#1632)**: if the file exceeds `OPERATOR_QUEUE_MAX_FILE_BYTES` (2 MiB) it is **not parsed** — one flood alert is emitted and the agent is skipped this cycle (C1 per-cycle DoS guard). Otherwise parses JSON and iterates the `requests` array (bounded to `OPERATOR_QUEUE_MAX_SCAN_PER_CYCLE` = 500 items/cycle).
4. **Ingestion caps (#1632)** — for each new `pending` request not already in DB (see "Ingestion caps" below): the reserved-id guard, per-agent **pending-DEPTH cap** (`db.count_operator_queue_pending_for_agent`, primary), and per-agent + fleet **RATE cap** gate admission; an admitted item is field-hygiene-clamped (`_clamp_ingested_item`) then created via `db.create_operator_queue_item()` and added to `new_items`. A depth-held / rate-skipped / malformed-id episode emits **one** aggregated flood alert.
5. For each request with `status=acknowledged`: marks acknowledged in DB via `db.mark_operator_queue_acknowledged()`
6. Broadcasts `operator_queue_new` WebSocket events for new items (line 156-171)
7. Broadcasts `operator_queue_acknowledged` WebSocket events for acknowledged items (line 173-184)
8. Checks for `responded` items via `db.get_operator_queue_responded_for_agent()` AND — only when `file_exists` (#1017) — recently terminal (`cancelled`/`expired`) items via `db.get_operator_queue_terminal_for_agent()` (created_at-bounded to the last 168h so the per-agent 5s query stays cheap; there is no per-status timestamp column; deliberately NOT filtered on `cleared_at` — hidden items still need their flip delivered). Passes both lists + `file_exists` to `_write_responses_to_agent()` (line 186-199).

**Response write-back** (`_write_responses_to_agent`, line 201-286):

Accepts `terminal_items` (#1017 — cancelled/expired) and a `file_exists` parameter (default `True`) to handle the case where the agent's queue file is missing after a container restart.

1. Builds lookup maps of responded items and terminal items by ID
2. Iterates agent's JSON requests, updates matching pending items with response data
3. **Terminal-status propagation** (#1017, line 235-238): file entries still in `status=pending` whose ID is in the terminal map are flipped in place to the item's DB status (`req["status"] = terminal_map[req_id]["status"]` — `cancelled` or `expired`) so the agent stops waiting on them (and so a stale 'pending' file entry can't resurrect the item if its row is ever purged). Terminal entries are **never appended** if missing from the file (unlike responded items) — there is nothing to deliver.
4. **Reconstruction of missing items** (line 242-260): For any responded item in the DB whose ID is not found in the agent's `requests` array (tracked via `seen_ids` set), the service reconstructs a full request entry from DB data and appends it to the `requests` array. Reconstructed fields include: `id`, `type`, `status` (set to `"responded"`), `priority`, `title`, `question`, `options`, `context`, `created_at`, `response`, `response_text`, `responded_by`, `responded_at`.
5. Writes updated JSON back to agent via `client.write_file(QUEUE_FILE_PATH, content, timeout=10.0, platform=True)`; on success logs "Wrote N responses and M terminal-status flips back to {agent}" (line 277-280)
6. Sets `status=responded`, `response`, `response_text`, `responded_by`, `responded_at` fields in the agent's JSON
7. The agent server's `write_file` endpoint (`docker/base-image/agent_server/routers/files.py:360`) automatically creates parent directories (`mkdir(parents=True, exist_ok=True)`), so `.trinity/` is created if it doesn't exist.

**WebSocket broadcast payloads**:

`operator_queue_new` (line 159-169):
```json
{
  "type": "operator_queue_new",
  "data": {
    "id": "<item_id>",
    "agent_name": "<agent_name>",
    "type": "approval|question|alert",
    "priority": "critical|high|medium|low",
    "title": "<title>",
    "created_at": "<iso_timestamp>"
  }
}
```

`operator_queue_acknowledged` (line 176-182):
```json
{
  "type": "operator_queue_acknowledged",
  "data": {
    "id": "<item_id>",
    "agent_name": "<agent_name>"
  }
}
```

**Lifecycle**: Started in `main.py` lifespan (line 254-258), stopped on shutdown (line 290-294).

### Sync honesty (#2915)

**Why.** A pending approval sat unseen for six days on a production instance: the agent had rewritten its entry (title, body, options, expiry) and later marked it `acknowledged` on its side, the platform kept the original snapshot and a `pending` row, and nothing on either side raised anything. Ingestion is create-only (an entry whose `(agent, request_id)` already exists is skipped), the acknowledge UPDATE matches only `responded` rows, and only `pending` / `acknowledged` file statuses were ever read — so every agent-side change after ingest was silent. The file contract is deliberately unchanged (an approval is frozen to the exact action the human read; corrections are trinity-enterprise#619, the native object is #611); the sync now **detects, records, refuses where it must, and audits**.

**Per-agent reconcile (`_sync_agent`).** One index read replaces the per-entry `exists()` probe: `db.get_operator_queue_sync_index_for_agent` returns full rows for pending + responded items and `{request_id: id/status/sync_state}` for every terminal row. Each file entry is classified against it:

| Entry vs row | `sync_state` | `sync_detail` |
|---|---|---|
| present, same content, `pending` (or our own `responded` write landed) | `confirmed` | — |
| present, content differs (`title` / `question` / `options` / `expires_at` / `type` / `priority` / `context` / `addressee`, compared through a pure fingerprint: the same caps and `create_item` defaults the row was stored with, canonical options JSON, normalised `expires_at`, `context` as canonical JSON with the platform's workspace-thread key stripped and the clamp's truncation marker as one sentinel; `addressee` only when the row resolved one, since that value is a roster decision, not a copy) | `changed` | the changed field names |
| entry status set by the agent on a row that was never responded (`acknowledged`, `cancelled`, `withdrawn`, …) | `closed_by_filer` | the folded status (`[a-z_]{1,32}` or `other`) |
| open `pending` row absent from the file / file absent (a `responded` row the file lost is the write-back's business — it re-appends it the same cycle; a platform-minted alarm was never in the file and is never `missing`) | `missing` | `entry_missing` / `file_missing` |
| `pending` entry re-using a **terminal** row's id (the on-conflict create would return the old uuid silently — a phantom admit + "new" broadcast every cycle) | `stale_id` (on the terminal row) | the terminal status |
| read failed 3 cycles in a row (hysteresis; the 5 s read times out on a busy container), file unparseable / wrong shape (not an object with a `requests` list — read as unconfirmed, never as empty) / oversize, agent not running, Docker unreadable | `unconfirmed` | `timeout` / `unreachable` / `http_<code>` / `invalid_json` / `wrong_shape` / `oversize_file` / `agent_not_running` |

Writers are edge-triggered (`WHERE sync_state IS NULL OR != :v …`; the rowcount **is** the transition, never read-then-write, so two overlapping leaders cannot double-record); `last_confirmed_at` is refreshed by ONE batched UPDATE per agent per cycle for confirmed rows older than 60 s. An unparseable, wrong-shape or oversize file marks `unconfirmed` and **runs no write-back that cycle** — the previous path treated it as an empty request list and overwrote the agent's file with the reconstructed responses alone. A duplicated id in the file is reconciled once, on its first entry (two copies with different content used to flip the row every cycle). No file-derived state is ever written on a platform-minted row (the `unconfirmed` sweeps exclude the reserved id prefixes in SQL, `_not_prefixed`). `_poll_cycle` now reads the tri-state `docker_service.agent_container_states()` (`None` = Docker unreadable ⇒ nothing swept, nothing synced — the #2196 class), runs expiry **before** the "no running agents" return (an all-stopped fleet never expired anything), sweeps open rows of non-running agents to `unconfirmed:agent_not_running` (explicit empty-set branch, never `notin_([])`), marks every answer / terminal flip still owed to a non-running agent `undelivered:agent_not_running` (`db.mark_operator_queue_undelivered_for_stopped_agents`, edge-triggered, each transition audited `undeliverable` once — AC3: the row, not just the sync state, says the container cannot receive it), and broadcasts ONE thin `operator_queue_sync` per cycle when any row changed.

**Write-back (`_write_responses_to_agent`).** Re-reads the file immediately before writing, merges by id, and writes with `if_match=<sha256 of the UTF-8 text it read>`; the agent server's `PUT /api/files` writes atomically (tmp + `os.replace`) and answers **412** on a mismatch, so an entry the agent appended in between is refused-and-retried, never clobbered (an older agent server ignores the parameter and degrades to the narrowed window). Both sides hash the text the download endpoint serves (`errors="replace"`), so a stray non-UTF-8 byte cannot wedge delivery behind an endless 412. The check-then-replace on the agent server is not atomic against the agent's own unlocked `write_text`; a lock in the server alone would not close that window, and the single leader-locked poller is the only platform writer. A response is delivered **only into an entry that is still `pending` and whose content still matches the row** — an answer to a rewritten question is `undelivered:entry_changed`, to a closed one `undelivered:closed_by_filer` — **unless the human acknowledged the divergence at respond time** (`divergence_acknowledged_at`, stamped by the operator route and the portal answer when `acknowledge_divergence` is sent against a `changed` / `closed_by_filer` row): then the answer is written into the entry as it is now, a closed entry included (status → `responded`), so "send again to answer anyway" reaches the file. A previously landed answer is recognised by its own `responded_at` on the entry, never by the entry's status alone; and `db.mark_operator_queue_acknowledged` flips a `responded` row only when `delivery_state = 'delivered'`, so an entry the agent closed itself can no longer be read as an acknowledgement of an answer it never saw (a legacy answer delivered before #2915 self-heals: the write-back records it `delivered` on sight, the flip follows next cycle). A file that the cycle-start read had and the pre-write re-read does not is an agent mid-rewrite: nothing is written and nothing recorded that cycle; a file missing at both reads is reconstructed (the container-restart case). Terminal (cancelled/expired) flips are fetched by delivery state (`db.get_operator_queue_terminal_for_agent`: `delivery_state IS NULL OR 'undelivered'`, oldest first, capped 200, and **excluding `undelivered:entry_missing`** — a flip whose entry the agent already dropped can never land, stays recorded, and must not starve the rows that still can) instead of a 168 h `created_at` window, so a flip is attempted until it lands or is recorded (`undelivered:entry_missing`); the header's `undelivered_count` likewise excludes a dropped cancellation, which is recorded but is nobody's action. Platform-minted rows are `not_applicable:platform_minted` (whether still `responded` or already terminal — and they take NO part in delivery even when a pre-ent#499 file still carries them as `responded` entries; matching those flip-flopped delivered ↔ not_applicable every cycle on the first live run). **Upgrade-time one-off:** the first cycles after this lands record every pre-existing cancelled/expired row whose entry is already gone as `undelivered:entry_missing`, once each (edge-triggered; ≤200 per agent per cycle), so the header count is honest about history rather than silent about it. Every write outcome lands as `delivery_state` (`delivered` / `undelivered:<conflict|http_<code>|unreachable|timeout>`). The aging receipt (`platform.aging_since`, written once at the crossing, taught in the prompt's Operator Communication section) rides the same write — never a standalone write at ingest, which would race the agent's own turn.

**Refusal.** `POST /api/operator-queue/{id}/respond` and the portal answer return **409 `{code: "item_diverged", message, sync_state, sync_detail}`** while `sync_state ∈ {changed, closed_by_filer}` unless the body carries `acknowledge_divergence: true` (`OperatorResponse` / `WorkspaceAskAnswer`; MCP `respond_to_operator_queue` takes the same parameter). The store treats it as a verb outcome: refetch (the badge now renders), keep the card open, show `InlineError` beside the controls, and the next Send carries the acknowledgement.

**Aging.** `operator_queue_aging_hours` (ops setting, default 24, `0` disables, validated 0–8760, admin-gated `GET/PUT /api/settings/ops/config`, edited in Settings → Operations through `components/settings/OperatorQueueAgingSetting.vue` on the `BaseInput` primitive). `operator_queue_service.is_aged` / `annotate_aging` compute `aging` + `aged_since` once, server-side, for the list/get routes and the portal projection; the frontend renders, it never recomputes.

**Escalation = a visible count.** The list response carries `undelivered_count` and `closed_by_filer_count` (`db.count_operator_queue_flags`, access-scoped); the Operations header renders both and offers **Cancel N closed by agents** — the existing bulk-cancel over the `closed_by_filer` pending ids, through the same `ConfirmDialog`. No queue item is minted about queue items.

**Audit** (`platform_audit_service`, `event_type=operator_queue`, `source=system`, target = the row uuid, details = ids and enums only): `ingested` · `diverged` (→ changed / closed_by_filer / missing / stale_id) · `reconciled` (diverged → confirmed) · `written_back` · `undeliverable`. The `confirmed ↔ unconfirmed` flap is never audited: a fleet restart writes zero rows.

**Surfaces.** ONE rule, `utils/operatorQueue.js::queueSyncBadge(item)` → `{label, variant, title} | null`, rendered as a `BaseBadge` by `QueueCard`, `ResolvedCard` and `PortalAsks`, and as a currentColor pill in `/m` (`MobileAdmin.vue`, so its colour count cannot move). The portal projection (`WorkspaceAsk`) exposes a coarse `sync ∈ {confirmed, changed, closed, unconfirmed}` + `aging` only — no reason, no poller timestamp.

**Residuals (registered in the debt inbox, owned by trinity-enterprise#619/#611):** `closed_by_filer` rows still count toward the #1632 depth cap until cancelled; a response undelivered because the entry changed *after* the answer (with no acknowledgement to carry) has no re-answer path — narrowed by #611: the agent reads the answer back by its own `request_id` (`get_my_ask`); a re-ask with a reused id after `acknowledged` is swallowed by #1631's design; the four remaining string-prefix workspace guards in the agent server's files router; a NUL byte in a `path` is an unhandled 500 there.

**Tests.** `tests/unit/test_2915_operator_queue_sync_honesty.py` (the seven divergence fixtures, hysteresis, no write on invalid JSON or a wrong-shape file, tri-state sweep, one broadcast per cycle, write-back gating and `if_match`, the pre-write re-read, the vanished-file guard, acknowledged delivery into a rewritten and into a closed entry, first-wins duplicates, the extended fingerprint, the stopped-agent delivery sweep, aging receipt once, fingerprint stability, and the accessors on a real migrated SQLite — delivered-only acknowledge, platform-row exclusion, terminal ordering, header-count exclusion); `tests/unit/test_2915_agent_server_files_if_match.py`; `src/frontend/tests/unit/operatorQueueSyncBadge.spec.js` (rule + store 409 flow), `queueCardSyncBadge.spec.js` (mounted), `operatorQueueAgingSetting.spec.js` (mounted).

### Endings (trinity-enterprise#611)

**Why.** An ask ends in exactly one of three ways — answered, cancelled, expired — and before #611 five write sites ended one, each with its own subset of side effects: single cancel recorded no actor, audited nothing, broadcast nothing and woke nobody; respond audited nothing; expiry never broadcast; bulk-cancel audited the ids it was ASKED to cancel. No surface could say who ended an ask or when (ResolvedCard showed `created_at` as the ending time), and an agent with no next turn waited forever on a cancelled ask. And an agent-scoped key resolved to its owner, so an agent could answer or cancel any ask its owner could reach — its own approval included — recorded as the owner.

**The ledger.** Written in the SAME compare-and-set UPDATE that flips `status`, so a writer that loses the race records nothing: `disposition` (`answered | cancelled | expired`), `disposed_at`, `disposed_by` (`person | timeout` — an enum of two), `disposed_by_email` (NULL for timeout), `disposition_reason` (the operator's optional cancel reason, ≤ 500), `batch_id` (one uuid per bulk sweep). Nullable, no backfill — a row that ended before the ledger reads from `status`, and no surface presents its `created_at` as an ending time. The same migration (`operator_queue_ask_object` / Alembic `0076_operator_queue_ask_object`) adds `raised_by`, `channel`, `to_role`, `resolved_to`, `proposal`, `supersedes_expired` for PR B (agent-raised asks); the file poller already stamps `channel='file', raised_by='agent'` as keyword-only arguments — never read from the agent's entry. `expires_at` is stored ISO-Z at the sink (Invariant #16; an unparseable value is kept verbatim so the #2915 fingerprint never reads it as a rewrite).

**The writers** (`db/operator_queue.py`): `respond_to_item` (CAS adds `expires_at IS NULL OR expires_at > now` — a late answer returns `_status_conflict` with status still `pending`); `cancel_item(item_id, *, disposed_by_email, reason)` (CAS; a lost race returns `_status_conflict`); `bulk_cancel_items(…, *, disposed_by_email, reason) → {batch_id, rows}` (one UPDATE stamps the sweep's own `batch_id`, the re-select `id IN (:ids) AND batch_id = :b` is exactly the winners — dialect-agnostic, no RETURNING); `mark_expired() → rows` (bounded candidate select, then per-id CAS — overlapping leaders end each row once).

**The sink** (`services/ask_service.py`): `answer(item, …)` (runs the #2376 offered-option check first — the one writer of an answer), `cancel`, `bulk_cancel`, `expire`. Each: CAS writer → one audit row per transition (`answered` · `cancelled` (+`has_reason`) · `bulk_cancel` (+`batch_id`, the cancelled ids) · `expired` (`source=system`); ids and enums only) → ONE thin trigger (`operator_queue_responded` / `operator_queue_cancelled` agent-keyed; `operator_queue_cleared` count-only for a sweep; expiry sends none — the poll cycle's one `operator_queue_sync` covers it, its reset now sits above expiry) → the ending observers (`register_ending_observer`), handed only the CAS-won rows. Synchronous: the portal's answer route is a plain `def` on a worker thread, so the audit and broadcast hop to the loop through `operator_resume_service.spawn_on_loop` and are never awaited. `_broadcast_payload` names each trigger so the ent#467 `/ws` guard reads it.

**Only a person ends an ask.** `dependencies.reject_non_person_principal` (an allowlist over `mcp_scope ∈ {None, "user"}`, no `agent_name`/`connector_agent`/`portal_delegate`; a principal with no scope attribute fails closed) guards respond, cancel and bulk-cancel before anything is read → 403 `person_required`. The Workspace answer (`client_portal/asks/router.py`) holds its platform principals to the same rule: `get_portal_principal` sets `PortalPrincipal.is_person` from `is_person_principal`, so a system-scoped key keeps its Workspace read breadth (#2198) but its answer is refused with the same 403 before the row is read.

**The wake (default observer, `_wake_filer`).** Only for agents whose owner opted in (one flag read per agent per event; the spawned work re-reads it at spend time). Answered → the ent#329 `spawn_resume_dispatch`, one per ask, trigger `operator_response`. Cancelled / expired → `spawn_ending_dispatch`: ONE turn per agent per event (a sweep of one agent's 25 asks is one turn), trigger `operator_ending`, idempotency key `operator_ending:{agent}:{sha256(disposition + sorted ids)}`, `source_user_email` = the person (None for timeout). Skipped: platform-minted rows (ent#499), rows the agent already closed (`sync_state = closed_by_filer`), agents that are stopped or missing (audited `operator_resume_dispatch` status `skipped_not_running`; Docker unreadable ⇒ attempted). The framing lists the agent's own request ids + titles; an expiry carries the rider verbatim — "Denied by timeout; do not re-ask the same action without new information." — and the operator's reason is framed as data. A crash between the ending commit and the spawn loses the wake (accepted: the readback and the context line still carry it).

**The agent's read path.** `GET /api/agents/{name}/operator-queue/{request_id}` + MCP `get_my_ask` (see Endpoints), and an `Ended asks (last 24 h)` line in every platform-composed turn's Execution Context (`platform_prompt_service._resolve_ended_asks`: up to 5 request ids + disposition + time, platform alarms excluded, fail-soft). The pull-worker path does not compose the platform prompt (#1629), so pull-claimed turns do not get the line.

**Person fields withheld from machines.** `disposed_by_email` and `resolved_to` are removed from `GET /api/operator-queue`, `/{id}` and `/agents/{name}` for any non-person principal; the pre-existing `responded_by_email` / `addressed_to_email` still pass (registered residual) — except `addressed_to_email` on a NATIVE ask (PR B), which holds the person the platform resolved a role to (the email `resolved_to` also carries) and is withheld as well.

**Surfaces.** ONE rule, `utils/operatorQueue.js::queueEnding(item) → {kind, label, who, when} | null` (+ `queueEndingText`, `queueEndingSortTime`, `recentlyEnded`): ResolvedCard shows who and when (relative, absolute on hover) and the cancel reason; the resolved feed and the list API order ended rows by ending time (#627 AC6); `/m` gets a "Recently ended" strip from the fetch it already made; the Workspace (`include_ended`, read past the operator's Clear All) lists asks that ended in the last 7 days on the agent page and in chat with a coarse who (`you` / `the operator` / `timeout`) — never an operator email or the reason — with no answer controls, while the sidebar count and the Work tab's "Waiting on you" (`pending-only`) stay pending-only; an answered ask stays listed as the server's `answered` projection. The wake opt-in copy (ReliabilityPanel) and the Clear-All confirmation say only what the platform delivers.

**Guards.** `test_ent329_operator_resume.py` G2 re-pinned to the sink: both spawners' only call site is `services/ask_service.py` (separate floors), every function feeding `_ended(` must refuse on `_status_conflict` in an `if` or take its rows from a named set-CAS accessor, and only `_ended` iterates the observers. The #2376 writer guard names the sink as the one writer. The route-level test (pending A + already-cancelled B ⇒ the wake gets only A) and three call-site mutations are in `tests/unit/test_ent611_ask_endings.py`.

**Residuals.** Pre-existing person emails to agent keys on get/list; accepted wake loss on a crash; no native path for ephemeral agents; the pull path lacks the context line; the router's clear-resolved trigger still carries `cleared_by`.

### Raising an ask (trinity-enterprise#611, PR B)

**Why.** Before PR B an agent asked a person by appending to its own `~/.trinity/operator-queue.json` and waiting up to 5 s for the poller: nothing was validated at the call (a malformed entry was clamped or quarantined out of sight), there was no receipt, and nothing answered "did my ask land?". The native path validates at the call, stores, announces and answers with a receipt.

**Entry.** `POST /api/agents/{name}/operator-queue` (`agent_router`, `raise_my_ask`) and the MCP tool `ask_operator` (`src/mcp-server/src/tools/operator_queue.ts`). Self-acting like the readback: `get_self_acting_agent` (agent key == name, system key == `trinity-system`, anything else → 403 `agent_identity_required`); the MCP tool takes the agent from the key and forwards only the schema's declared fields. Ephemeral agents stay fenced (no `_EPHEMERAL_ALLOWED_ROUTES` change) and keep the file. Body `OperatorAskCreate` (`extra="forbid"`): `request_id`, `title`, optional `question`, `type` (approval · question · alert), `priority`, `options`, `context`, `proposal`, `to` (primary · approver · viewer · operator), `expires_at`, `supersedes_expired`.

**The sink entry** — `ask_service.raise_ask(agent_name, ask, *, raised_by, channel, actor_user)`, the seam the gate path (trinity-enterprise#164) calls with `raised_by="gate"`:
1. **Static validation** (`_validated_ask`), every refusal a named 422: `invalid_ask`, `invalid_request_id`, `reserved_request_id`, `invalid_type`, `invalid_priority`, `invalid_title`, `field_too_large` (`field`, `limit`, `unit`), `invalid_question`, `invalid_options`, `options_required`, `invalid_context`, `invalid_proposal`, `invalid_to`, `invalid_expires_at`, `invalid_supersedes_expired`. Sizes are measured by the ingest clamp's own `_json_bytes`; the agent never authors the Workspace thread (`workspace_session_id` is platform-written).
2. **Replay.** The same `request_id` returns the first receipt (`status: replayed`, HTTP 200) with `differs` naming each field that is not the same. It runs before every time-dependent check, so a retry after the clock moved past the deadline floor still gets its receipt.
3. **Deadline floor (C5).** `expires_at` needs a timezone and must be at least 15 minutes out: the bound on the expiry wake's agent-driven self-trigger.
4. **The re-ask link.** `supersedes_expired` must name the agent's OWN expired ask (one uniform 422 otherwise). C6: repeating one of the agent's expired `proposal`s without the link is 422 `reask_requires_link` (with `expired_request_id`).
5. **Addressing.** A registered provider answers first (`assignment_provider.people_for`, optional); otherwise `primary` → the owner's email (and so the owner's Workspace), `operator` → no person. An owner without an email makes it an operator ask with `resolved: false`; `approver` / `viewer` → 422 `role_unassigned` until someone fills them. Several people are recorded in `resolved_to`, but none becomes the single Workspace addressee.
6. **Rate caps.** The #1632 buckets (`operator_queue_create:{agent}` and `_fleet`) → 429 `rate_limited`.
7. **Atomic create** (`db/operator_queue.py::create_native_item`): replay check, pending count and insert under ONE per-agent lock (PostgreSQL `pg_advisory_xact_lock` keyed on the agent, SQLite `BEGIN IMMEDIATE`) → 429 `queue_full` at the depth cap; a lost insert race comes back as a replay.
8. **Side effects.** One `raised` audit row (ids and enums only; `source` api or system), attributed to the AGENT (`actor_type` agent — never the owner its key resolves to) with the presented key and the owner's email carried explicitly (the ent#614 rule), and a thin `operator_queue_new` `{id, agent_name}` spawned on the loop (the store refetches).

**The row.** `channel=mcp`, `raised_by=agent`, `to_role`, `resolved_to`, `proposal`, `supersedes_expired` (the predecessor's uuid), `delivery_state=not_applicable` / `delivery_detail=mcp_raised`. It sits outside the file contract everywhere (`_file_contract()` = `channel` NULL or `file`): the sync index lists its id as `foreign`, the responded and terminal write-backs and both sweeps never touch it, and Clear All hides it once answered, since there is nothing left to deliver.

**The receipt.** `status`, `id`, `request_id`, `channel`, `type`, `to_role`, `resolved`, `ask_status`, `disposition`, `disposed_at`, `expires_at`, `wakes_on_ending` (the owner's wake opt-in), `supersedes_expired` (the predecessor's `request_id`), plus `differs` on a replay. It names the ROLE, never an email — and so do the reads: on a native row `addressed_to_email` is withheld from every non-person principal, like `resolved_to`.

**The file channel.** Kept for two releases. The poller skips a file entry re-using a native id before any branch reads it (logged once per agent and id), creates through `create_operator_queue_item_with_outcome` and never counts, announces or audits a row it did not insert, and logs the file channel's deprecation (naming `ask_operator`) on the first ingest per agent per process.

**Surfaces.** `utils/operatorQueue.js::queueReaskBadges`: a re-ask names the expired ask it re-raises ("Re-ask of …") and the expired ask names its successor ("Re-asked as …"), on QueueCard and ResolvedCard; a re-ask whose predecessor is not loaded still says it is one. A native row carries no sync badge (there is no file to be out of sync with), and the Workspace reads it as `confirmed`.

**Guards.** G3 (`test_1677_operator_alert_emitters.py`) allowlists `raise_ask` and the two new facade methods with a justification; `test_1402_prompt_contract.py` pins that the prompt teaches `ask_operator` by its bare name before the file.

### Ingestion caps (#1632)

The agent-authored sync boundary is the only untrusted operator-queue producer (there is **no HTTP create** — every item flows through `db.create_operator_queue_item`, and platform callers create directly, bypassing `_sync_agent`; since #1677 that exemption is split into platform-only emitters, which stay direct, and agent-influenceable ones, which are budgeted — see "Platform exemption & emitter budget" below). #1402 makes this queue the approval channel for irreversible actions, so a compromised / prompt-injected agent flooding plausible "approve this" items causes operator fatigue → reflexive approval. The bound must stop a **hostile** agent (a rate-aware attacker), not just a runaway. All limits are env-tunable and generous by design (requirements §26.7).

Two independent bounds plus field hygiene, applied in-order per new `pending` request:

1. **DEPTH cap (primary, DB-measured ⇒ Redis-independent).** `pending = db.count_operator_queue_pending_for_agent(agent)` is computed once per cycle; a new item is admitted only while `pending + admitted < OPERATOR_QUEUE_MAX_PENDING_PER_AGENT` (25). At the cap the scan **stops** (`break`) — it does not keep dripping a growing file (that would re-scan on every 5s cycle → per-cycle DoS). Because it is DB-measured it holds through a Redis outage, which is why fail-open on the rate limiters is safe. Depth is the fatigue metric: a rate-aware drip that never trips the rate limit still can't accumulate more than 25 pending items.
2. **RATE cap (burst smoothing, Redis, fail-open).** Per-agent `rate_limiter.check("operator_queue_create:{agent}", 60, 60)` + fleet `check("operator_queue_create:_fleet", 300, 60)`, charged only at the real create point. Denied → the item is **held** this cycle and the scan `break`s (the window is monotonic within a cycle). The fleet cap bounds a colluding / shared-upstream-injected fleet in aggregate.
3. **Field hygiene** (`_clamp_ingested_item`, a total helper run **inside** the #1525 create try/except so any failure is quarantined, never hot-looped): `title`→300 / `question`→4000 chars **truncate-with-marker** (losing a real approval is worse than a clamped one); `context` serialized >8 KiB or non-serializable → `{"_truncated":true,"_original_bytes":N,"execution_id":<validated ≤128 & id-shaped, else null>}` (so a huge `execution_id` can't smuggle past the context cap); non-dict `context` → `{}`; `options` >4 KiB → a small dropped-marker list; `priority` validate-only (unknown → `medium`, legit `critical` untouched — the depth cap already bounds critical *volume*); **`created_at` normalized to ingest time** (an agent-supplied future date would pin the item atop the `created_at DESC` sort). `expires_at` is honored as authored.
4. **Reserved-id guard + malformed-id reject.** An agent item whose `id` starts with a platform-reserved prefix (`queue-flood-`, `poison-`, `cb-dormant-`, `sync-failing-`, `git-bloat-`, `skill-not-found-`, `val_`, `system-seed-`, `base-image-stale-`, `alert-budget-` (#1677), `sub-headroom-` (ent#434), `skills-legacy-adoption-` (#2744)) is **rejected** so an agent can't pre-create — and via `create_item`'s `on_conflict_do_nothing` silence — its own flood alarm or the #1402 poison alert. An `id` >256 chars or not matching `^[A-Za-z0-9._:-]+$` is rejected (a create PK can't be safely rewritten).
5. **Flood alert.** A depth-held / rate-skipped / malformed-id / oversize-file episode emits **one** `type:"alert"` item per episode via a platform **direct-DB create** (exempt), id `queue-flood-{agent}-{utc_now_iso()}` (**un-guessable** — the agent can't pre-suppress it), priority `high`, softened wording, in-memory `OPERATOR_QUEUE_FLOOD_ALERT_COOLDOWN_SECONDS` (300) cooldown, wrapped so an emit failure never kills the sync. The single leader ⇒ no WS double-broadcast.

**Deferred/held semantics.** Held items are **not dropped** — they stay `pending` in the agent's file and are re-evaluated on a later cycle once depth/rate frees up (or after the operator resolves existing items). This is a behavior change agents must understand: an agent-supplied `created_at` is ignored, and a burst beyond the caps is throttled rather than fully ingested at once.

**Generous DB-sink belt.** `create_item` independently rejects (`ValueError`) `title`>4 KiB / `question`>16 KiB / serialized `context`>64 KiB / `id`>512 — an order of magnitude above the service caps so a clamped agent item or a small platform item never trips it, but the "platform bypasses the boundary" exemption stops being *solely* load-bearing (#1525's validate-at-boundary-AND-at-sink).

**Platform exemption & emitter budget (#1677).** Platform callers create straight through `db.create_operator_queue_item`, bypassing `_sync_agent` and therefore every file-seam cap — but the exemption is **split by influence, not by caller location**. A *platform-only* emitter — volume bound by platform cadence (edge-triggered, idempotent/bucketed id, leader-locked, or operator-driven): lease-reaper poison-park #1402, `agent_client` circuit-breaker-dormant, `sync_health_service` git-sync/bloat, the skills / system-seed / base-image / retention-guard / portal-inbox alarms (of which `skill_service._record_adoption_failure` carries its bound **per branch** since #2744: the terminal *"this install already has sources"* refusal is `low` with an idempotent URL-keyed id (`skills-legacy-adoption-refused-{sha256(url)[:12]}`, family prefix reserved) ⇒ **≤1 row per refused URL** with an operator's dismissal sticking, while its two genuine failure branches stay `high` and timestamped by product decision. That bound is **not** the "operator-driven" disjunct of the list above — its `_ALLOWED_CALLERS` entry claimed "admin-driven sync cadence" until #2744, and that was false: ent#236's auto-sync drives the same `sync_library()` unattended on a 300s-86400s timer, so what makes it platform-only is that its ONLY input is `skills_library_url`, which `routers/settings.py` blocks on the generic settings PUT (`LEGACY_SKILLS_LIBRARY_KEYS`, 422) and no other writer reaches), `subscription_headroom_alerts._emit` (ent#434 — edge-triggered per weekly window via a deterministic reset-day id whose on-conflict makes a re-emit a no-op, plus a per-cycle cap; no agent input reaches any field), and `validation_service._notify_operator_on_failure` (**converted to a direct DB create in #1632**, which also fixed a latent bug where it appended to a bare list the sync loop can't parse) — stays direct and unthrottled. An *agent-influenceable* emitter — one whose VOLUME an agent can drive: `task_execution_service._alert_skill_not_found` (#1410: one `priority:"high"` item + notification per **distinct** unknown slash-command, at $0/turn — the per-command dedup and timestamped id never collapse distinct commands) and `git_service._emit_gitignore_untracked_alert` (#2529: the per-Push `.gitignore` sweep names what it untracked **and what it newly un-ignored** — the gate is `GitignoreSweep.changed_tracking`, both directions, because the rebuild that stops a managed default reversing an agent negation can also newly un-ignore a path the same Push then commits; and `sync_to_github` is reachable from the `git_sync` MCP tool, which an agent-scoped key may call on itself — a repeated `git add -f <ignored>` + sync loop yields a fresh removed set every time against a timestamped, non-idempotent id, so nothing upstream bounds it; note its `git_bloat`/`sync_failing` SIBLINGS stay direct, because those fire on the 60-second poller's cadence, which is the whole influence-not-location distinction) — routes through `operator_queue_service.create_bounded_alert`: a per-(agent, registered-type) pending-DEPTH budget (`OPERATOR_ALERT_MAX_PENDING_PER_TYPE`, default 5; DB-measured ⇒ Redis-independent, the #1632 primary-bound mirror; deliberately no rate cap — depth is rate-independent, a faster spray only reaches the cap sooner). The budget type is derived from `item["type"]` against the `_BUDGETED_ALERT_TYPES` frozenset registry (registering a type is a one-line reviewed act) and **every arm is fail-closed**: an unregistered type, an unreadable count, or a failed create suppresses the ALERT only — the FAILED/`SKILL_NOT_FOUND` execution rows stay the primary observability surface, and the caller gates the paired notification on the same bool so it can never outlive its queue item. Ordering in `_alert_skill_not_found` is pinned **dedup → budget → both creates** (a benign repeat of an already-pending command at cap stays a silent no-op, never a false "compromised" signal), and the agent-derived command is truncated (~200 chars, marker) on every echo surface. At the cap, ONE cooldown-gated (`OPERATOR_QUEUE_FLOOD_ALERT_COOLDOWN_SECONDS`) episode alert per window with the deterministic reserved id `alert-budget-{agent}-{type}-b{bucket}` — the bucket makes the `(agent_name, request_id)` on-conflict target a cross-worker dedup backstop; the alert carries no `held` count and no agent-controlled text (context = `reason`/`alert_type`/`cap`[/`last_triggered_by` platform enum]). The classification is **CI-forced**: the AST caller-parity guard `tests/unit/test_1677_operator_alert_emitters.py` fails any unclassified `create_operator_queue_item` call site (both facade spellings), and its `_ALLOWED_CALLERS` dict is the **living caller inventory** — (path, qualname) + a one-line justification per entry (OSS tree only; the enterprise submodule owns its own twin). A sink-level default bound inside `create_item` was rejected on fail-direction grounds: it fails QUIETLY at the load-bearing poison-park create (a park happens only on a successful create), where the parity test fails LOUDLY at CI (#1890's lesson deliberately inverted).

### Database Delegation

**File**: `src/backend/database.py` (lines 1940-1989)

`DatabaseManager` delegates to `self._operator_queue_ops` (`OperatorQueueOperations()`):

| DatabaseManager Method | Delegates To | Line |
|----------------------|-------------|------|
| `create_operator_queue_item(agent_name, item)` | `create_item()` | 1944-1945 |
| `get_operator_queue_item(item_id)` | `get_item()` | 1947-1948 |
| `list_operator_queue_items(**kwargs)` | `list_items()` | 1950-1951 |
| `respond_to_operator_queue_item(...)` | `respond_to_item()` | 1953-1957 |
| `cancel_operator_queue_item(item_id)` | `cancel_item()` | 1959-1960 |
| `bulk_cancel_operator_queue_items(ids, accessible_agent_names)` (#1017) | `bulk_cancel_items()` | 1962-1963 |
| `clear_resolved_operator_queue_items(agent_name, accessible_agent_names)` (#1017) | `clear_resolved_items()` | 1965-1969 |
| `get_operator_queue_terminal_for_agent(agent_name, since_hours)` (#1017) | `get_terminal_items_for_agent()` | 1971-1974 |
| `mark_operator_queue_acknowledged(item_id)` | `mark_acknowledged()` | 1976-1977 |
| `mark_operator_queue_expired()` | `mark_expired()` | 1979-1980 |
| `get_operator_queue_stats(**kwargs)` | `get_stats()` | 1982-1983 |
| `get_operator_queue_responded_for_agent(agent_name)` | `get_responded_items_for_agent()` | 1985-1986 |
| `operator_queue_item_exists(item_id)` | `item_exists()` | 1988-1989 |
| `count_operator_queue_pending_for_agent(agent_name, item_type=None)` (#1632; `item_type` #1677) | `count_pending_for_agent()` | — |

The `item_type` pass-through (#1677) is load-bearing: the budget helper swallows every exception, so a facade that dropped the kwarg would silently void the whole budget — pinned by `test_1677_operator_alert_budget.py`'s real-facade test.

---

## Data Layer

### Database Table

**Table**: `operator_queue` (in `src/backend/db/schema.py:981-1005`)

```sql
CREATE TABLE IF NOT EXISTS operator_queue (
    id TEXT PRIMARY KEY,
    agent_name TEXT NOT NULL,
    type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    priority TEXT NOT NULL DEFAULT 'medium',
    title TEXT NOT NULL,
    question TEXT NOT NULL,
    options TEXT,                    -- JSON array for approval type
    context TEXT,                    -- JSON object metadata from agent
    execution_id TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    response TEXT,
    response_text TEXT,
    responded_by_id TEXT,
    responded_by_email TEXT,
    responded_at TEXT,
    acknowledged_at TEXT,
    cleared_at TEXT,                 -- #1017: NULL = visible; set = hidden by Clear All (Resolved tab)
    FOREIGN KEY (responded_by_id) REFERENCES users(id)
)
```

**Migration** (#1017): `operator_queue_cleared_at` (`src/backend/db/migrations.py:_migrate_operator_queue_cleared_at`, line 2386-2402) — `ALTER TABLE operator_queue ADD COLUMN cleared_at TEXT` via `_safe_add_column`. Rationale (from the migration docstring): clearing the Resolved tab **hides** rows rather than deleting them — a DELETE would let the 5s sync loop resurrect any item whose agent-file entry still says 'pending' (always true for expired items, which are never written back, and for cancelled items whose status flip hasn't been written back yet). Actual deletion is the retention sweep's job (#1142).

**Indexes** (`src/backend/db/schema.py:1313-1317`):
```sql
CREATE INDEX IF NOT EXISTS idx_operator_queue_agent ON operator_queue(agent_name);
CREATE INDEX IF NOT EXISTS idx_operator_queue_status ON operator_queue(status);
CREATE INDEX IF NOT EXISTS idx_operator_queue_priority ON operator_queue(priority);
CREATE INDEX IF NOT EXISTS idx_operator_queue_type ON operator_queue(type);
CREATE INDEX IF NOT EXISTS idx_operator_queue_created ON operator_queue(created_at DESC);
```

### Database Operations

**File**: `src/backend/db/operator_queue.py` (495 lines) -- `OperatorQueueOperations` class

| Method | Line | Description |
|--------|------|-------------|
| `_row_to_item(row)` | 19-42 | Convert DB row (19 columns) to dict, JSON-parses options and context. `cleared_at` is `row[18]` — it must stay positionally LAST in `_SELECT_COLS` (line 44-50, #1017) |
| `create_item(agent_name, item)` | 52-88 | INSERT OR IGNORE from agent JSON data. Extracts execution_id from `context.execution_id` |
| `get_item(item_id)` | 90-102 | SELECT single item by ID (no `cleared_at` filter) |
| `list_items(..., include_cleared=False)` | 104-177 | Filtered list with dynamic WHERE clauses. `include_cleared: bool = False` (#1017) — cleared rows (`cleared_at IS NOT NULL`) are excluded by default; only listing honors this — `get_item` and the sync-service accessors never filter on `cleared_at`, and the Workspace's ended-asks listing passes `include_cleared=True` (Clear All is the operator's hygiene, #611). `hide_ended_before` (#611) drops rows that ended before a cutoff. Sort: pending first by priority then created_at DESC; ended rows by `coalesce(disposed_at, responded_at, created_at)` DESC (#611, #627 AC6) |
| `respond_to_item(...)` | 179-221 | UPDATE status=responded WHERE status=pending. Sets response, responded_by_id, responded_by_email, responded_at. **Race marker (#1017)**: `rowcount == 0` with an existing row means the item left 'pending' between the router's check and the UPDATE — returns the current item with `_status_conflict: True` so the router can 409 instead of returning a silent 200 |
| `cancel_item(item_id)` | 223-239 | UPDATE status=cancelled WHERE status=pending |
| `bulk_cancel_items(ids, accessible_agent_names)` (#1017) | 241-280 | Single UPDATE status=cancelled WHERE status=pending AND id IN (...). Tri-state scoping: `None` = no filter (admin), empty set = no-op (returns 0), non-empty = SQL-side `agent_name IN (...)`. Returns rowcount |
| `clear_resolved_items(agent_name, accessible_agent_names)` (#1017) | 282-327 | UPDATE SET cleared_at=now WHERE status IN ('acknowledged','cancelled','expired') AND cleared_at IS NULL. A **hide flag, NOT a DELETE** — the 5s sync loop re-creates any DB-missing item whose agent-file entry still says 'pending' (always true for expired items, and for cancelled items whose flip hasn't been written back yet), so a DELETE would resurrect them; actual deletion is the retention sweep's job (#1142). `responded` rows are intentionally kept (sync service still has to deliver the answer). Same tri-state scoping; optional `agent_name` narrows further. Returns rowcount |
| `mark_acknowledged(item_id)` | 329-340 | UPDATE status=acknowledged WHERE status=responded. Sets acknowledged_at |
| `mark_expired()` | 342-358 | UPDATE status=expired WHERE status=pending AND expires_at < now. Returns count |
| `get_stats()` | 360-445 | Aggregate counts by status, type (pending), priority (pending), agent (pending). Calculates avg_response_seconds and responded_today |
| `get_pending_item_ids()` | 447-452 | SELECT id WHERE status=pending |
| `get_responded_items_for_agent(agent_name)` | 454-466 | SELECT WHERE agent_name=? AND status=responded (for sync service write-back) |
| `get_terminal_items_for_agent(agent_name, since_hours=168)` (#1017) | 468-488 | SELECT WHERE agent_name=? AND status IN ('cancelled','expired') AND created_at >= `iso_cutoff(since_hours)` — created_at-bounded (no per-status timestamp column) so the per-agent 5s sync query stays cheap. Deliberately NOT filtered on `cleared_at` — hidden items still need their flip delivered. Feeds the terminal-status write-back |
| `item_exists(item_id)` | 490-495 | SELECT 1 existence check |

**List query sort order** (line 156-169):
```sql
ORDER BY
    CASE status WHEN 'pending' THEN 0 ELSE 1 END,
    CASE priority
        WHEN 'critical' THEN 0
        WHEN 'high' THEN 1
        WHEN 'medium' THEN 2
        WHEN 'low' THEN 3
        ELSE 4
    END,
    created_at DESC
LIMIT ? OFFSET ?
```

---

## Agent Protocol

### File Format

Agents write to `~/.trinity/operator-queue.json`:

```json
{
  "$schema": "operator-queue-v1",
  "requests": [
    {
      "id": "approval-<execution_id>-deploy",
      "type": "approval",
      "status": "pending",
      "priority": "high",
      "title": "Short summary",
      "question": "Full description. Markdown supported.",
      "options": ["approve", "reject"],
      "context": { "key": "value" },
      "created_at": "2026-03-07T10:00:00Z",
      "expires_at": "2026-03-09T10:00:00Z"
    }
  ]
}
```

**Request ids are per-agent scoped (#1631, shipped)** — the item `id` is now a platform-minted `uuid.uuid4().hex` (the global row handle; REST/MCP/frontend lookups unchanged), and the agent-authored correlation string lives in a nullable `request_id` column with `UNIQUE(agent_name, request_id)`. `create_item` mints the uuid, stores `request_id`, and targets `on_conflict_do_nothing` at `(agent_name, request_id)` — a same-agent re-insert stays idempotent (re-reads and returns the surviving row's uuid) while two agents reusing the same string no longer collide (previously the second agent's item was silently swallowed by the global PK). The sync loop's `item_exists`, `mark_acknowledged`, the `_create_failures` quarantine map, and `_write_responses_to_agent` are all keyed on `(agent_name, request_id)`, closing the cross-agent write where agent B's acknowledgement flipped agent A's row. A guard rejects (log-once, bounded) agent-authored ids using reserved platform prefixes so an agent can't pre-claim the platform's own alert ids (the guarded prefix set is extended by #1632 — see the ingestion caps below). The #1402 contract still mandates execution-id-derived ids (`approval-{execution_id}-{slug}`), which make a re-park under pull-mode re-delivery idempotent.

**Ingestion caps (#1632, shipped).** Agent field values are bounded at ingestion: `title` ≤300 chars and `question` ≤4000 chars are truncated-with-marker; `context` >8 KiB and `options` >4 KiB collapse to a marker; an unknown `priority` becomes `medium`; **`created_at` is ignored and set to ingest time** (a future date can no longer pin the item atop the sort); the agent-authored `request_id` must be ≤256 chars and match `^[A-Za-z0-9._:-]+$`, and must **not** use a platform-reserved prefix (`queue-flood-`/`poison-`/`cb-dormant-`/`sync-failing-`/`git-bloat-`/`skill-not-found-`/`val_`/`system-seed-`/`base-image-stale-`/`alert-budget-`/`db-backup-`/`log-archive-`/`sub-headroom-`/`gitignore-untracked-`/`skills-legacy-adoption-`) or it is rejected. Per-agent pending depth is capped (default 25) and creation is rate-limited (60/60s per agent, 300/60s fleet); items over the cap are **held** (left `pending` in the file, re-evaluated on a later cycle), not dropped, and trigger one aggregated `queue-flood-*` operator alert. See "Ingestion caps (#1632)" under the Sync Service. These bounds apply **only** to the agent-authored file path — platform-created items (poison-park, validation alerts, internal breaker/skill/git alerts, the #1816 system-agent base-image staleness / start-failure alarms) bypass them; since #1677 the *agent-influenceable* platform emitters (`skill_not_found`, and `gitignore_untracked` since #2529) are additionally bounded by a per-(agent, type) pending budget via `create_bounded_alert`, with an `alert-budget-*` episode alert at the cap (see "Platform exemption & emitter budget (#1677)").

### Async contract — fire-and-park (#1402)

Operator communication is **asynchronous by design**: an agent parks a request and **ends its turn** — it never polls or blocks in-turn waiting for a human (a blocked turn would pin a worker/slot for its whole timeout budget). Responses are written back to the agent's file within ~5s of the operator responding (running agents only) and are processed at the start of a **later** turn. Consequences the contract documents to agents (platform prompt + `docs/TRINITY_COMPATIBLE_AGENT_GUIDE.md`):

- Agents with no future turn (one-shot triggers, no schedule/heartbeat) must embed resume instructions in the request; the respond→re-trigger dispatch path is a deferred follow-up (#1630).
- `expires_at` is recommended on gating requests; `expired` ⇒ "not approved — do not proceed".
- Irreversible-and-un-confineable effects (agent's own keys / `gh` / `curl`) are gated by parking an `approval` FIRST — an authored, honor-system contract (the agent owns its queue file), not a security boundary.

### Platform-created items: poison-park (#1081 Phase 3 / #1402)

The lease reaper (`services/lease_reaper_service.py`) creates operator-queue items **directly in the DB** (no agent-file leg) when a pull-claimed task exceeds `MAX_REDELIVERY`: type `alert`, priority `high`, id `poison-{execution_id}` (idempotent via `on_conflict_do_nothing`), created BEFORE the row is CAS-failed (alert-first ordering — a crash between the two leaves the row re-findable rather than invisibly parked). The sync service's write-back path delivers responses for DB-only items the same as file-originated ones.

### Status Lifecycle

```
Agent creates -> pending -> responded (by a person) -> acknowledged (by agent)
                         -> cancelled (by a person — single cancel or bulk Clear All)
                         -> expired (by the platform, once expires_at passed)
```

Since trinity-enterprise#611 the same UPDATE writes the endings ledger: `disposition` answered | cancelled | expired, `disposed_by` person | timeout, `disposed_at`, and for a cancel the optional `disposition_reason` (+ `batch_id` for a sweep). `acknowledged` is a delivery fact, not an ending — its row keeps `disposition = answered`.

Cancellations and expirations are propagated back to the agent on the next sync cycle (#1017): still-`pending` entries in the agent's file are flipped in place to their terminal DB status (`cancelled` or `expired`) so the agent stops waiting on them (entries missing from the file are never appended — there is nothing to deliver).

### Prompt Integration

The "Operator Communication" instructions agents receive — `ask_operator` / `get_my_ask` by their bare names (trinity-enterprise#611), the three request types, the #1402 fire-and-park contract, derived request ids, the deadline and re-ask rules, and the queue file as the two-release fallback with its schema and hygiene — live in `src/backend/services/platform_prompt_service.py` (`PLATFORM_INSTRUCTIONS`), injected per invocation via `--append-system-prompt` on the push dispatch path. Sentinel phrases are test-locked by `tests/unit/test_1402_prompt_contract.py`.

**Historical note**: the old file-based injection (`config/trinity-meta-prompt/prompt.md` → agent-side `/api/trinity/inject` with staleness re-injection) was removed in #136 — `docker/base-image/agent_server/routers/trinity.py` now serves only a static status endpoint. `prompt.md` remains as a reference copy kept in sync with the canonical section (parity-checked by the same test); deleting it is a candidate cleanup. Pull-claimed turns compose the same instructions via `services/pull_coordination_service.py` (#1629, fix #1633 — fail-open: a composition error runs the turn with the caller prompt only rather than blocking the claim).

---

## Side Effects

- **WebSocket broadcast**: `operator_queue_new` -- When new items are synced from agents (sync service). Also carries the #1632 `queue-flood-{agent}-{utc_now_iso()}` alert emitted (once per cooldown episode, from the single leader worker) when an agent's ingestion is depth-held / rate-skipped / oversize — a platform direct-DB create, exempt from the caps
- **Operator-queue item** (#1632): a `type:"alert"`, priority `high` `queue-flood-*` summary item created directly in the DB when an agent exceeds its per-agent ingestion depth/rate limit or writes an oversized queue file; un-guessable id so the agent can't pre-suppress it; one per `OPERATOR_QUEUE_FLOOD_ALERT_COOLDOWN_SECONDS` episode
- **Operator-queue item** (#1677): a `type:"alert"`, priority `high` `alert-budget-{agent}-{type}-b{bucket}` episode item created directly in the DB when an agent hits its per-(agent, type) platform-alert budget; deterministic bucketed reserved id (DB on-conflict = cross-worker dedup, prefix reserved so the agent can't pre-suppress it); no `held` count, no agent-controlled text; one per `OPERATOR_QUEUE_FLOOD_ALERT_COOLDOWN_SECONDS` window per (agent, type); also broadcast as `operator_queue_new` (best-effort)
- **WebSocket broadcast**: `operator_queue_responded` -- When operator submits response (router, line 255-264)
- **WebSocket broadcast**: `operator_queue_acknowledged` -- When agent acknowledges response (sync service, line 173-184)
- **WebSocket broadcast**: `operator_queue_cleared` (#1017) -- One event per bulk-cancel (`scope: "pending"`) or clear-resolved (`scope: "resolved"`) operation; all connected clients refetch
- **Audit log** (#1017): `AuditEventType.OPERATOR_QUEUE` with `event_action="bulk_cancel"` (details: cancelled/skipped counts + the ids actually cancelled + `batch_id` + `has_reason`, #611) or `event_action="clear_resolved"` (details: cleared count + agent_name) -- only when the operation actually touched rows
- **Audit log** (trinity-enterprise#611): `answered`, `cancelled` (`has_reason`), `expired` (`source=system`) — one row per transition from the ask sink; `operator_resume_dispatch` (`EXECUTION`) now also records ending wakes (`queue_item_ids`, `disposition`, `batch_id`, status incl. `skipped_not_running`)
- **Execution** (trinity-enterprise#611): an opted-in agent is woken on ANY ending — trigger `operator_response` for an answer, `operator_ending` for a cancel or an expiry (one per agent per event)
- **File write**: Response data (and #1017 terminal-status flips, cancelled/expired) written back to agent container JSON via `AgentClient.write_file()` (sync service, line 267-286)
- **NavBar badge**: Updates pending count in real time via WebSocket events and polling

---

## Error Handling

| Error Case | HTTP Status | Message | Source |
|------------|-------------|---------|--------|
| Item not found | 404 | "Queue item not found" | `operator_queue.py:215,230,281` |
| Respond to non-pending | 400 | "Cannot respond to item with status '{status}'" | `operator_queue.py:235-239` |
| Respond/cancel race (#1017) | 409 | "Item is no longer pending (now '{status}') — response was not recorded" (cancel: "— it was not cancelled") | via the `_status_conflict` DB marker, raised by the ask sink as `AskConflict` |
| Answer after the deadline (#611) | 409 | `{code: "expired", message}` | respond CAS predicate `expires_at > now` |
| Not a person (#611) | 403 | `{code: "person_required", message}` | `dependencies.reject_non_person_principal` (respond / cancel / bulk-cancel) |
| Readback by anyone but the agent itself (#611) | 403 | `{code: "agent_identity_required", message}` | `dependencies.get_self_acting_agent` |
| Cancel reason > 500 chars (#611) | 422 | Validation error | Pydantic `OperatorCancel` / `BulkCancelRequest` |
| Cancel non-pending | 400 | "Cannot cancel item with status '{status}'" | `operator_queue.py:286-290` |
| Inaccessible agent | 403 | "Access denied" | `_assert_agent_accessible()` (`operator_queue.py:73-76`) |
| Bulk-cancel ids out of bounds (#1017) | 422 | Validation error (`ids` 1-500 items) | Pydantic `BulkCancelRequest` |
| Missing response body | 422 | Validation error (Pydantic) | FastAPI automatic |
| Unauthenticated | 401 | "Not authenticated" | `get_current_user` dependency |
| Invalid JSON in agent file | -- | Logged as warning, skipped | `operator_queue_service.py:124` |
| Agent unreachable | -- | Silently skipped (no log) | `operator_queue_service.py:109-111` |
| Write-back failure | -- | Logged as warning/error | `operator_queue_service.py:281-286` |

Note: bulk-cancel never errors on individual ids — non-pending or inaccessible ids are silently skipped and reported in the `skipped` count. An empty accessible-agent set (zero-agent user) is a no-op (`cancelled: 0` / `cleared: 0`), not a 403.

---

## Testing

### Test Files

- `tests/test_operator_queue.py` -- registered in `tests/registry.json`
- `tests/test_ops_clear_all.py` (#1017, 20 tests) -- registered in `tests/registry.json`
- `tests/unit/test_ent611_ask_endings.py` (trinity-enterprise#611 PR A, endings) and `tests/unit/test_ent611_native_ask.py` (PR B: the atomic native create, the file-contract exclusion, `raise_ask`, replay, re-asks, caps, the route and the poller's side of the seam)
- `src/mcp-server/src/operator_queue.test.ts` + `src/mcp-server/src/access-wiring.test.ts` (`get_my_ask`, `ask_operator`: identity from the key, the field pick, named refusals)
- `src/frontend/tests/unit/operatorQueueReask.spec.js` (the re-ask badges, mounted)

### Test Categories

`test_operator_queue.py`:
- **Authentication** (6 tests): All endpoints require JWT auth
- **List items** (10 tests): Structure, pagination, filters, validation
- **Get item** (2 tests): Found and not found cases
- **Stats** (6 tests): Response structure and field types
- **Respond** (5 tests): Happy path, already responded, not found, validation
- **Cancel** (3 tests): Happy path, already responded, not found
- **Agent items** (5 tests): Per-agent queries, filters, empty results

`test_ops_clear_all.py` (#1017):
- **TestClearAllAuthentication**: bulk-cancel / clear-resolved / notifications dismiss-all require JWT
- **TestBulkCancel**: only listed pending ids cancelled, skipped counting, ids validation (1-500)
- **TestRespondConflictMarker**: respond after cancel returns 409, response not recorded
- **TestClearResolved**: hides acknowledged/cancelled/expired (sets `cleared_at`, rows drop out of listings), keeps responded + pending, optional agent_name filter, idempotency
- **TestDismissAllNotifications**: Notifications-tab Clear All counterpart (`POST /api/notifications/dismiss-all`)
- **TestClearAllAccessControl**: tri-state accessible-agent scoping (admin = unfiltered; empty set = no-op)

### Prerequisites

- Backend running at `http://localhost:8000`
- Admin user authenticated
- Queue items present (seeded by sync service or direct DB insert)

### Test Steps

1. **List items**: `GET /api/operator-queue` -> 200 with `{items, count}`
2. **Filter by status**: `GET /api/operator-queue?status=pending` -> only pending items
3. **Get stats**: `GET /api/operator-queue/stats` -> `{by_status, by_type, ...}`
4. **Respond to item**: `POST /api/operator-queue/{id}/respond` -> 200, status=responded
5. **Cancel item**: `POST /api/operator-queue/{id}/cancel` -> 200, status=cancelled
6. **Bulk cancel** (#1017): `POST /api/operator-queue/bulk-cancel` with `{"ids": [...]}` -> 200 `{cancelled, skipped}`; listed pending items become cancelled
7. **Clear resolved** (#1017): `POST /api/operator-queue/clear-resolved` -> 200 `{cleared}`; acknowledged/cancelled/expired rows get `cleared_at` set and drop out of listings (rows are NOT deleted — retention sweep #1142), responded rows remain visible

---

## Complete Data Flow

### Flow 1: Agent Creates Request -> Operator Sees It

```
1. Agent writes ~/.trinity/operator-queue.json with new request (status=pending)
2. OperatorQueueSyncService._poll_cycle() runs every 5s
3. _sync_agent() reads file via AgentClient.read_file()
4. New item detected (not in DB) -> db.create_operator_queue_item()
5. WebSocket broadcast: {type: "operator_queue_new", data: {...}}
6. websocket.js dispatches to operatorQueueStore.handleWebSocketEvent()
7. Store calls fetchItems() -> GET /api/operator-queue
8. Operations.vue re-renders with new card
9. NavBar badge count updates via computed pendingCount
```

### Flow 2: Operator Responds to Request

```
1. User clicks option button or types answer in QueueCard.vue
2. submitApproval() / submitAnswer() calls store.respondToItem()
3. POST /api/operator-queue/{id}/respond -> router respond_to_queue_item()
4. Router validates item exists and status=pending
5. db.respond_to_operator_queue_item() -> UPDATE status=responded
6. Router broadcasts WebSocket: {type: "operator_queue_responded", data: {...}}
7. Store optimistic update: item.status = 'responded'
8. Store auto-advances: expands next open item
9. Next sync cycle: _sync_agent() finds responded items via db.get_operator_queue_responded_for_agent()
10. _write_responses_to_agent() updates matching items in agent JSON, or reconstructs missing items from DB
11. Agent reads updated JSON, processes response, sets status=acknowledged
12. Next sync cycle detects acknowledged -> db.mark_operator_queue_acknowledged()
13. WebSocket broadcast: {type: "operator_queue_acknowledged", data: {...}}
```

### Flow 3: Item Expiration

```
1. Agent creates request with expires_at field (stored ISO-Z, trinity-enterprise#611)
2. OperatorQueueSyncService._poll_cycle() resets _changed_this_cycle, then calls ask_service.expire()
3. db.mark_operator_queue_expired(): bounded candidate select, then per-id CAS
   UPDATE status=expired, disposition=expired, disposed_by=timeout WHERE status=pending
4. The sink audits `expired` per row (source=system) and hands the rows to the observers:
   the ent#329 wake sends each opted-in, running filer ONE `operator_ending` turn carrying
   "Denied by timeout; do not re-ask the same action without new information."
5. The cycle's ONE thin `operator_queue_sync` trigger announces it; the write-back flips
   the agent's file entry to expired
6. Meanwhile, an answer to a row past its deadline but not yet swept is 409 `expired`
```

### Flow 4: Response Delivery After Container Restart

```
1. Operator responds to item while agent is running (response stored in DB as status=responded)
2. Agent container restarts -- filesystem wiped, ~/.trinity/operator-queue.json lost
3. Next sync cycle: _sync_agent() reads file -> not found (file_exists=False)
4. Service creates empty queue_data instead of returning early
5. db.get_operator_queue_responded_for_agent() finds responded items in DB
6. _write_responses_to_agent(file_exists=False) called
7. No items in agent's requests array -> all responded items are "missing" from seen_ids
8. Reconstruction loop appends full request entries (from DB data) to requests array
9. client.write_file() writes new operator-queue.json to agent
10. Agent server's files.py:360 creates .trinity/ directory automatically (mkdir parents=True)
11. Agent reads reconstructed JSON, finds responded items, processes them normally
```

### Flow 5: Clear All on Needs Response (#1017)

```
1. Operator on the Needs Response tab clicks "Clear All" (data-testid ops-clear-all)
2. ConfirmDialog states only what the platform delivers (#611): each agent's queue
   file is marked cancelled (it reads that on its next turn), and running agents set
   to wake when their asks end are woken now, one turn per agent; affects all operators
3. confirmClearAll() -> operatorQueueStore.bulkCancel(openItems ids)
   (only the rendered ids — a sync-race item the operator never saw is untouched)
4. POST /api/operator-queue/bulk-cancel {ids, reason?} (a person only, #611) ->
   ask_service.bulk_cancel() -> db.bulk_cancel_operator_queue_items(): ONE UPDATE
   status=cancelled + the endings ledger + one batch_id WHERE status=pending AND
   id IN (...) [+ agent scoping]; re-select by batch_id = exactly the rows it flipped
5. Audit log (bulk_cancel: batch_id + the ids actually cancelled) + one WS broadcast
   {type: "operator_queue_cleared", data: {scope: "pending", count}} + one ending wake
   per opted-in running agent (trigger operator_ending, #611)
6. All connected clients refetch; cancelled items move to the Resolved tab
   (resolvedItems now includes cancelled/expired), rendered with a gray badge
7. Concurrent respond on a just-cancelled item: router status check passed but the
   UPDATE ... WHERE status='pending' hits 0 rows -> DB returns _status_conflict
   -> router 409 -> store shows "your response was not recorded" and refetches
8. Next sync cycle: get_operator_queue_terminal_for_agent() (168h window,
   cancelled+expired, not filtered on cleared_at) -> still-'pending' entries
   in the agent's operator-queue.json flipped to their terminal status in
   place -> agent stops waiting
```

Clear All on **Resolved** instead calls `clearResolved()` -> `POST /api/operator-queue/clear-resolved` -> sets `cleared_at` on acknowledged/cancelled/expired rows so they drop out of listings (`responded` kept visible for write-back delivery; rows are NOT deleted — a DELETE would be resurrected by the sync loop, actual deletion deferred to retention sweep #1142) -> same WS event with `scope: "resolved"`. Clear All on **Notifications** calls `notificationsStore.dismissAll()` -> `POST /api/notifications/dismiss-all` (broadcasts `notifications_cleared`; see [agent-notifications.md](agent-notifications.md)).

---

## UI Layout

```
+--------------------------------------------------------------------+
| Operations                                                          |
| 3 pending responses, 2 notifications                               |
|                                                                     |
| [Needs Response (3)] [Notifications (2)] [Health*] [Executions]     |
|                                          [Resolved]  [↻ Refresh]    |
| (* Health tab admin-only)                                           |
|                                                                     |
|  === Needs Response tab ===                                         |
| +-- Card (expanded) --------------------+                           |
| | (DA) deploy-agent . 2m ago         X  |                           |
| | Deploy PR #47 to production?          |                           |
| | [Needs approval] [critical]           |                           |
| | ____________________________________  |                           |
| | PR #47 has 47 changed files...        |                           |
| |                                       |                           |
| | > Show details                        |                           |
| |                                       |                           |
| | [Approve] [Reject] [Defer]           |                           |
| | [Add a note...          ] [Send]      |                           |
| +---------------------------------------+                           |
|                                                                     |
|  === Notifications tab ===                                          |
| [Agent v] [Type v] [Priority v] [Status v] [ ] Show dismissed      |
| [Pending: 2] [Acknowledged: 5] [Total: 7] [Agents: 3]             |
| +-- notification row ----------------------+                        |
| | [ ] (!) agent-name . 5m ago              |                        |
| |     [URGENT] Task completed              |   [pending] [✓] [✕]   |
| +------------------------------------------+                        |
+--------------------------------------------------------------------+
```

---

## Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Consolidation | 3-tab unified page | Reduce nav clutter; one place for all agent-to-operator communication. Removed standalone Events and Alerts pages; Cost Alerts tab removed with process engine deletion (#430) |
| Layout | Single-column card feed | Calm, inbox-like -- not a dense operational dashboard |
| Tab naming | "Needs Response" not "Open" | Clearer call-to-action; tells operator what's expected |
| Combined NavBar badge | Sum of queue + notifications | Single badge reduces cognitive load; operator sees total attention items at a glance |
| Critical detection | Any of: critical queue item, urgent notification | Red+pulse badge catches attention for any category of urgent item |
| Legacy route redirects | `/events` redirects to Operating Room; `/alerts` redirect is stale (tab removed) | Original migration; `/alerts` redirect not yet cleaned up after Cost Alerts removal |
| Deep linking | `?tab=` query param | Shareable URLs for specific tabs; no hard page reloads |
| Extracted panels | NotificationsPanel as a component | Reusable; keeps OperatingRoom.vue small; panel owns its own fetch/filter logic |
| Agent identity | Avatar + name on every card | Users associate items with agents, not types |
| Response UX | Inline expand with auto-advance | Process items sequentially like messages |
| Context display | Collapsible "Show details" | Keep cards clean, details on demand |
| Type labels | "Needs approval" / "Question" / "Heads up" | Business-friendly, not technical jargon |
| Alert response | "Got it" button | Low friction acknowledgement |
| Sync direction | Platform polls agents | Agents don't need to know about the platform API |
| Poll interval | 5 seconds (sync service), 10s (queue frontend), 60s (alerts/notifications) | Fast for queue, lighter for background counts |
| WebSocket key | `type` field (not `event`) | Distinct from agent lifecycle events which use `event` field |
| Clear All sends rendered ids (#1017) | Client posts the ids it showed, not "cancel everything pending" | A sync-loop race can never cancel an item the operator never saw |
| Clear Resolved = `cleared_at` hide, NOT a DELETE (#1017) | UPDATE cleared_at on terminal rows; keep `responded` visible until acknowledged | A DELETE would be resurrected by the 5s sync loop — it re-creates any DB-missing item whose agent-file entry still says 'pending', which is always true for expired items (never written back before #1017) and for cancelled items whose flip hasn't propagated. Sync write-back also still has to deliver the operator's answer for `responded` rows. Actual deletion deferred to the retention sweep (#1142) |
| Respond race -> 409, not silent 200 (#1017) | DB `_status_conflict` marker surfaces concurrent cancel | The operator must know their answer was NOT recorded |
| Terminal write-back flips in place (#1017) | Never appends missing entries to the agent file | There is nothing to deliver for a cancelled/expired item; appending would resurrect noise. Flipping also prevents a stale 'pending' file entry from resurrecting the item once its row is eventually purged |

---

## File Inventory

### Backend Files

| File | Lines | Purpose |
|------|-------|---------|
| `src/backend/main.py` | 70, 82, 193-194, 254-258, 290-294, 357 | Router/service imports, WS injection, lifespan start/stop, router registration |
| `src/backend/routers/operator_queue.py` | 311 | REST API endpoints (8 endpoints incl. #1017 bulk-cancel/clear-resolved) |
| `src/backend/services/operator_queue_service.py` | 290 | Background sync service (poll, create, write-back incl. #1017 terminal-status flips, restart resilience) |
| `src/backend/db/operator_queue.py` | 495 | Database operations class (15 methods incl. #1017 bulk_cancel_items/clear_resolved_items/get_terminal_items_for_agent) |
| `src/backend/db/schema.py` | 981-1005, 1313-1317 | Table definition (incl. #1017 `cleared_at`) + 5 indexes |
| `src/backend/db/migrations.py` | 2386-2402 | #1017 `operator_queue_cleared_at` migration (`_migrate_operator_queue_cleared_at`) |
| `src/backend/database.py` | 1940-1989 | Delegation methods (13 methods) |
| `tests/test_ops_clear_all.py` | -- | #1017 test suite (20 tests, 6 classes) |

### Agent Base Image Files

| File | Lines | Purpose |
|------|-------|---------|
| `docker/base-image/agent_server/routers/trinity.py` | 76-91 | Stale meta-prompt detection in `/api/trinity/inject` |
| `docker/base-image/agent_server/routers/files.py` | 358-360 | `write_file` endpoint creates parent directories automatically |

### Frontend Files

| File | Lines | Purpose |
|------|-------|---------|
| `src/frontend/src/views/Operations.vue` | -- | Main page with 5 tabs, `?tab=` deep linking, admin-gated Health tab, combined subtitle, refresh, container-level polling lifecycle (renamed from `OperatingRoom.vue`, #1109) |
| `src/frontend/src/components/MonitoringPanel.vue` | -- | Health tab content (extracted from deleted `views/Monitoring.vue`, #1109) |
| `src/frontend/src/components/ExecutionsPanel.vue` | -- | Executions tab content (extracted from deleted `views/Executions.vue`, #1109) |
| `src/frontend/src/stores/operatorQueue.js` | 239 | Pinia store for operator queue (state, getters, actions incl. #1017 bulkCancel/clearResolved, WS handler, 409 surfacing) |
| `src/frontend/src/stores/notifications.js` | 315 | Pinia store for notifications (filters, bulk actions, polling) |
| `src/frontend/src/components/operator/QueueCard.vue` | 257 | Expandable card with response controls |
| `src/frontend/src/components/operator/ResolvedCard.vue` | 82 | Compact resolved card (incl. #1017 Cancelled/Expired badge) |
| `src/frontend/src/components/operator/NotificationsPanel.vue` | 541 | Notifications tab -- filters, bulk actions, stats, notification list with expand/dismiss/acknowledge |
| `src/frontend/src/components/operator/QueueItemDetail.vue` | 286 | Full item detail panel |
| `src/frontend/src/components/NavBar.vue` | -- | Single "Operations" link + combined badge (2 stores), starts notification polling (#1109: replaced separate Health/Ops/Executions links + Executions running badge) |
| `src/frontend/src/router/index.js` | -- | `/operations` route registration + legacy redirects (`/operating-room`, `/monitoring`, `/executions`, `/events`) (#1109) |
| `src/frontend/src/utils/websocket.js` | 147-149 | Dispatch operator-queue WS events (incl. #1017 `operator_queue_cleared`) to the store |

**Deleted files** (consolidated into Operations, then subsequently removed):
- `src/frontend/src/views/Events.vue` -- Replaced by `NotificationsPanel.vue` in the Notifications tab
- `src/frontend/src/views/Alerts.vue` -- Replaced by `CostAlertsPanel.vue` in the Cost Alerts tab (tab itself later removed in PR #430)
- `src/frontend/src/components/operator/CostAlertsPanel.vue` -- Deleted in PR #430 (process engine deletion)
- `src/frontend/src/stores/alerts.js` -- Deleted in PR #430 (process engine deletion)
- `src/frontend/src/views/Monitoring.vue` -- Deleted in #1109; content extracted to `components/MonitoringPanel.vue` (Health tab)
- `src/frontend/src/views/Executions.vue` -- Deleted in #1109; content extracted to `components/ExecutionsPanel.vue` (Executions tab). `ExecutionDetail.vue` retained (`/agents/:name/executions/:executionId` unchanged)

---

## Not Yet Implemented

### Phase 5: MCP Tools & Polish
- [x] MCP read tools: `list_operator_queue`, `get_operator_queue_item` (#1101); respond (#1104, a person's key only since trinity-enterprise#611); the agent's own readback `get_my_ask` (#611); the native raise `ask_operator` (#611 PR B)
- [x] Batch cancel/clear ("Clear All", #1017); batch **respond** to multiple items still open
- [ ] Activity feed integration (log responses as activities)
- [ ] Sound/desktop notifications for critical items
- [ ] Keyboard shortcuts (j/k navigation, Enter to respond)

---

## Related Flows

- [Agent Notifications](agent-notifications.md) -- Backend notification system (NOTIF-001); UI now embedded as Notifications tab in Operating Room
- [Events Page](events-page.md) -- Former standalone Events page; **consolidated** into Operating Room Notifications tab (view now redirects)
- [Agent Terminal](agent-terminal.md) -- Direct agent interaction
- [MCP Orchestration](mcp-orchestration.md) -- Agent-to-agent communication tools

---

## Revision History

| Date | Change |
|------|--------|
| 2026-09-25 | trinity-enterprise#611 PR B — agents raise asks natively: `POST /api/agents/{name}/operator-queue` + MCP `ask_operator` (self-acting) into `ask_service.raise_ask` (named 422s, replay-first receipts with `differs`, the 15-minute deadline floor, `supersedes_expired` + `reask_requires_link`, role addressing through an optional provider, the #1632 rate buckets, an atomic per-agent depth cap); native rows outside the file contract; the poller skips re-used native ids, counts only inserted rows and logs the file's deprecation; the prompt leads with `ask_operator`; re-ask badges on the cards |
| 2026-09-25 | trinity-enterprise#611 PR A — endings: one transition sink (`services/ask_service.py`) for every way an ask ends; the endings ledger (migration `operator_queue_ask_object` / Alembic `0076`, twelve nullable columns incl. PR B's); person-only endings (403 `person_required`); 409 `expired` for a late answer; cancel audited/raced/woken like respond with an optional reason; bulk returns its `batch_id` and audits the winners; expiry through the sink with the rider; the ent#329 wake on any ending (trigger `operator_ending`, one per agent per event, opted-in running agents only); the agent's own readback (REST + MCP `get_my_ask`) and the Execution Context "Ended asks" line; person fields withheld from machine keys; surfaces show who + when (ResolvedCard, `/m` strip, Workspace ended asks for 7 days); list ordered by ending time. Tests: `tests/unit/test_ent611_ask_endings.py`, re-pinned G2 / #2376 / ent#430 / ent#499 / #2048 guards, `operatorQueueEnding.spec.js`, `portalAskEndings.spec.js`, `mobileAdminRecentlyEnded.spec.js`, MCP `operator_queue.test.ts` + `access-wiring.test.ts`. |
| 2026-09-14 | #2744 the legacy skills-library adoption refusal is bounded by its CLASS. `skill_service._adopt_legacy_clone` runs as the first statement of every `sync_library()`, so an install past migration whose `skills_library_url` matches no configured source filed a fresh `priority:"high"`, `expires_at:None` item on every sync — 17 permanent, operator-unclearable rows (~17% of everything pending) on the reporting install, 288/day at the ent#236 auto-sync floor, because the TIMESTAMPED `request_id` defeats `create_item`'s `(agent_name, request_id)` ON CONFLICT DO NOTHING by construction. The terminal *"already has sources"* branch — the designed resting state of a migrated install, not a failure — now emits at `low` + `logger.info` with a STABLE `skills-legacy-adoption-refused-{sha256(url.strip())[:12]}` id, so N syncs collapse to one row per refused URL and, since that conflict target ignores `status`, an operator's dismissal sticks (cancel rather than acknowledge: Acknowledge writes `responded`, which Clear All deliberately excludes and nothing will ever deliver for a container-less `_skills-sync`). A different refused URL still raises its own item. Shaped as a keyword-only `steady_state` flag on the one emitter (one #1677 `_ALLOWED_CALLERS` key; the `False` default leaves the two actionable call sites unchanged lines, which is the AC-4 proof). `skills-legacy-adoption-` joins `_RESERVED_ID_PREFIXES` — a URL-derived id is guessable, and the same tuple drives `is_platform_minted`, gating the ent#499 responded write-back and the ent#329 respond→resume dispatch. The echoed URL is `strip_url_credentials`-scrubbed: `EmbeddedCredentialError` is a `ValueError`, so the validation-reject branch is exactly the one a PAT-bearing URL reaches, and the raw value had been landing at ERROR in the Vector-captured log and durably in `operator_queue.context` (Invariant #12). The #1677 justification's "admin-driven sync cadence" is corrected. No schema change (#1631 shipped `request_id`) ⇒ Invariant #9 not triggered. Pre-existing rows are not retro-cleaned — `POST /api/operator-queue/bulk-cancel` is the zero-code remedy. Tests: `tests/unit/test_2744_skills_adoption_alert_idempotency.py`. |
| 2026-09-07 | #2529 second budgeted emitter: `git_service._emit_gitignore_untracked_alert` — the per-Push `.gitignore` sweep files a `gitignore_untracked` entry naming what it untracked (both confirmed field incidents were unattended 15-minute cycles that surfaced two months late, so the session-bound surfaces — API response, MCP result, toast, commit message — could not have caught them). Classified as agent-INFLUENCEABLE rather than platform-only, so it routes through `create_bounded_alert`: `sync_to_github` is reachable from the `git_sync` MCP tool an agent-scoped key may call on itself, and a `git add -f <ignored>` + sync loop yields a fresh removed set each time against a timestamped, non-idempotent id. Its `git_bloat`/`sync_failing` siblings stay direct because their cadence is the 60-second poller's — the influence-not-location distinction, applied. `_BUDGETED_ALERT_TYPES` gains `gitignore_untracked`; `_RESERVED_ID_PREFIXES` gains `gitignore-untracked-` so an agent cannot pre-create the id and suppress its own alert through the sink's on-conflict dedup (the C2 class). Tests: `tests/unit/test_2529_gitignore_precedence.py`. |
| 2026-08-13 | #1677 platform-emitter budget: the #1632 platform exemption split by *influence* — platform-only emitters stay direct/unthrottled; the agent-influenceable `_alert_skill_not_found` (#1410; distinct unknown slash-commands defeated its per-command dedup at $0/turn) now routes through `operator_queue_service.create_bounded_alert`, a per-(agent, registered-type) pending-DEPTH budget (`OPERATOR_ALERT_MAX_PENDING_PER_TYPE`=5, DB-measured; `_BUDGETED_ALERT_TYPES` frozenset; fail-closed on every arm; the paired notification gated on the same bool; command truncated ~200 chars on every echo). At the cap: one cooldown-gated episode alert with deterministic reserved id `alert-budget-{agent}-{type}-b{bucket}` (DB on-conflict = cross-worker dedup; no `held`, no command text). `count_pending_for_agent` gained an optional `item_type` (query-only — no migration); `create_item` belts the derived `execution_id` COLUMN (non-str/>512 → None). AST caller-parity guard `tests/unit/test_1677_operator_alert_emitters.py` forces classification of every `create_operator_queue_item` call site (`_ALLOWED_CALLERS` = the living inventory; OSS-scoped). Tests: `tests/unit/test_1677_operator_alert_budget.py`. Closes the platform-emitter residual named on the #1081 pull-default-ON gate list. |
| 2026-06-11 | #1017 "Clear All": per-tab bulk clear on the operator tabs (Needs Response -> `POST /api/operator-queue/bulk-cancel` `{ids}` (deduped order-preserving for an honest `skipped` count); Resolved -> `POST /api/operator-queue/clear-resolved` `{agent_name?}`; Notifications -> existing `POST /api/notifications/dismiss-all`). Clear-resolved **hides** terminal rows (new `cleared_at` column, migration `operator_queue_cleared_at`) instead of hard-deleting — a DELETE would be resurrected by the 5s sync loop (it re-creates DB-missing items whose file entry still says 'pending'); actual deletion deferred to the retention sweep (#1142). `list_items` gained `include_cleared=False`; `get_item` and the sync accessors never filter on `cleared_at`. Both new endpoints are audit-logged and broadcast one `operator_queue_cleared` WS event (`scope: pending\|resolved`). Tri-state accessible-agent scoping (None=admin, empty set=no-op) pushed into the DB layer. Respond/cancel race fixed: `respond_to_item` returns `_status_conflict` when the item left 'pending', router 409s instead of a silent 200; store surfaces a user-visible error. Sync service now also fetches recently-terminal items (`get_operator_queue_terminal_for_agent` — cancelled+expired, 168h created_at window) and flips still-'pending' agent-file entries to their terminal status in place (never appends). Frontend: Clear All button (`data-testid ops-clear-all`) + ConfirmDialog with blast-radius copy in `Operations.vue`; `resolvedItems` now includes cancelled/expired; `ResolvedCard.vue` Cancelled/Expired badge; `websocket.js` routes `operator_queue_cleared`. Tests: `tests/test_ops_clear_all.py` (20 tests). |
| 2026-06-09 | #1109 frontend IA refactor: "Operating Room" -> **Operations**. `OperatingRoom.vue` -> `Operations.vue`, route `/operating-room` -> `/operations` (name `Operations`). Tabs 3 -> 5 (added Health + Executions; `VALID_TABS = ['needs-response','notifications','health','executions','resolved']`). Health renders new `MonitoringPanel.vue` (from deleted `views/Monitoring.vue`), admin-tab-gated. Executions renders new `ExecutionsPanel.vue` (from deleted `views/Executions.vue`); `ExecutionDetail.vue` unchanged. Tabs toggle via `v-if` for polling teardown; operator-queue polling + `fetchAgents()` stay container-level. NavBar collapsed Health/Ops/Executions links into one "Operations" link with the existing combined badge; removed the separate Executions running-count badge. Legacy redirects added: `/operating-room` (function form, preserves `?tab=`), `/monitoring`->`?tab=health`, `/executions`->`?tab=executions`, `/events`->`?tab=notifications`. No backend changes. |
| 2026-03-08 | Consolidated Events page and Cost Alerts page into Operating Room as tabs. Added NotificationsPanel.vue, CostAlertsPanel.vue. Removed NavBar bell icons. Combined Ops badge count. Old routes redirect. |
| 2026-03-08 | Restart-resilient sync, refresh button, stale prompt detection |
| 2026-03-07 | Initial implementation (Phases 1-4): backend, sync service, frontend, meta-prompt |
