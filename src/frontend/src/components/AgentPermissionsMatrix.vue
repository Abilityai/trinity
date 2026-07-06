<template>
  <div class="p-6">
    <!-- Header + intro -->
    <div class="mb-4">
      <h3 class="text-lg font-medium text-gray-900 dark:text-white mb-1">Agent Permissions Matrix</h3>
      <p class="text-sm text-gray-500 dark:text-gray-400">
        Fleet-wide view of which agent may call which over the Trinity MCP tools.
        A check at <span class="font-medium">(caller&nbsp;row, target&nbsp;column)</span>
        means the row agent may call the column agent — grants are one-directional.
        Writes go through the same grant/revoke path as each agent's Permissions tab.
      </p>
    </div>

    <!-- Loading -->
    <div v-if="loading" class="text-center py-10">
      <div class="animate-spin rounded-full h-8 w-8 border-b-2 border-action-primary-500 mx-auto"></div>
      <p class="mt-2 text-sm text-gray-500 dark:text-gray-400">Loading permissions…</p>
    </div>

    <!-- Load error -->
    <div v-else-if="loadError" class="rounded-lg border border-status-danger-200 dark:border-status-danger-800 bg-status-danger-50 dark:bg-status-danger-900/30 p-4 text-sm text-status-danger-700 dark:text-status-danger-300">
      {{ loadError }}
      <button class="ml-2 underline" @click="reload">Retry</button>
    </div>

    <!-- Empty -->
    <div v-else-if="agents.length === 0" class="text-center py-10 text-sm text-gray-500 dark:text-gray-400">
      No agents available to display. Create at least two agents to manage cross-agent permissions.
    </div>

    <div v-else>
      <!-- Toolbar: filter + summary -->
      <div class="flex flex-wrap items-center gap-3 mb-4">
        <div class="relative">
          <input
            v-model="filter"
            type="text"
            placeholder="Filter agents (both axes)…"
            class="w-64 pl-3 pr-8 py-1.5 text-sm rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-900 text-gray-900 dark:text-gray-100 focus:ring-action-primary-500 focus:border-action-primary-500"
          />
          <button
            v-if="filter"
            @click="filter = ''"
            class="absolute right-2 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600 dark:hover:text-gray-200"
            aria-label="Clear filter"
          >×</button>
        </div>

        <span class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium bg-action-primary-50 dark:bg-action-primary-900/40 text-action-primary-700 dark:text-action-primary-300">
          {{ grantCount }} grants / {{ possibleCount }} possible
        </span>

        <span v-if="busy" class="inline-flex items-center gap-1.5 text-xs text-state-autonomous-600 dark:text-state-autonomous-400">
          <span class="animate-spin rounded-full h-3 w-3 border-b-2 border-current"></span>
          Saving…
        </span>

        <span v-if="visibleAgents.length !== agents.length" class="text-xs text-gray-400">
          showing {{ visibleAgents.length }} of {{ agents.length }}
        </span>
      </div>

      <!-- Toast -->
      <div v-if="message" :class="[
        'mb-3 text-sm rounded-md px-3 py-2',
        message.type === 'error'
          ? 'bg-status-danger-50 dark:bg-status-danger-900/30 text-status-danger-700 dark:text-status-danger-300'
          : 'bg-status-success-50 dark:bg-status-success-900/30 text-status-success-700 dark:text-status-success-300'
      ]">{{ message.text }}</div>

      <div v-if="visibleAgents.length === 0" class="text-sm text-gray-500 dark:text-gray-400 py-6">
        No agents match “{{ filter }}”.
      </div>

      <div v-else class="flex gap-4 items-start">
        <!-- The grid -->
        <div class="overflow-auto max-h-[70vh] border border-gray-200 dark:border-gray-700 rounded-lg flex-1">
          <table class="border-collapse text-xs" @mouseleave="hoverRow = hoverCol = -1">
            <thead>
              <!-- Target axis band -->
              <tr>
                <th class="sticky left-0 top-0 z-30 bg-gray-50 dark:bg-gray-800"></th>
                <th
                  :colspan="visibleAgents.length"
                  class="sticky top-0 z-20 bg-gray-50 dark:bg-gray-800 px-2 py-1 text-center font-semibold text-gray-500 dark:text-gray-400 border-b border-gray-200 dark:border-gray-700"
                >
                  Target — receives the call →
                </th>
              </tr>
              <!-- Column headers -->
              <tr>
                <!-- Split corner cell -->
                <th class="sticky left-0 top-7 z-30 bg-gray-100 dark:bg-gray-800 w-40 min-w-40 h-16 p-0 border-b border-r border-gray-200 dark:border-gray-700">
                  <div class="relative w-full h-full corner-split">
                    <span class="absolute top-1 right-1 text-[10px] text-gray-500 dark:text-gray-400">target →</span>
                    <span class="absolute bottom-1 left-1 text-[10px] text-gray-500 dark:text-gray-400">caller ↓</span>
                  </div>
                </th>
                <th
                  v-for="(t, ci) in visibleAgents"
                  :key="'col-' + t.name"
                  scope="col"
                  :class="[
                    'sticky top-7 z-10 bg-gray-100 dark:bg-gray-800 px-1 py-2 border-b border-gray-200 dark:border-gray-700 font-medium align-bottom whitespace-nowrap',
                    hoverCol === ci ? 'bg-action-primary-50 dark:bg-action-primary-900/40' : ''
                  ]"
                >
                  <div class="flex flex-col items-center gap-1">
                    <button
                      class="text-gray-400 hover:text-action-primary-600 dark:hover:text-action-primary-400"
                      :title="`Column actions for → ${t.name}`"
                      @click="openHeaderMenu('col', t.name, $event)"
                    >⋯</button>
                    <span class="col-label text-gray-700 dark:text-gray-300" :title="'→ ' + t.name">→ {{ t.name }}</span>
                  </div>
                </th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="(s, ri) in visibleAgents"
                :key="'row-' + s.name"
              >
                <!-- Row header (caller) -->
                <th
                  scope="row"
                  :class="[
                    'sticky left-0 z-10 bg-gray-100 dark:bg-gray-800 px-2 py-1 text-left font-medium border-r border-b border-gray-200 dark:border-gray-700 whitespace-nowrap',
                    hoverRow === ri ? 'bg-action-primary-50 dark:bg-action-primary-900/40' : ''
                  ]"
                >
                  <div class="flex items-center justify-between gap-2">
                    <span class="text-gray-700 dark:text-gray-300" :title="s.name + ' →'">{{ s.name }} →</span>
                    <button
                      class="text-gray-400 hover:text-action-primary-600 dark:hover:text-action-primary-400"
                      :title="`Row actions for ${s.name} →`"
                      @click="openHeaderMenu('row', s.name, $event)"
                    >⋯</button>
                  </div>
                </th>

                <!-- Cells -->
                <td
                  v-for="(t, ci) in visibleAgents"
                  :key="s.name + '->' + t.name"
                  :class="cellClass(s, t, ri, ci)"
                  @mouseenter="hoverRow = ri; hoverCol = ci"
                  @click="onCellClick(s, t, $event)"
                >
                  <template v-if="s.name === t.name">
                    <span class="sr-only">self</span>
                  </template>
                  <template v-else-if="hasGrant(s.name, t.name)">
                    <span class="text-action-primary-600 dark:text-action-primary-400 font-bold" aria-label="granted">✓</span>
                  </template>
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <!-- Selected-pair rail -->
        <div class="w-72 shrink-0" v-if="selected">
          <div class="border border-gray-200 dark:border-gray-700 rounded-lg bg-white dark:bg-gray-800 p-4 sticky top-4">
            <div class="flex items-center justify-between mb-2">
              <h4 class="text-sm font-semibold text-gray-900 dark:text-white">Selected pair</h4>
              <button class="text-gray-400 hover:text-gray-600 dark:hover:text-gray-200" @click="selected = null" aria-label="Close">×</button>
            </div>

            <!-- Direction sentence -->
            <p class="text-sm text-gray-700 dark:text-gray-300 mb-3">
              <span class="font-medium">{{ selected.source }}</span>
              <span class="text-gray-400"> → </span>
              <span class="font-medium">{{ selected.target }}</span>
            </p>

            <template v-if="selectedGranted">
              <p class="text-sm text-gray-700 dark:text-gray-300 mb-1">
                <span class="font-medium">{{ selected.source }}</span> may call
                <span class="font-medium">{{ selected.target }}</span>.
                The reverse direction
                <span v-if="reverseGranted">also has a grant.</span>
                <span v-else>has no grant.</span>
              </p>
              <p class="text-xs text-gray-500 dark:text-gray-400 mb-3">
                Granted by {{ grantMeta.granted_by || 'unknown' }}<span v-if="grantMeta.granted_at"> · {{ formatDate(grantMeta.granted_at) }}</span>.
                This does <span class="font-semibold">not</span> let {{ selected.target }} call {{ selected.source }}.
              </p>
              <div class="flex flex-col gap-2">
                <button
                  class="w-full text-sm px-3 py-1.5 rounded-md bg-status-danger-600 hover:bg-status-danger-700 text-white disabled:opacity-50"
                  :disabled="busy"
                  @click="revoke(selected.source, selected.target)"
                >Revoke {{ selected.source }} → {{ selected.target }}</button>
                <button
                  v-if="!reverseGranted"
                  class="w-full text-sm px-3 py-1.5 rounded-md border border-action-primary-500 text-action-primary-600 dark:text-action-primary-400 hover:bg-action-primary-50 dark:hover:bg-action-primary-900/30 disabled:opacity-50"
                  :disabled="busy"
                  @click="grant(selected.target, selected.source)"
                >Grant reverse ({{ selected.target }} → {{ selected.source }})</button>
              </div>
            </template>

            <template v-else>
              <p class="text-sm text-gray-700 dark:text-gray-300 mb-3">
                No grant yet. Granting lets <span class="font-medium">{{ selected.source }}</span>
                call <span class="font-medium">{{ selected.target }}</span> — one direction only.
                This does <span class="font-semibold">not</span> let {{ selected.target }} call {{ selected.source }}.
              </p>
              <button
                class="w-full text-sm px-3 py-1.5 rounded-md bg-action-primary-600 hover:bg-action-primary-700 text-white disabled:opacity-50"
                :disabled="busy"
                @click="grant(selected.source, selected.target)"
              >Grant {{ selected.source }} → {{ selected.target }}</button>
            </template>
          </div>
        </div>
      </div>
    </div>

    <!-- Header bulk-action menu (row/col) -->
    <div
      v-if="headerMenu"
      class="fixed inset-0 z-40"
      @click="headerMenu = null"
    >
      <div
        class="absolute z-50 w-60 rounded-md border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 shadow-lg py-1 text-sm"
        :style="{ left: headerMenu.x + 'px', top: headerMenu.y + 'px' }"
        @click.stop
      >
        <div class="px-3 py-2 text-xs text-gray-500 dark:text-gray-400 border-b border-gray-100 dark:border-gray-700">
          <template v-if="headerMenu.kind === 'row'">Caller: <span class="font-medium">{{ headerMenu.agent }} →</span></template>
          <template v-else>Target: <span class="font-medium">→ {{ headerMenu.agent }}</span></template>
        </div>
        <button class="w-full text-left px-3 py-1.5 hover:bg-gray-100 dark:hover:bg-gray-700 disabled:opacity-50" :disabled="busy" @click="bulkGrant(headerMenu)">Grant all</button>
        <button class="w-full text-left px-3 py-1.5 hover:bg-gray-100 dark:hover:bg-gray-700 text-status-danger-600 dark:text-status-danger-400 disabled:opacity-50" :disabled="busy" @click="bulkRevoke(headerMenu)">Revoke all</button>
        <template v-if="headerMenu.kind === 'row'">
          <div class="border-t border-gray-100 dark:border-gray-700 mt-1 pt-1">
            <div class="px-3 py-1 text-xs text-gray-500 dark:text-gray-400">Copy grants from…</div>
            <select
              class="mx-3 mb-2 w-[calc(100%-1.5rem)] text-xs rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-900 py-1"
              :disabled="busy"
              @change="copyGrants(headerMenu.agent, $event.target.value); $event.target.value = ''"
            >
              <option value="">Select agent…</option>
              <option v-for="a in agents.filter(x => x.name !== headerMenu.agent)" :key="a.name" :value="a.name">{{ a.name }}</option>
            </select>
          </div>
        </template>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useAgentsStore } from '@/stores/agents'

const agentsStore = useAgentsStore()

const loading = ref(true)
const loadError = ref(null)
const busy = ref(false)
const message = ref(null)

const agents = ref([])            // accessible non-system agents (both axes)
const grants = ref(new Set())     // keys "source target"
const grantMetaMap = ref(new Map())

const filter = ref('')
const hoverRow = ref(-1)
const hoverCol = ref(-1)
const selected = ref(null)        // { source, target }
const headerMenu = ref(null)      // { kind:'row'|'col', agent, x, y }

const key = (s, t) => `${s} ${t}`

const visibleAgents = computed(() => {
  const f = filter.value.trim().toLowerCase()
  if (!f) return agents.value
  return agents.value.filter(a => a.name.toLowerCase().includes(f))
})

const grantCount = computed(() => grants.value.size)
const possibleCount = computed(() => {
  const n = agents.value.length
  return n > 1 ? n * (n - 1) : 0
})

const hasGrant = (s, t) => grants.value.has(key(s, t))

const selectedGranted = computed(() =>
  selected.value && hasGrant(selected.value.source, selected.value.target)
)
const reverseGranted = computed(() =>
  selected.value && hasGrant(selected.value.target, selected.value.source)
)
const grantMeta = computed(() => {
  if (!selected.value) return {}
  return grantMetaMap.value.get(key(selected.value.source, selected.value.target)) || {}
})

function cellClass(s, t, ri, ci) {
  const base = ['w-8', 'h-8', 'text-center', 'border-b', 'border-r', 'border-gray-100', 'dark:border-gray-700/60', 'cursor-pointer', 'select-none']
  if (s.name === t.name) {
    base.push('self-diag', 'cursor-default')
    return base
  }
  const isSel = selected.value && selected.value.source === s.name && selected.value.target === t.name
  if (isSel) base.push('ring-2', 'ring-inset', 'ring-action-primary-500')
  if (hasGrant(s.name, t.name)) {
    base.push('bg-action-primary-100', 'dark:bg-action-primary-900/50', 'hover:bg-action-primary-200', 'dark:hover:bg-action-primary-900/70')
  } else if (hoverRow.value === ri || hoverCol.value === ci) {
    base.push('bg-action-primary-50/60', 'dark:bg-action-primary-900/20')
  } else {
    base.push('hover:bg-gray-100', 'dark:hover:bg-gray-700/50')
  }
  return base
}

function onCellClick(s, t, ev) {
  if (s.name === t.name) return
  selected.value = { source: s.name, target: t.name }
  headerMenu.value = null
}

function openHeaderMenu(kind, agent, ev) {
  ev.stopPropagation()
  headerMenu.value = {
    kind,
    agent,
    x: Math.min(ev.clientX, window.innerWidth - 260),
    y: Math.min(ev.clientY, window.innerHeight - 220),
  }
}

function flash(type, text, ms = 3500) {
  message.value = { type, text }
  setTimeout(() => { if (message.value && message.value.text === text) message.value = null }, ms)
}

function formatDate(iso) {
  try { return new Date(iso).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' }) }
  catch { return iso }
}

function applyEdges(edges) {
  const set = new Set()
  const meta = new Map()
  for (const e of edges) {
    const k = key(e.source, e.target)
    set.add(k)
    meta.set(k, { granted_by: e.granted_by, granted_at: e.granted_at })
  }
  grants.value = set
  grantMetaMap.value = meta
}

async function reload() {
  loading.value = true
  loadError.value = null
  try {
    // Single gated read: both axes (accessible, non-system agents, already
    // filtered + is_owner-tagged server-side) plus every grant edge.
    const m = await agentsStore.getPermissionsMatrix()
    agents.value = [...m.agents].sort((a, b) => a.name.localeCompare(b.name))
    applyEdges(m.edges)
  } catch (err) {
    loadError.value = err.response?.data?.detail || 'Failed to load permissions matrix.'
  } finally {
    loading.value = false
  }
}

// After a write, re-read only the edges (the axis rarely changes mid-session).
async function refreshEdges() {
  const m = await agentsStore.getPermissionsMatrix()
  applyEdges(m.edges)
}

async function grant(source, target) {
  if (source === target || busy.value) return
  busy.value = true
  try {
    await agentsStore.addAgentPermission(source, target)
    await refreshEdges()
    flash('success', `Granted ${source} → ${target}`)
  } catch (err) {
    flash('error', writeError(err, source))
  } finally {
    busy.value = false
  }
}

async function revoke(source, target) {
  if (busy.value) return
  busy.value = true
  try {
    await agentsStore.removeAgentPermission(source, target)
    await refreshEdges()
    flash('success', `Revoked ${source} → ${target}`)
  } catch (err) {
    flash('error', writeError(err, source))
  } finally {
    busy.value = false
  }
}

function writeError(err, source) {
  const detail = err.response?.data?.detail
  if (err.response?.status === 404) return `Can't modify grants for "${source}" — it has no container (never started or deleted).`
  if (err.response?.status === 403) return `You can only modify grants for agents you own.`
  return detail || 'Permission change failed.'
}

// Bulk over a caller row or a target column.
async function bulkGrant(menu) {
  headerMenu.value = null
  const targets = agents.value.filter(a => a.name !== menu.agent).map(a => a.name)
  if (menu.kind === 'row') {
    await runBatch(targets.map(t => [menu.agent, t]).filter(([s, t]) => !hasGrant(s, t)))
  } else {
    await runBatch(targets.map(s => [s, menu.agent]).filter(([s, t]) => !hasGrant(s, t)))
  }
}

async function bulkRevoke(menu) {
  headerMenu.value = null
  const targets = agents.value.filter(a => a.name !== menu.agent).map(a => a.name)
  let pairs
  if (menu.kind === 'row') {
    pairs = targets.map(t => [menu.agent, t]).filter(([s, t]) => hasGrant(s, t))
  } else {
    pairs = targets.map(s => [s, menu.agent]).filter(([s, t]) => hasGrant(s, t))
  }
  await runBatch(pairs, true)
}

// Copy every grant of `from` onto row `to` (grant the union; never revokes).
async function copyGrants(to, from) {
  if (!from || from === to) return
  headerMenu.value = null
  const fromTargets = agents.value
    .map(a => a.name)
    .filter(t => t !== from && hasGrant(from, t) && t !== to && !hasGrant(to, t))
  await runBatch(fromTargets.map(t => [to, t]))
}

async function runBatch(pairs, isRevoke = false) {
  if (pairs.length === 0) { flash('success', 'Nothing to change.'); return }
  busy.value = true
  let ok = 0, fail = 0
  for (const [s, t] of pairs) {
    try {
      if (isRevoke) await agentsStore.removeAgentPermission(s, t)
      else await agentsStore.addAgentPermission(s, t)
      ok++
    } catch { fail++ }
  }
  await refreshEdges()
  busy.value = false
  flash(fail ? 'error' : 'success',
    `${isRevoke ? 'Revoked' : 'Granted'} ${ok} pair${ok === 1 ? '' : 's'}${fail ? `, ${fail} failed (no container or not owned)` : ''}.`)
}

onMounted(reload)
</script>

<style scoped>
/* Hatched diagonal for self cells (agent can't call itself). */
.self-diag {
  background-image: repeating-linear-gradient(
    45deg,
    rgba(148, 163, 184, 0.25),
    rgba(148, 163, 184, 0.25) 3px,
    transparent 3px,
    transparent 6px
  );
}
:root.dark .self-diag,
.dark .self-diag {
  background-image: repeating-linear-gradient(
    45deg,
    rgba(100, 116, 139, 0.35),
    rgba(100, 116, 139, 0.35) 3px,
    transparent 3px,
    transparent 6px
  );
}
/* Split corner cell diagonal divider. */
.corner-split {
  background-image: linear-gradient(to top right, transparent calc(50% - 0.5px), rgba(148, 163, 184, 0.5) 50%, transparent calc(50% + 0.5px));
}
/* Rotate column labels for density on wide fleets. */
.col-label {
  writing-mode: vertical-rl;
  transform: rotate(180deg);
  max-height: 8rem;
  overflow: hidden;
  text-overflow: ellipsis;
}
</style>
