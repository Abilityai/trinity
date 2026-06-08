<script setup>
/**
 * Enterprise Audit Log dashboard (#941, v2).
 *
 * Admin-facing read view over the platform audit log. Frontend ships
 * in the OSS bundle; route is gated by `requiresEntitlement: 'audit'`
 * in `router/index.js` so OSS-only deploys (where the entitlement
 * service does not register `'audit'`) bounce to the catalogue.
 *
 * v2 (this revision): stats tiles header, time-preset chips, inline
 * cell drill-down, hash-chain verify badge, CSV/JSON export. All five
 * sit on top of existing audit-log endpoints — no backend changes.
 *
 * Out of scope: SIEM webhook push (separate enterprise pillar),
 * sparkline chart (bundle weight), WebSocket live updates.
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useAuditLogStore } from '../../stores/auditLog'

const store = useAuditLogStore()
// Direct reactive access — Pinia state is already reactive, and the
// template's v-model writes pass through to the store unchanged.
const filters = store.filters

const TIME_PRESETS = [
  { key: '1h', label: 'Last 1h' },
  { key: '24h', label: 'Last 24h' },
  { key: '7d', label: 'Last 7d' },
  { key: '30d', label: 'Last 30d' },
  { key: 'all', label: 'All time' },
]

// #941 v3.2 — single foldable card hosting both heatmap views.
// `v-show` (not `v-if`) keeps both heatmaps mounted, so tab swaps don't
// remount the table or re-fetch — the user can pivot between weekly
// pattern and calendar view instantly.
const heatmapsOpen = ref(true)
const heatmapTab = ref('weekly')   // 'weekly' | 'calendar'

onMounted(async () => {
  await store.loadDistinct()
  await Promise.all([
    store.loadList(),
    store.loadStats(),
    store.loadHeatmap(),
    store.loadCalendar(),
  ])
})

// Re-load list when offset changes (pagination clicks).
watch(
  () => store.offset,
  () => {
    store.loadList()
  }
)

// Manual time-filter edits flip the preset back to "custom" so the
// chips reflect reality. We watch the two time fields rather than
// hooking the inputs because v-model binds directly to the store.
watch(
  () => [filters.start_time, filters.end_time],
  () => {
    // If the new bounds happen to match an active preset's "now − Xh"
    // computation, we still mark as custom — exact match is too fragile
    // (ms drift). Manual edit always means custom.
    if (
      store.activePreset !== 'custom' &&
      !isPresetSelected(store.activePreset)
    ) {
      store.activePreset = 'custom'
    }
  }
)

function isPresetSelected(_key) {
  // The preset action sets `activePreset` explicitly. Anywhere else
  // that touches `start_time` or `end_time` (manual form edits, the
  // drill-down handler) demotes to 'custom'. This helper exists as a
  // hook for future precise-match logic without changing the watcher.
  return false
}

function applyFilters() {
  store.offset = 0
  store.activePreset = 'custom'
  Promise.all([
    store.loadList(),
    store.loadStats(),
    store.loadHeatmap(),
    store.loadCalendar(),
  ])
}

function resetFilters() {
  store.resetFilters()
  store.loadDistinct(true)
  Promise.all([
    store.loadList(),
    store.loadStats(),
    store.loadHeatmap(),
    store.loadCalendar(),
  ])
}

async function applyPreset(key) {
  await store.applyTimePreset(key)
}

async function drilldownEvent(eventType) {
  if (!eventType) return
  await store.drilldownFilter('event_type', eventType)
}

async function drilldownActor(entry) {
  // Prefer actor_id (queryable). Fall back to actor_type if no id.
  if (entry.actor_id) {
    await store.drilldownFilter('actor_id', entry.actor_id)
  } else if (entry.actor_type) {
    await store.drilldownFilter('actor_type', entry.actor_type)
  }
}

async function verifyChain() {
  await store.verifyChain()
}

async function exportAs(format) {
  await store.downloadExport(format)
}

async function openDetail(entry) {
  // Prefer the in-list payload, but refresh from the detail endpoint to
  // pick up any field the list response truncated. The server returns
  // the same row in both cases today, but keeping the detail call lets
  // us add lazy-loaded fields (e.g. raw `details` JSON) without
  // changing list payload shape later.
  store.selectEntry(entry)
  await store.loadDetail(entry.event_id)
}

function closeDetail() {
  store.clearSelection()
}

// ─────────────────────────────────────────────────────────────────────
// #941 v3 — Heatmap rendering helpers
//
// SQLite ``strftime('%w', ...)`` returns 0=Sunday..6=Saturday. We display
// rows in ISO weekday order (Mon..Sun) because that matches what most
// admins expect when scanning a "weekly pattern" chart. The reordering
// happens here, not in the API payload — the API stays calendrically
// canonical (Sun=0) so external tooling sees what they'd see from raw
// SQLite.
// ─────────────────────────────────────────────────────────────────────
const HOURS = Array.from({ length: 24 }, (_, h) => h)
// SQLite dow indices in ISO display order: Mon=1, Tue=2, …, Sat=6, Sun=0.
const DOW_ROWS = [
  { sqliteIndex: 1, label: 'Mon' },
  { sqliteIndex: 2, label: 'Tue' },
  { sqliteIndex: 3, label: 'Wed' },
  { sqliteIndex: 4, label: 'Thu' },
  { sqliteIndex: 5, label: 'Fri' },
  { sqliteIndex: 6, label: 'Sat' },
  { sqliteIndex: 0, label: 'Sun' },
]

const heatmapGrid = computed(() => {
  // Build a dense 7×24 lookup from the sparse cell payload. Empty cells
  // are rendered as zero — never as "no data" — because the dashboard
  // already labels the time window separately.
  const cells = store.heatmap?.cells || []
  const byKey = new Map()
  for (const c of cells) {
    byKey.set(`${c.dow}-${c.hour}`, c.count)
  }
  return DOW_ROWS.map((row) => ({
    label: row.label,
    sqliteIndex: row.sqliteIndex,
    hours: HOURS.map((h) => ({
      hour: h,
      count: byKey.get(`${row.sqliteIndex}-${h}`) || 0,
    })),
  }))
})

function heatmapCellStyle(count) {
  // Linear opacity from the max cell count. Empty cells render as a
  // near-transparent neutral so the grid stays legible — pure white
  // would look like a missing tile in dark mode.
  const max = store.heatmap?.max_count || 0
  if (count === 0 || max === 0) {
    return { backgroundColor: 'rgba(148, 163, 184, 0.12)' }
  }
  // Floor opacity at 0.18 so a "1 event" cell is still visible against
  // the empty tint. Tailwind blue-600 (#2563eb) tracks the rest of the
  // dashboard accents.
  const opacity = 0.18 + 0.82 * (count / max)
  return { backgroundColor: `rgba(37, 99, 235, ${opacity.toFixed(3)})` }
}

function heatmapCellTitle(label, hour, count) {
  const hourLabel = String(hour).padStart(2, '0') + ':00'
  if (count === 0) return `${label} ${hourLabel} UTC · no events`
  return `${label} ${hourLabel} UTC · ${count} event${count === 1 ? '' : 's'}`
}

// ─────────────────────────────────────────────────────────────────────
// #941 v3.1 — GitHub-style calendar heatmap
//
// Per-day rather than dow×hour: columns are ISO calendar weeks, rows
// are Mon..Sun. Same color ramp as the dow×hour heatmap but with its
// own max so a quiet weekly pattern doesn't wash out a busy day.
//
// Click handler narrows the filter to a single UTC day, which the
// dow×hour heatmap can't offer (its cells are recurring buckets).
// ─────────────────────────────────────────────────────────────────────
const MONTH_LABELS = [
  'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
  'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec',
]

function parseIsoDate(s) {
  // Parse 'YYYY-MM-DD' as UTC midnight. Using Date.UTC keeps the
  // browser's local TZ from shifting the date and producing off-by-one
  // grid cells (e.g. UTC May 25 rendering as the May 24 cell west of GMT).
  const [y, m, d] = s.split('-').map(Number)
  return new Date(Date.UTC(y, m - 1, d))
}

function isoDate(d) {
  return d.toISOString().slice(0, 10)
}

function snapToMonday(d) {
  // ISO week starts Monday. JS getUTCDay() is 0=Sun..6=Sat — shift so
  // that Mon=0, Sun=6, then subtract to land on Monday.
  const out = new Date(d)
  const dow = (out.getUTCDay() + 6) % 7
  out.setUTCDate(out.getUTCDate() - dow)
  return out
}

const calendarGrid = computed(() => {
  const days = store.calendar?.days || []
  if (!days.length) return { weeks: [], months: [] }

  const byDate = new Map(days.map((d) => [d.date, d.count]))
  const first = parseIsoDate(days[0].date)
  const last = parseIsoDate(days[days.length - 1].date)
  const start = snapToMonday(first)
  const end = new Date(last)
  // Snap end forward to Sunday so each column is a full week.
  const endDow = (end.getUTCDay() + 6) % 7
  end.setUTCDate(end.getUTCDate() + (6 - endDow))

  const weeks = []
  const months = []
  const cursor = new Date(start)
  let lastMonth = null
  let weekIdx = 0
  while (cursor <= end) {
    const col = []
    for (let i = 0; i < 7; i++) {
      const iso = isoDate(cursor)
      const inRange = cursor >= first && cursor <= last
      col.push({
        date: iso,
        count: byDate.get(iso) || 0,
        inRange,
      })
      if (i === 0) {
        const m = cursor.getUTCMonth()
        if (lastMonth !== m) {
          months.push({ weekIdx, label: MONTH_LABELS[m] })
          lastMonth = m
        }
      }
      cursor.setUTCDate(cursor.getUTCDate() + 1)
    }
    weeks.push(col)
    weekIdx++
  }
  return { weeks, months }
})

function calendarCellStyle(count, inRange) {
  if (!inRange) {
    // Out-of-range pad cells stay transparent so the grid edges read
    // as "not in window" rather than "zero events" — important when
    // the user picks a 7d preset and most of the grid is padding.
    return { backgroundColor: 'transparent' }
  }
  const max = store.calendar?.max_count || 0
  if (count === 0 || max === 0) {
    return { backgroundColor: 'rgba(148, 163, 184, 0.12)' }
  }
  const opacity = 0.18 + 0.82 * (count / max)
  return { backgroundColor: `rgba(37, 99, 235, ${opacity.toFixed(3)})` }
}

function calendarCellTitle(cell) {
  if (!cell.inRange) return `${cell.date} · outside window`
  if (cell.count === 0) return `${cell.date} · no events`
  return `${cell.date} · ${cell.count} event${cell.count === 1 ? '' : 's'} (click to filter)`
}

async function drilldownDay(cell) {
  if (!cell.inRange || cell.count === 0) return
  await store.drilldownToDay(cell.date)
}

function formatTimestamp(ts) {
  if (!ts) return ''
  // Drop the millisecond + trailing Z for tighter table rows.
  return ts.replace('T', ' ').replace(/\.\d+/, '').replace(/Z$/, ' UTC')
}

function actorLabel(entry) {
  return entry.actor_email || entry.actor_id || `(${entry.actor_type})`
}

function targetLabel(entry) {
  if (!entry.target_type && !entry.target_id) return '—'
  if (!entry.target_id) return entry.target_type
  if (!entry.target_type) return entry.target_id
  return `${entry.target_type}/${entry.target_id}`
}

const detailsJson = computed(() => {
  const e = store.selectedEntry
  if (!e || !e.details) return ''
  if (typeof e.details === 'string') return e.details
  try {
    return JSON.stringify(e.details, null, 2)
  } catch {
    return String(e.details)
  }
})
</script>

<template>
  <div class="audit-dashboard p-6 max-w-7xl mx-auto">
    <header class="mb-6">
      <div class="flex items-center gap-3 mb-2">
        <router-link
          to="/enterprise"
          class="text-sm text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200"
        >
          ← Enterprise
        </router-link>
      </div>
      <div class="flex items-center gap-3 mb-2">
        <h1 class="text-3xl font-semibold text-gray-900 dark:text-white">Audit Log</h1>
        <span class="px-2 py-0.5 text-xs font-bold rounded bg-purple-100 text-purple-700 dark:bg-purple-900 dark:text-purple-200">
          PRO
        </span>
      </div>
      <p class="text-sm text-gray-500 dark:text-gray-400">
        Tamper-evident record of administrative actions. Default filter
        shows the last 24 hours.
      </p>

      <!-- Hash-chain verify badge — manual trigger, visible-range only. -->
      <div class="mt-3 flex items-center gap-2">
        <span
          class="inline-flex items-center gap-1.5 px-2 py-0.5 text-xs font-medium rounded"
          :class="{
            'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300':
              store.verifyState === 'idle',
            'bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-200':
              store.verifyState === 'verifying',
            'bg-green-100 text-green-700 dark:bg-green-900 dark:text-green-200':
              store.verifyState === 'valid',
            'bg-red-100 text-red-700 dark:bg-red-900 dark:text-red-200':
              store.verifyState === 'invalid',
            'bg-yellow-100 text-yellow-700 dark:bg-yellow-900 dark:text-yellow-200':
              store.verifyState === 'error',
          }"
        >
          <span v-if="store.verifyState === 'idle'">Hash chain · not verified</span>
          <span v-else-if="store.verifyState === 'verifying'">Verifying…</span>
          <span v-else-if="store.verifyState === 'valid'">
            ✓ Valid · {{ store.verifyResult?.checked || 0 }} entries
          </span>
          <span v-else-if="store.verifyState === 'invalid'">
            ✗ Tamper detected · first invalid id #{{ store.verifyResult?.first_invalid_id }}
          </span>
          <span v-else>⚠ Verify failed</span>
        </span>
        <button
          class="px-2 py-0.5 text-xs rounded border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-200 hover:bg-gray-50 dark:hover:bg-gray-700 disabled:opacity-50"
          :disabled="store.verifyState === 'verifying' || store.entries.length === 0"
          :title="
            store.entries.length === 0
              ? 'Load some entries first.'
              : `Verify ids #${Math.min(...store.entries.map(e => e.id))}–#${Math.max(...store.entries.map(e => e.id))} on this page`
          "
          @click="verifyChain"
        >
          Verify visible range
        </button>
      </div>
    </header>

    <!-- Stats tiles -->
    <section class="grid grid-cols-2 lg:grid-cols-4 gap-3 mb-4">
      <div class="p-3 rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800">
        <div class="text-xs text-gray-500 dark:text-gray-400 uppercase tracking-wider">
          Total events
        </div>
        <div class="text-2xl font-semibold text-gray-900 dark:text-white mt-1">
          {{ store.statsLoading ? '…' : (store.stats?.total ?? '—') }}
        </div>
        <div class="text-[11px] text-gray-400 mt-1">in window</div>
      </div>

      <button
        class="p-3 rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 text-left hover:border-blue-400 transition disabled:opacity-60 disabled:hover:border-gray-200 disabled:cursor-default"
        :disabled="!store.topEventType"
        :title="store.topEventType ? `Click to filter by ${store.topEventType.key}` : ''"
        @click="store.topEventType && drilldownEvent(store.topEventType.key)"
      >
        <div class="text-xs text-gray-500 dark:text-gray-400 uppercase tracking-wider">
          Top event type
        </div>
        <div class="text-lg font-semibold text-gray-900 dark:text-white mt-1 truncate">
          {{ store.topEventType?.key || '—' }}
        </div>
        <div class="text-[11px] text-gray-400 mt-1">
          {{ store.topEventType ? `${store.topEventType.count} events` : 'no data' }}
        </div>
      </button>

      <button
        class="p-3 rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 text-left hover:border-blue-400 transition disabled:opacity-60 disabled:hover:border-gray-200 disabled:cursor-default"
        :disabled="!store.topActorType"
        :title="store.topActorType ? `Click to filter by actor_type=${store.topActorType.key}` : ''"
        @click="store.topActorType && store.drilldownFilter('actor_type', store.topActorType.key)"
      >
        <div class="text-xs text-gray-500 dark:text-gray-400 uppercase tracking-wider">
          Top actor type
        </div>
        <div class="text-lg font-semibold text-gray-900 dark:text-white mt-1 truncate">
          {{ store.topActorType?.key || '—' }}
        </div>
        <div class="text-[11px] text-gray-400 mt-1">
          {{ store.topActorType ? `${store.topActorType.count} events` : 'no data' }}
        </div>
      </button>

      <div class="p-3 rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800">
        <div class="text-xs text-gray-500 dark:text-gray-400 uppercase tracking-wider">
          Time window
        </div>
        <div class="text-sm font-medium text-gray-900 dark:text-white mt-1 break-all">
          {{ store.timeWindowLabel }}
        </div>
        <div class="text-[11px] text-gray-400 mt-1">{{ store.activePreset }}</div>
      </div>
    </section>

    <!-- Time-preset chips -->
    <div class="flex flex-wrap items-center gap-2 mb-4">
      <span class="text-xs text-gray-500 dark:text-gray-400 mr-1">Time:</span>
      <button
        v-for="p in TIME_PRESETS"
        :key="p.key"
        class="px-2.5 py-1 text-xs font-medium rounded-full transition"
        :class="
          store.activePreset === p.key
            ? 'bg-blue-600 text-white'
            : 'bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-200 hover:bg-gray-200 dark:hover:bg-gray-600'
        "
        @click="applyPreset(p.key)"
      >
        {{ p.label }}
      </button>
      <span
        v-if="store.activePreset === 'custom'"
        class="px-2.5 py-1 text-xs font-medium rounded-full bg-purple-100 text-purple-700 dark:bg-purple-900 dark:text-purple-200"
      >
        Custom
      </span>
    </div>

    <!-- Unified Activity card: foldable, weekly-pattern + calendar tabs (#941 v3.2) -->
    <section
      class="mb-4 rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800"
    >
      <!-- Sticky header: fold chevron + title + tab pills + active-tab legend. -->
      <header class="flex items-center gap-3 flex-wrap p-4 pb-3">
        <button
          class="text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 font-mono text-xs"
          :aria-expanded="heatmapsOpen"
          :title="heatmapsOpen ? 'Collapse heatmaps' : 'Expand heatmaps'"
          @click="heatmapsOpen = !heatmapsOpen"
        >
          {{ heatmapsOpen ? '▾' : '▸' }}
        </button>
        <div class="flex-shrink-0">
          <h2 class="text-sm font-medium text-gray-700 dark:text-gray-200">Activity</h2>
          <p class="text-[11px] text-gray-500 dark:text-gray-400">
            <template v-if="heatmapTab === 'weekly'">
              Weekday × hour of day · UTC · honors current filters
            </template>
            <template v-else>
              One cell per UTC day · click a day to filter the dashboard to it
            </template>
          </p>
        </div>

        <!-- Tab pills. Disabled affordance when folded so the user understands
             clicking a tab unfolds first. -->
        <div
          class="inline-flex rounded-md border border-gray-200 dark:border-gray-700 overflow-hidden text-xs"
          role="tablist"
        >
          <button
            role="tab"
            :aria-selected="heatmapTab === 'weekly'"
            class="px-2.5 py-1 transition"
            :class="
              heatmapTab === 'weekly'
                ? 'bg-blue-600 text-white'
                : 'bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-200 hover:bg-gray-50 dark:hover:bg-gray-700'
            "
            @click="heatmapTab = 'weekly'; heatmapsOpen = true"
          >
            Weekly
          </button>
          <button
            role="tab"
            :aria-selected="heatmapTab === 'calendar'"
            class="px-2.5 py-1 border-l border-gray-200 dark:border-gray-700 transition"
            :class="
              heatmapTab === 'calendar'
                ? 'bg-blue-600 text-white'
                : 'bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-200 hover:bg-gray-50 dark:hover:bg-gray-700'
            "
            @click="heatmapTab = 'calendar'; heatmapsOpen = true"
          >
            Calendar
          </button>
        </div>

        <div class="flex-1"></div>

        <!-- Legend tracks the active tab's max_count so the swatch ramp matches
             the cells the user is actually looking at. Hidden when folded. -->
        <div
          v-if="heatmapsOpen"
          class="flex items-center gap-2 text-[11px] text-gray-500 dark:text-gray-400"
        >
          <span>Less</span>
          <template v-if="heatmapTab === 'weekly'">
            <span class="inline-block w-3 h-3 rounded-sm" :style="heatmapCellStyle(0)" />
            <span
              class="inline-block w-3 h-3 rounded-sm"
              :style="heatmapCellStyle(Math.max(1, Math.round((store.heatmap?.max_count || 0) * 0.25)))"
            />
            <span
              class="inline-block w-3 h-3 rounded-sm"
              :style="heatmapCellStyle(Math.max(1, Math.round((store.heatmap?.max_count || 0) * 0.5)))"
            />
            <span
              class="inline-block w-3 h-3 rounded-sm"
              :style="heatmapCellStyle(Math.max(1, Math.round((store.heatmap?.max_count || 0) * 0.75)))"
            />
            <span
              class="inline-block w-3 h-3 rounded-sm"
              :style="heatmapCellStyle(store.heatmap?.max_count || 1)"
            />
          </template>
          <template v-else>
            <span class="inline-block w-3 h-3 rounded-sm" :style="calendarCellStyle(0, true)" />
            <span
              class="inline-block w-3 h-3 rounded-sm"
              :style="calendarCellStyle(Math.max(1, Math.round((store.calendar?.max_count || 0) * 0.25)), true)"
            />
            <span
              class="inline-block w-3 h-3 rounded-sm"
              :style="calendarCellStyle(Math.max(1, Math.round((store.calendar?.max_count || 0) * 0.5)), true)"
            />
            <span
              class="inline-block w-3 h-3 rounded-sm"
              :style="calendarCellStyle(Math.max(1, Math.round((store.calendar?.max_count || 0) * 0.75)), true)"
            />
            <span
              class="inline-block w-3 h-3 rounded-sm"
              :style="calendarCellStyle(store.calendar?.max_count || 1, true)"
            />
          </template>
          <span>More</span>
        </div>
      </header>

      <!-- Body: tabs share a panel; v-show keeps both DOM trees mounted so
           switching tabs is instant and table state is preserved. -->
      <div v-show="heatmapsOpen" class="px-4 pb-4">
        <!-- Weekly (dow × hour) -->
        <div v-show="heatmapTab === 'weekly'" role="tabpanel" aria-label="Weekly pattern">
          <div v-if="store.heatmapLoading" class="text-xs text-gray-500 py-4 text-center">
            Loading heatmap…
          </div>
          <div
            v-else-if="(store.heatmap?.total || 0) === 0"
            class="text-xs text-gray-500 py-4 text-center"
          >
            No events in this window.
          </div>
          <div v-else class="overflow-x-auto">
            <table class="text-[10px] text-gray-500 dark:text-gray-400 border-separate" style="border-spacing: 2px">
              <thead>
                <tr>
                  <th class="w-8"></th>
                  <th
                    v-for="h in HOURS"
                    :key="`h-${h}`"
                    class="font-normal text-center"
                    style="min-width: 16px"
                  >
                    <span v-if="h % 3 === 0">{{ String(h).padStart(2, '0') }}</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="row in heatmapGrid" :key="row.label">
                  <td class="pr-1 text-right whitespace-nowrap">{{ row.label }}</td>
                  <td
                    v-for="cell in row.hours"
                    :key="`${row.label}-${cell.hour}`"
                    class="rounded-sm"
                    style="width: 16px; height: 16px"
                    :style="heatmapCellStyle(cell.count)"
                    :title="heatmapCellTitle(row.label, cell.hour, cell.count)"
                    :aria-label="heatmapCellTitle(row.label, cell.hour, cell.count)"
                  ></td>
                </tr>
              </tbody>
            </table>
            <p class="mt-2 text-[11px] text-gray-500 dark:text-gray-400">
              {{ store.heatmap?.total || 0 }} events ·
              peak {{ store.heatmap?.max_count || 0 }}/hour
            </p>
          </div>
        </div>

        <!-- Calendar (per-day, GitHub-style) -->
        <div v-show="heatmapTab === 'calendar'" role="tabpanel" aria-label="Calendar">
          <div v-if="store.calendarLoading" class="text-xs text-gray-500 py-4 text-center">
            Loading calendar…
          </div>
          <div
            v-else-if="(store.calendar?.total || 0) === 0"
            class="text-xs text-gray-500 py-4 text-center"
          >
            No events in this window.
          </div>
          <div v-else class="overflow-x-auto">
            <table class="text-[10px] text-gray-500 dark:text-gray-400 border-separate" style="border-spacing: 2px">
              <thead>
                <tr>
                  <th class="w-8"></th>
                  <th
                    v-for="(_w, idx) in calendarGrid.weeks"
                    :key="`mh-${idx}`"
                    class="font-normal text-left"
                    style="min-width: 14px"
                  >
                    <span v-for="m in calendarGrid.months.filter(mm => mm.weekIdx === idx)" :key="m.label">
                      {{ m.label }}
                    </span>
                  </th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(dow, dowIdx) in ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']" :key="dow">
                  <td class="pr-1 text-right whitespace-nowrap">
                    <span v-if="dowIdx % 2 === 0">{{ dow }}</span>
                  </td>
                  <td
                    v-for="(week, weekIdx) in calendarGrid.weeks"
                    :key="`c-${weekIdx}-${dowIdx}`"
                    class="rounded-sm"
                    :class="week[dowIdx].inRange && week[dowIdx].count > 0 ? 'cursor-pointer' : ''"
                    style="width: 14px; height: 14px"
                    :style="calendarCellStyle(week[dowIdx].count, week[dowIdx].inRange)"
                    :title="calendarCellTitle(week[dowIdx])"
                    :aria-label="calendarCellTitle(week[dowIdx])"
                    @click="drilldownDay(week[dowIdx])"
                  ></td>
                </tr>
              </tbody>
            </table>
            <p class="mt-2 text-[11px] text-gray-500 dark:text-gray-400">
              {{ store.calendar?.total || 0 }} events across
              {{ store.calendar?.days?.length || 0 }} active day{{ (store.calendar?.days?.length || 0) === 1 ? '' : 's' }} ·
              peak {{ store.calendar?.max_count || 0 }}/day
            </p>
          </div>
        </div>
      </div>
    </section>

    <!-- Filter form -->
    <section
      class="mb-4 p-4 rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800"
    >
      <h2 class="text-sm font-medium text-gray-700 dark:text-gray-200 mb-3">
        Filters
      </h2>
      <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
        <div>
          <label class="block text-xs text-gray-500 dark:text-gray-400 mb-1"
            >Event type</label
          >
          <select
            v-model="filters.event_type"
            class="w-full px-2 py-1 text-sm rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-gray-900 dark:text-white"
          >
            <option value="">All</option>
            <option v-for="t in store.distinctEventTypes" :key="t" :value="t">
              {{ t }}
            </option>
          </select>
        </div>
        <div>
          <label class="block text-xs text-gray-500 dark:text-gray-400 mb-1"
            >Actor type</label
          >
          <select
            v-model="filters.actor_type"
            class="w-full px-2 py-1 text-sm rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-gray-900 dark:text-white"
          >
            <option value="">All</option>
            <option v-for="t in store.distinctActorTypes" :key="t" :value="t">
              {{ t }}
            </option>
          </select>
        </div>
        <div>
          <label class="block text-xs text-gray-500 dark:text-gray-400 mb-1"
            >Actor ID</label
          >
          <input
            v-model="filters.actor_id"
            type="text"
            placeholder="user.id or agent_name"
            class="w-full px-2 py-1 text-sm rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-gray-900 dark:text-white"
          />
        </div>
        <div>
          <label class="block text-xs text-gray-500 dark:text-gray-400 mb-1"
            >Target type</label
          >
          <input
            v-model="filters.target_type"
            type="text"
            placeholder="agent / user / schedule…"
            class="w-full px-2 py-1 text-sm rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-gray-900 dark:text-white"
          />
        </div>
        <div>
          <label class="block text-xs text-gray-500 dark:text-gray-400 mb-1"
            >Start (ISO 8601 UTC)</label
          >
          <input
            v-model="filters.start_time"
            type="text"
            placeholder="2026-05-25T00:00:00Z"
            class="w-full px-2 py-1 text-sm rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-gray-900 dark:text-white"
          />
        </div>
        <div>
          <label class="block text-xs text-gray-500 dark:text-gray-400 mb-1"
            >End (ISO 8601 UTC)</label
          >
          <input
            v-model="filters.end_time"
            type="text"
            placeholder="leave blank for now"
            class="w-full px-2 py-1 text-sm rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-gray-900 dark:text-white"
          />
        </div>
      </div>
      <div class="mt-3 flex flex-wrap items-center gap-2">
        <button
          class="px-3 py-1.5 text-sm font-medium rounded bg-blue-600 text-white hover:bg-blue-700"
          @click="applyFilters"
        >
          Apply
        </button>
        <button
          class="px-3 py-1.5 text-sm font-medium rounded border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-200 hover:bg-gray-50 dark:hover:bg-gray-700"
          @click="resetFilters"
        >
          Reset
        </button>
        <div class="flex-1"></div>
        <span class="text-xs text-gray-500 dark:text-gray-400 hidden sm:inline">
          Export current view:
        </span>
        <button
          class="px-3 py-1.5 text-sm font-medium rounded border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-200 hover:bg-gray-50 dark:hover:bg-gray-700 disabled:opacity-50"
          :disabled="store.exporting"
          title="Download a CSV of the current filter window (uses /api/audit-log/export)."
          @click="exportAs('csv')"
        >
          ⬇ CSV
        </button>
        <button
          class="px-3 py-1.5 text-sm font-medium rounded border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-200 hover:bg-gray-50 dark:hover:bg-gray-700 disabled:opacity-50"
          :disabled="store.exporting"
          title="Download a JSON array of the current filter window."
          @click="exportAs('json')"
        >
          ⬇ JSON
        </button>
      </div>
      <div
        v-if="store.error"
        class="mt-2 text-xs text-red-600 dark:text-red-400"
      >
        {{ store.error }}
      </div>
    </section>

    <!-- Table + detail layout -->
    <section class="flex gap-4">
      <div
        class="flex-1 rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 overflow-hidden"
        :class="store.selectedEntry ? 'lg:max-w-3xl' : ''"
      >
        <div v-if="store.loading" class="p-6 text-sm text-gray-500 text-center">
          Loading…
        </div>
        <div
          v-else-if="!store.entries.length"
          class="p-6 text-sm text-gray-500 text-center"
        >
          {{
            store.total === 0
              ? 'No audit entries match these filters.'
              : 'No results on this page.'
          }}
        </div>
        <table v-else class="w-full text-sm">
          <thead class="bg-gray-50 dark:bg-gray-900 text-left">
            <tr class="text-xs uppercase tracking-wider text-gray-500 dark:text-gray-400">
              <th class="px-3 py-2 font-medium">Timestamp</th>
              <th class="px-3 py-2 font-medium">Event</th>
              <th class="px-3 py-2 font-medium">Actor</th>
              <th class="px-3 py-2 font-medium">Target</th>
              <th class="px-3 py-2 font-medium">Source</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="entry in store.entries"
              :key="entry.event_id"
              class="border-t border-gray-100 dark:border-gray-700 cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-700"
              :class="
                store.selectedEntry?.event_id === entry.event_id
                  ? 'bg-blue-50 dark:bg-blue-900/20'
                  : ''
              "
              @click="openDetail(entry)"
            >
              <td class="px-3 py-2 font-mono text-xs text-gray-600 dark:text-gray-300 whitespace-nowrap">
                {{ formatTimestamp(entry.timestamp) }}
              </td>
              <td class="px-3 py-2 text-gray-900 dark:text-white">
                <button
                  class="font-medium underline-offset-2 hover:underline hover:text-blue-700 dark:hover:text-blue-300"
                  :title="`Filter by event_type=${entry.event_type}`"
                  @click.stop="drilldownEvent(entry.event_type)"
                >
                  {{ entry.event_type }}
                </button>
                <span class="ml-1 text-xs text-gray-500 dark:text-gray-400"
                  >· {{ entry.event_action }}</span
                >
              </td>
              <td class="px-3 py-2 text-gray-700 dark:text-gray-200">
                <button
                  class="underline-offset-2 hover:underline hover:text-blue-700 dark:hover:text-blue-300"
                  :title="
                    entry.actor_id
                      ? `Filter by actor_id=${entry.actor_id}`
                      : `Filter by actor_type=${entry.actor_type}`
                  "
                  @click.stop="drilldownActor(entry)"
                >
                  {{ actorLabel(entry) }}
                </button>
              </td>
              <td class="px-3 py-2 text-gray-700 dark:text-gray-200">
                {{ targetLabel(entry) }}
              </td>
              <td class="px-3 py-2 text-xs text-gray-500 dark:text-gray-400">
                {{ entry.source }}
              </td>
            </tr>
          </tbody>
        </table>
        <footer
          class="flex items-center justify-between px-4 py-2 border-t border-gray-100 dark:border-gray-700 text-xs text-gray-500 dark:text-gray-400"
        >
          <span>{{ store.rangeLabel }}</span>
          <span class="flex items-center gap-2">
            <button
              class="px-2 py-0.5 rounded border border-gray-300 dark:border-gray-600 disabled:opacity-40"
              :disabled="!store.hasPrev || store.loading"
              @click="store.prevPage()"
            >
              ← Prev
            </button>
            <span>Page {{ store.page }} of {{ store.pageCount }}</span>
            <button
              class="px-2 py-0.5 rounded border border-gray-300 dark:border-gray-600 disabled:opacity-40"
              :disabled="!store.hasNext || store.loading"
              @click="store.nextPage()"
            >
              Next →
            </button>
          </span>
        </footer>
      </div>

      <!-- Side detail panel -->
      <aside
        v-if="store.selectedEntry"
        class="hidden lg:block flex-1 max-w-lg rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 p-4 overflow-y-auto self-start"
      >
        <header class="flex items-start justify-between mb-3">
          <div>
            <h3 class="text-base font-medium text-gray-900 dark:text-white">
              {{ store.selectedEntry.event_type }} ·
              {{ store.selectedEntry.event_action }}
            </h3>
            <p class="text-xs text-gray-500 dark:text-gray-400 font-mono mt-0.5">
              {{ store.selectedEntry.event_id }}
            </p>
          </div>
          <button
            class="text-gray-400 hover:text-gray-600 dark:hover:text-gray-200"
            aria-label="Close detail"
            @click="closeDetail"
          >
            ✕
          </button>
        </header>

        <dl class="grid grid-cols-3 gap-x-3 gap-y-1 text-xs mb-3">
          <dt class="text-gray-500 dark:text-gray-400">Timestamp</dt>
          <dd class="col-span-2 font-mono text-gray-900 dark:text-white">
            {{ store.selectedEntry.timestamp }}
          </dd>

          <dt class="text-gray-500 dark:text-gray-400">Actor</dt>
          <dd class="col-span-2 text-gray-900 dark:text-white">
            {{ store.selectedEntry.actor_type }} ·
            {{ actorLabel(store.selectedEntry) }}
          </dd>

          <template v-if="store.selectedEntry.actor_ip">
            <dt class="text-gray-500 dark:text-gray-400">Actor IP</dt>
            <dd class="col-span-2 font-mono text-gray-900 dark:text-white">
              {{ store.selectedEntry.actor_ip }}
            </dd>
          </template>

          <template v-if="store.selectedEntry.mcp_key_name">
            <dt class="text-gray-500 dark:text-gray-400">MCP key</dt>
            <dd class="col-span-2 text-gray-900 dark:text-white">
              {{ store.selectedEntry.mcp_key_name }} ({{ store.selectedEntry.mcp_scope || 'unknown scope' }})
            </dd>
          </template>

          <dt class="text-gray-500 dark:text-gray-400">Target</dt>
          <dd class="col-span-2 text-gray-900 dark:text-white">
            {{ targetLabel(store.selectedEntry) }}
          </dd>

          <dt class="text-gray-500 dark:text-gray-400">Source</dt>
          <dd class="col-span-2 text-gray-900 dark:text-white">
            {{ store.selectedEntry.source }}
            <span
              v-if="store.selectedEntry.endpoint"
              class="text-gray-500 dark:text-gray-400 font-mono"
            >
              ({{ store.selectedEntry.endpoint }})
            </span>
          </dd>

          <template v-if="store.selectedEntry.request_id">
            <dt class="text-gray-500 dark:text-gray-400">Request</dt>
            <dd class="col-span-2 font-mono text-gray-900 dark:text-white">
              {{ store.selectedEntry.request_id }}
            </dd>
          </template>
        </dl>

        <details class="mb-3" open>
          <summary class="text-xs font-medium text-gray-600 dark:text-gray-300 cursor-pointer">
            Details JSON
          </summary>
          <pre
            class="mt-2 p-2 rounded bg-gray-50 dark:bg-gray-900 text-xs text-gray-800 dark:text-gray-200 overflow-x-auto"
          >{{ detailsJson || '(none)' }}</pre>
        </details>

        <details>
          <summary class="text-xs font-medium text-gray-600 dark:text-gray-300 cursor-pointer">
            Hash chain
          </summary>
          <dl class="mt-2 text-xs grid grid-cols-3 gap-x-3 gap-y-1">
            <dt class="text-gray-500 dark:text-gray-400">previous_hash</dt>
            <dd class="col-span-2 font-mono break-all text-gray-900 dark:text-white">
              {{ store.selectedEntry.previous_hash || '(none)' }}
            </dd>
            <dt class="text-gray-500 dark:text-gray-400">entry_hash</dt>
            <dd class="col-span-2 font-mono break-all text-gray-900 dark:text-white">
              {{ store.selectedEntry.entry_hash || '(none)' }}
            </dd>
          </dl>
        </details>
      </aside>
    </section>
  </div>
</template>
