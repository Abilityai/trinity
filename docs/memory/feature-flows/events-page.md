# Feature: Events Page UI (NOTIF-002)

> **CONSOLIDATED (2026-03-08; route updated 2026-06-09 #1109)**: The standalone Events page (`/events`) has been consolidated into the **Notifications** tab of the **Operations** view (formerly Operating Room). The `/events` route now redirects to `/operations?tab=notifications` (was `/operating-room?tab=notifications` before the #1109 rename). The UI content was extracted into `src/frontend/src/components/operator/NotificationsPanel.vue`. See [operating-room.md](operating-room.md) for current documentation.
>
> `src/frontend/src/views/Events.vue` has been **deleted**. The notifications store (`notifications.js`) and all backend APIs remain unchanged.

## Overview

Dedicated Events page for viewing, filtering, and managing agent notifications. Provides a unified stream of all agent events with real-time updates via WebSocket.

## User Story

As a **platform user**, I want to see all notifications from my agents in one place so I can quickly identify what needs attention.

## Entry Points

- **NavBar**: Inbox icon with badge showing pending count (links to `/events`)
- **URL**: Direct navigation to `/events`
- **WebSocket**: Real-time notification updates

---

## Frontend Layer

### Components

**Events Page** (`src/frontend/src/views/Events.vue`, 574 lines):

**Template Structure**:
- Header with title and refresh button (lines 8-26)
- Filter controls panel (lines 29-117): agent, type, priority, status dropdowns + show dismissed checkbox
- Stats cards grid: pending (red — the global count from `GET /api/notifications/count`, independent of the list filter and page, #3449), acknowledged (green), shown (the rows rendered below), agents count (blue)
- Bulk actions bar (lines 140-164): shown when items selected
- Notifications list (lines 167-320): cards with checkbox, icon, content, actions
- Load more button (lines 311-319): pagination

**Script Setup** (lines 326-573):
- Local state: `loading`, `statusFilter`, `priorityFilter`, `agentFilter`, `typeFilter`, `showDismissed`, `expandedIds`
- Computed: `hasActiveFilters`, `availableAgents`, `displayedNotifications`, `acknowledgedCount`, `allSelected`
- Lifecycle: `onMounted()` calls `fetchNotifications()`
- Methods: `fetchNotifications()`, `applyFilters()`, `clearFilters()`, `acknowledge()`, `dismiss()`, `bulkAcknowledge()`, `bulkDismiss()`, `loadMore()`, `toggleSelectAll()`, `toggleExpanded()`, `truncateMessage()`, `formatRelativeTime()`
- Helper functions: `getTypeIcon()` (line 513), `getPriorityIconBg()` (line 524), `getPriorityIconColor()` (line 534), `getPriorityBadge()` (line 544), `getTypeBadge()` (line 554), `getStatusBadge()` (line 565)

**Filter Logic**:
- Status, priority, agent filters sent to API (server-side)
- Type filter applied client-side in `displayedNotifications` computed (line 373-375)
- Show dismissed filter applied client-side (line 378-380)

**NavBar Badge** (`src/frontend/src/components/NavBar.vue:52-67`):
- Inbox icon with pending notification count badge (lines 57-59: SVG inbox icon)
- Badge display (lines 60-66): Shows count, capped at "99+"
- Pulsing red badge (`animate-pulse`) for urgent/high priority pending (`hasUrgentPending`)
- Blue badge (`bg-blue-500`) for normal priority pending
- Polling started on mount (line 266: `notificationsStore.startPolling(60000)`)
- Polling stopped on unmount (line 282: `notificationsStore.stopPolling()`)

### State Management

**Notifications Store** (`src/frontend/src/stores/notifications.js`):

| State | Type | Description |
|-------|------|-------------|
| `notifications` | Array | Loaded notifications |
| `pendingCount` | Number | Global pending count (badges, Operations header). Server figure from `fetchPendingCount()`; never derived from the loaded list (#3449) |
| `loading` | Boolean | Loading state |
| `error` | String | Error message |
| `totalCount` | Number | Total notification count from API |
| `hasMore` | Boolean | Whether more notifications available |
| `filters` | Object | Current filter settings |
| `selectedIds` | Array | Selected notification IDs for bulk actions |

**Actions**:
- `fetchNotifications(options)` (line 69) - Fetch with filters, supports pagination via offset
- `fetchPendingCount()` (line 118) - Fetch count for badge (lightweight API call)
- `acknowledgeNotification(id)` (line 134) - Acknowledge single notification
- `dismissNotification(id)` (line 156) - Dismiss single notification
- `bulkAcknowledge(ids)` (line 183) - Acknowledge multiple via Promise.allSettled
- `bulkDismiss(ids)` (line 191) - Dismiss multiple via Promise.allSettled
- `addNotification(notification)` (line 200) - Add from WebSocket (with filter matching)
- `startPolling(intervalMs)` (line 223) - Start badge polling (default 60s)
- `stopPolling()` (line 231) - Stop badge polling, clears interval
- `setFilters(filters)` (line 239) - Update filters and refetch
- `clearFilters()` (line 244) - Reset all filters to defaults
- `loadMore()` (line 256) - Pagination: fetch next page of results
- `toggleSelected(id)` (line 263) - Toggle selection for bulk actions
- `selectAll()` (line 272) - Select all notifications
- `clearSelection()` (line 276) - Clear all selections

**Getters**:
- `pendingNotifications` (line 35) - Filter to pending only
- `acknowledgedNotifications` (line 39) - Filter to acknowledged
- `hasPendingNotifications` (line 43) - Boolean: pendingCount > 0
- `hasUrgentPending` (line 45) - Has urgent/high priority pending (used for badge pulsing)
- `agentCounts` (line 49) - Count per agent (Object)
- `filteredNotifications` (line 57) - Apply client-side dismissed filter

### Router Configuration

**Route** (`src/frontend/src/router/index.js:116-120`):
```javascript
{
  path: '/events',
  name: 'Events',
  component: () => import('../views/Events.vue'),
  meta: { requiresAuth: true }
}
```

### WebSocket Integration

**Handler** (`src/frontend/src/utils/websocket.js:71-91`):
```javascript
case 'agent_notification':
  // Real-time notification from an agent
  // The WebSocket event contains: notification_id, agent_name, notification_type, title, priority, category, timestamp
  // We update the pending count and can add to list if we have full details
  notificationsStore.fetchPendingCount()
  // If we have enough data, we can add a partial notification
  if (data.notification_id && data.agent_name && data.title) {
    notificationsStore.addNotification({
      id: data.notification_id,
      agent_name: data.agent_name,
      notification_type: data.notification_type || 'info',
      title: data.title,
      priority: data.priority || 'normal',
      category: data.category || null,
      status: 'pending',
      created_at: data.timestamp || new Date().toISOString(),
      message: null,
      metadata: null,
    })
  }
  break
```

---

## API Integration

### Endpoints Used

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/notifications` | GET | List notifications with filters |
| `/api/notifications/{id}/acknowledge` | POST | Acknowledge notification |
| `/api/notifications/{id}/dismiss` | POST | Dismiss notification |

### Query Parameters (GET /api/notifications)

| Parameter | Type | Description |
|-----------|------|-------------|
| `status` | string | pending, acknowledged, dismissed |
| `priority` | string | Comma-separated: low, normal, high, urgent |
| `agent_name` | string | Filter by agent |
| `limit` | int | Max results (1-500, default 50) |

### Response Format

```json
{
  "count": 25,
  "notifications": [
    {
      "id": "notif_abc123",
      "agent_name": "research-agent",
      "notification_type": "completion",
      "title": "Analysis complete",
      "message": "Processed 15,000 records",
      "priority": "normal",
      "category": "progress",
      "metadata": {"records": 15000},
      "status": "pending",
      "created_at": "2026-02-20T10:30:00Z",
      "acknowledged_at": null,
      "acknowledged_by": null
    }
  ]
}
```

---

## UI Components

### Priority Indicators

| Priority | Icon Color | Badge Color |
|----------|------------|-------------|
| Urgent | Red | Red (pulsing) |
| High | Orange | Orange |
| Normal | Blue | Blue |
| Low | Gray | Gray |

### Type Icons

| Type | Icon |
|------|------|
| Alert | ExclamationTriangleIcon |
| Info | InformationCircleIcon |
| Status | ChartBarIcon |
| Completion | CheckCircleIcon |
| Question | QuestionMarkCircleIcon |

### Status Badges

| Status | Badge Color |
|--------|-------------|
| Pending | Yellow |
| Acknowledged | Green |
| Dismissed | Gray |

---

## Testing

### Prerequisites

- [ ] Backend running at http://localhost:8000
- [ ] Frontend running at http://localhost
- [ ] At least one agent with notifications

### Test Steps

#### 1. Navigate to Events Page

**Action**: Click inbox icon in NavBar or navigate to `/events`

**Expected**:
- Events page loads with filter controls
- Stats cards show counts
- Notifications list displays (or empty state)

**Verify**:
- [ ] Page renders without errors
- [ ] Filters are visible
- [ ] Stats cards show correct counts

#### 2. Filter Notifications

**Action**: Change status filter to "All", apply filters

**Expected**:
- Notification list updates to show all statuses
- Filter state persists

**Verify**:
- [ ] List updates reactively
- [ ] Clear filters button appears when filters active

#### 3. Acknowledge Notification

**Action**: Click checkmark button on pending notification

**Expected**:
- Notification status changes to "acknowledged"
- Pending count decrements in badge
- Success feedback (button state change)

**Verify**:
- [ ] API call succeeds (200)
- [ ] Local state updates
- [ ] Badge count updates

#### 4. Dismiss Notification

**Action**: Click X button on notification

**Expected**:
- Notification removed from list (if not showing dismissed)
- Pending count decrements if was pending

**Verify**:
- [ ] Notification disappears from view
- [ ] Can see in "Show dismissed" mode

#### 5. Real-Time Updates

**Action**:
1. Open Events page
2. From another agent/MCP client, send a notification
3. Observe Events page

**Expected**:
- New notification appears at top of list
- Badge count increments
- No page refresh needed

**Verify**:
- [ ] WebSocket message received
- [ ] Notification added to list
- [ ] Badge updates

#### 6. Bulk Actions

**Action**:
1. Select multiple notifications via checkboxes
2. Click "Acknowledge Selected" or "Dismiss Selected"

**Expected**:
- All selected notifications updated
- Selection cleared after action

**Verify**:
- [ ] All selected items updated
- [ ] Selection state cleared

### Edge Cases

- [ ] No notifications: Shows empty state with message
- [ ] Filtered with no results: Shows "No matching events" with clear filters button
- [ ] Very long message: Truncated with "Show more" button
- [ ] Notification with metadata: Expandable JSON view

**Last Tested**: Not yet tested
**Status**: Pending verification

---

## Related Flows

- **Upstream**:
  - `agent-notifications.md` - Backend notification creation and API (includes NOTIF-003 agent attribution fix)
  - `mcp-orchestration.md` - `send_notification` MCP tool
  - `mcp-api-keys.md` - Agent-scoped keys populate `User.agent_name` for correct attribution

- **Downstream**:
  - Navigation badge in all pages via NavBar

- **Similar UI**:
  - `alerts-page.md` - Cost alerts page (similar UI pattern)

---

## Files Changed

| File | Action | Description |
|------|--------|-------------|
| `src/frontend/src/views/Events.vue` | Create | Main events page |
| `src/frontend/src/stores/notifications.js` | Create | Pinia store |
| `src/frontend/src/router/index.js` | Modify | Add /events route |
| `src/frontend/src/components/NavBar.vue` | Modify | Add inbox icon with badge |
| `src/frontend/src/utils/websocket.js` | Modify | Handle agent_notification events |

---

## Revision History

| Date | Changes |
|------|---------|
| 2026-02-20 | **NOTIF-003 Bug Fix**: Agent attribution now works correctly - notifications display the actual agent name instead of the API key owner's username. See `agent-notifications.md` for technical details. |
| 2026-02-20 | Documentation verified and updated: corrected line numbers (router 116-120, websocket 71-91), added NavBar line refs (52-67, polling 266/282), expanded store action/getter line numbers, detailed Events.vue template structure and helper functions, documented client-side type filter |
| 2026-02-20 | Initial implementation (Phase 1-3: Core page, actions, real-time) |
