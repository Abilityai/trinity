<template>
  <div class="gtile" :class="{ system: isSystemAgent }">
    <!-- Avatar half-out on the left edge (same convention as AgentNode) -->
    <div class="gtile-avatar">
      <div
        class="rounded-full border-2 shadow-md overflow-hidden"
        :class="isSystemAgent ? 'border-accent-purple-400 dark:border-accent-purple-500' : 'border-action-primary-400 dark:border-action-primary-500'"
      >
        <AgentAvatar :name="agent.name" :avatar-url="agent.avatar_url" size="lg" />
      </div>
    </div>

    <!-- Zone 1: identity -->
    <div class="t-idrow">
      <div class="t-id">
        <div class="t-nameline">
          <span
            class="t-name nodrag"
            :title="agent.name"
            @click="viewDetails"
          >{{ agent.name }}</span>
          <RuntimeBadge :runtime="agent.runtime || 'claude-code'" :show-label="false" class="flex-none" />
          <span
            v-if="isSystemAgent"
            class="sys-badge"
            title="System Agent - Platform Orchestrator"
          >SYSTEM</span>
        </div>
        <div class="t-repo" :class="{ local: !githubRepoShort }">
          <svg v-if="githubRepoShort" viewBox="0 0 24 24"><path fill-rule="evenodd" d="M12 2C6.477 2 2 6.477 2 12c0 4.42 2.865 8.17 6.839 9.49.5.092.682-.217.682-.482 0-.237-.008-.866-.013-1.7-2.782.604-3.369-1.34-3.369-1.34-.454-1.156-1.11-1.464-1.11-1.464-.908-.62.069-.608.069-.608 1.003.07 1.531 1.03 1.531 1.03.892 1.529 2.341 1.087 2.91.831.092-.646.35-1.086.636-1.336-2.22-.253-4.555-1.11-4.555-4.943 0-1.091.39-1.984 1.029-2.683-.103-.253-.446-1.27.098-2.647 0 0 .84-.269 2.75 1.025A9.578 9.578 0 0112 6.836c.85.004 1.705.115 2.504.337 1.909-1.294 2.747-1.025 2.747-1.025.546 1.377.203 2.394.1 2.647.64.699 1.028 1.592 1.028 2.683 0 3.842-2.339 4.687-4.566 4.935.359.309.678.919.678 1.852 0 1.336-.012 2.415-.012 2.743 0 .267.18.578.688.48C19.138 20.167 22 16.418 22 12c0-5.523-4.477-10-10-10z" clip-rule="evenodd" /></svg>
          <span :title="githubRepoShort || 'Local agent'">{{ githubRepoShort || 'Local agent' }}</span>
        </div>
      </div>
      <div class="t-staterow">
        <span class="t-state" :class="{ off: stateWord === 'Offline' }">{{ stateWord }}</span>
        <span class="t-dot" :class="dotClass"></span>
      </div>
    </div>

    <!-- Zone 2: adaptive chip strip — problems escalate to the front -->
    <div class="t-chips">
      <span
        v-for="(chip, i) in chips"
        :key="i"
        class="chip"
        :class="chip.kind"
        :title="chip.title"
      >{{ chip.icon ? chip.icon + ' ' : '' }}{{ chip.text }}<span v-if="chip.timer" class="tmr">&nbsp;{{ chip.timer }}</span></span>
    </div>

    <!-- Zone 3: twin trend charts -->
    <div class="t-charts">
      <!-- Activity · 14d — stacked daily bars by trigger bucket -->
      <div class="mini" title="Executions per day by trigger type, last 14 days">
        <div class="mlbl">
          <span>Activity · 14d</span>
          <b v-if="analytics" :class="activityTotal > 0 ? 'info' : 'na'">{{ activityTotal }}</b>
          <b v-else class="na">&mdash;</b>
        </div>
        <div v-if="!analytics && analyticsPending" class="chartbox skeleton"></div>
        <!-- Empty days keep a faint baseline stub so sparse data reads as a
             14-day rhythm instead of a lone bar floating in a void. -->
        <div v-else-if="activityDays.length" class="stack">
          <span v-for="(d, i) in activityDays" :key="i" class="col" :title="d.date + ' — ' + d.total + ' runs'">
            <template v-if="d.total > 0">
              <i v-if="d.sched" class="bs" :style="{ height: d.schedPx + 'px' }"></i>
              <i v-if="d.man" class="bm" :style="{ height: d.manPx + 'px' }"></i>
              <i v-if="d.ext" class="be" :style="{ height: d.extPx + 'px' }"></i>
            </template>
            <i v-else class="stub"></i>
          </span>
        </div>
        <div v-else class="stack flat"><span class="flatline"></span></div>
      </div>

      <!-- Context · 7d — miniature trend line, colored by current level -->
      <div class="mini" :title="contextTooltip">
        <div class="mlbl">
          <span>Context · 7d</span>
          <b :class="contextHeadline == null ? 'na' : contextLevelClass">{{ contextHeadline == null ? '—' : contextHeadline + '%' }}</b>
        </div>
        <div v-if="!analytics && analyticsPending" class="chartbox skeleton"></div>
        <div v-else-if="contextPoints" class="chartbox">
          <svg class="ctxsvg" viewBox="0 0 160 32" preserveAspectRatio="none">
            <path class="ctxarea" :class="contextLevelClass" :d="contextPoints.area"></path>
            <polyline class="ctxline" :class="contextLevelClass" :points="contextPoints.line"></polyline>
            <circle class="ctxdot" :class="contextLevelClass" :cx="contextPoints.lastX" :cy="contextPoints.lastY" r="2.6"></circle>
          </svg>
        </div>
        <div v-else class="chartbox"><div class="ctxflat"></div></div>
      </div>
    </div>

    <!-- Zone 4: success micro-meter + stats -->
    <div class="t-statrow">
      <span class="sucmini">
        <span class="slbl">Success</span>
        <template v-if="hasTasks">
          <span class="sbar"><i :class="successClass" :style="{ width: successRate + '%' }"></i></span>
          <b :class="successClass">{{ successRate }}%</b>
        </template>
        <b v-else class="na">&mdash;</b>
      </span>
      <span v-if="hasTasks" class="t-stats">
        <b>{{ stats.taskCount }}</b> tasks <span class="dim">·</span>
        <b>{{ formatCostCompact(stats.totalCost || 0) }}</b> <span class="dim">·</span>
        {{ lastExecutionDisplay }}
      </span>
      <span v-else class="t-stats empty">No tasks (24h)</span>
    </div>

    <!-- Zone 5: actions -->
    <div class="t-actions">
      <template v-if="!isSystemAgent">
        <span class="tgl-group">
          <label>Run
            <RunningStateToggle
              :model-value="isRunning"
              :loading="runningLoading"
              :show-label="false"
              size="sm"
              class="nodrag"
              @toggle="handleRunningToggle"
            />
          </label>
          <label>Auto
            <AutonomyToggle
              :model-value="agent.autonomy_enabled === true"
              :loading="autonomyLoading"
              :show-label="false"
              size="sm"
              class="nodrag"
              @toggle="handleAutonomyToggle"
            />
          </label>
        </span>
        <button type="button" class="t-btn nodrag" @click="viewDetails">Details</button>
      </template>
      <template v-else>
        <span class="t-sysnote">Platform orchestrator · autonomous</span>
        <router-link to="/system-agent" class="t-btn sys nodrag">System Dashboard</router-link>
      </template>
    </div>
  </div>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { formatCostCompact } from '../composables/useFormatters'
import AgentAvatar from './AgentAvatar.vue'
import RuntimeBadge from './RuntimeBadge.vue'
import RunningStateToggle from './RunningStateToggle.vue'
import AutonomyToggle from './AutonomyToggle.vue'
import { useNetworkStore } from '@/stores/network'
import { useFleetGridStore } from '@/stores/fleetGrid'

/**
 * AgentTile (trinity-enterprise#47) — 384×216 landscape tile for the
 * Dashboard Grid view. Five zones per the approved design of record:
 * identity / adaptive chips / twin trend charts / success+stats / actions.
 * Composes the existing AgentAvatar, RuntimeBadge, RunningStateToggle and
 * AutonomyToggle — only the card layout is new. No Vue Flow dependency.
 *
 * Data comes reactively from stores the Dashboard already keeps warm
 * (context/execution/slot stats) plus the fleetGrid store's lazily hydrated
 * per-agent analytics — the tile itself never fetches; it only asks the
 * store to hydrate when its parent marks it visible.
 */
const props = defineProps({
  agent: { type: Object, required: true },
  // Shared 1s tick from FleetGrid (single interval for all tiles) driving
  // the live "working" elapsed timer.
  now: { type: Number, default: 0 },
})

const router = useRouter()
const networkStore = useNetworkStore()
const gridStore = useFleetGridStore()

const name = computed(() => props.agent.name)
const isSystemAgent = computed(() => props.agent.is_system === true)
const isRunning = computed(() => props.agent.status === 'running')

// --- Zone 1: identity ---
const githubRepoShort = computed(() => {
  const repo = props.agent.github_repo
  if (!repo) return null
  if (repo.startsWith('github:')) return repo.substring(7)
  // URL form: extract owner/repo via a real hostname check, not a substring
  // match (CodeQL js/incomplete-url-substring-sanitization).
  try {
    const url = new URL(repo)
    if (url.hostname === 'github.com' || url.hostname === 'www.github.com') {
      return url.pathname.replace(/^\//, '').replace(/\.git$/, '')
    }
  } catch {
    // not a URL — plain "owner/repo" falls through
  }
  return repo
})

const ctxStats = computed(() => networkStore.contextStats[name.value] || null)

// Working = a WS-observed in-flight execution (fast path), falling back to
// the polled context-stats activity state (15s granularity).
const workingInfo = computed(() => {
  const ws = networkStore.workingState[name.value]
  if (ws) return ws
  if (isRunning.value && ctxStats.value?.activityState === 'active') {
    return { since: ctxStats.value.lastActivityTime || null }
  }
  return null
})

const stateWord = computed(() => {
  if (!isRunning.value) return 'Offline'
  return workingInfo.value ? 'Active' : 'Idle'
})

const dotClass = computed(() => {
  if (!isRunning.value) return ''
  return workingInfo.value ? 'active' : 'green'
})

// --- Zone 2: adaptive chips ---
function ageLabel(iso) {
  if (!iso) return ''
  const diffMs = props.now - new Date(iso).getTime()
  const mins = Math.floor(diffMs / 60000)
  if (mins < 1) return 'now'
  if (mins < 60) return `${mins}m`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours}h`
  return `${Math.floor(hours / 24)}d`
}

function fmtTimer(iso) {
  const diffS = Math.max(0, Math.floor((props.now - new Date(iso).getTime()) / 1000))
  return `${Math.floor(diffS / 60)}m ${String(diffS % 60).padStart(2, '0')}s`
}

const stats = computed(() => networkStore.executionStats[name.value] || null)

const chips = computed(() => {
  const out = []
  const circuit = networkStore.circuitBreakers[name.value]
  if (circuit?.state === 'open') {
    out.push({ kind: 'crit', icon: '⚡', text: 'circuit open', title: 'Dispatch circuit breaker OPEN — new tasks fast-fail until it recovers' })
  }
  const oq = gridStore.opQueuePending[name.value]
  if (oq?.count) {
    const label = oq.hasApproval ? 'approval pending' : 'needs response'
    out.push({ kind: 'warn', icon: '⚠', text: `${label} · ${ageLabel(oq.oldestCreatedAt)}`, title: `${oq.count} pending operator-queue item${oq.count > 1 ? 's' : ''}` })
  }
  if (workingInfo.value) {
    out.push({
      kind: 'work',
      icon: '▶',
      text: 'working',
      timer: workingInfo.value.since ? fmtTimer(workingInfo.value.since) : null,
      title: 'Executing now',
    })
  }
  const sh = gridStore.syncHealth[name.value]
  if (sh && sh.last_sync_status === 'failed' && sh.consecutive_failures > 0) {
    out.push({ kind: 'warn', icon: '⟳', text: `sync failing ×${sh.consecutive_failures}`, title: sh.last_error_summary || 'Git sync failing' })
  }
  // Calm facts after problems. The system agent gets these too — an empty
  // chip row leaves a visual void on the tile.
  const total = stats.value?.schedulesTotal || 0
  const enabled = stats.value?.schedulesEnabled || 0
  if (total > 0) {
    if (props.agent.autonomy_enabled || isSystemAgent.value) {
      out.push({ kind: 'calm', text: `${enabled}/${total} schedules` })
    } else {
      out.push({ kind: 'calm', text: 'schedules paused' })
    }
  } else {
    out.push({ kind: 'calm', text: 'no schedules' })
  }
  if (sh && sh.auto_sync_enabled && sh.last_sync_status === 'success') {
    out.push({ kind: 'calm', text: 'git ✓' })
  }
  return out
})

// --- Zone 3: charts ---
// Collapse the backend's #1107 trigger buckets to the tile's three groups.
const BUCKET_GROUPS = {
  Scheduled: 'sched',
  'Chat/Tasks': 'man',
  MCP: 'man',
  Loops: 'man',
  'Agent-to-agent': 'man',
  Other: 'man',
  Channels: 'ext',
  Public: 'ext',
  Voice: 'ext',
}

const analytics = computed(() => gridStore.analyticsFor(name.value))
const analyticsPending = computed(() => {
  const s = gridStore.analyticsState[name.value]
  return !s || s === 'loading'
})

const activityDays = computed(() => {
  const timeline = analytics.value?.timeline || []
  const days = timeline.slice(-14).map((d) => {
    let sched = 0
    let man = 0
    let ext = 0
    for (const [bucket, n] of Object.entries(d.by_type || {})) {
      const g = BUCKET_GROUPS[bucket] || 'man'
      if (g === 'sched') sched += n
      else if (g === 'ext') ext += n
      else man += n
    }
    return { date: d.date, total: sched + man + ext, sched, man, ext }
  })
  const maxDay = Math.max(1, ...days.map((d) => d.total))
  const scale = 30 / maxDay
  for (const d of days) {
    d.schedPx = d.sched ? Math.max(2, Math.round(d.sched * scale)) : 0
    d.manPx = d.man ? Math.max(2, Math.round(d.man * scale)) : 0
    d.extPx = d.ext ? Math.max(2, Math.round(d.ext * scale)) : 0
  }
  return days
})

const activityTotal = computed(() => analytics.value?.total_executions ?? 0)

const contextMax = computed(() => ctxStats.value?.contextMax || 200000)

// Live context level drives the color + headline; per-day analytics draw the line.
const contextHeadline = computed(() => {
  if (!isRunning.value) return null
  const pct = ctxStats.value?.contextPercent
  if (pct == null) return null
  const rounded = Math.round(pct)
  // 0% with no daily history means "nothing tracked yet" — show the empty
  // dash rather than a numeric zero over a bare baseline.
  if (rounded === 0 && contextSeries.value.length === 0) return null
  return rounded
})

const contextLevelClass = computed(() => {
  const p = contextHeadline.value ?? contextSeries.value.at(-1)?.pct ?? 0
  return p >= 85 ? 'hot' : p >= 60 ? 'mid' : 'info'
})

const contextSeries = computed(() => {
  const timeline = analytics.value?.timeline || []
  return timeline
    .slice(-7)
    .filter((d) => d.context_avg != null)
    .map((d) => ({ pct: Math.min(100, Math.round((d.context_avg / contextMax.value) * 100)) }))
})

const contextPoints = computed(() => {
  const series = contextSeries.value
  if (!isRunning.value || series.length < 2) return null
  const W = 160
  const H = 32
  const n = series.length
  const pts = series.map((s, i) => [
    Math.round((i / (n - 1)) * W * 10) / 10,
    Math.round((H - 3 - (s.pct / 100) * (H - 8)) * 10) / 10,
  ])
  return {
    line: pts.map((p) => `${p[0]},${p[1]}`).join(' '),
    area: `M0,${H} L` + pts.map((p) => `${p[0]},${p[1]}`).join(' L') + ` L${W},${H} Z`,
    lastX: pts[n - 1][0],
    lastY: pts[n - 1][1],
  }
})

const contextTooltip = computed(() => {
  const used = ctxStats.value?.contextUsed
  if (used == null) return 'Avg context per day, last 7 days'
  return `Avg context per day, last 7 days — now ${Math.round(used / 1000)}k / ${Math.round(contextMax.value / 1000)}k tokens`
})

// --- Zone 4: success + stats (24h window, same thresholds as AgentNode) ---
const hasTasks = computed(() => (stats.value?.taskCount || 0) > 0)
const successRate = computed(() => Math.round(stats.value?.successRate || 0))
const successClass = computed(() =>
  successRate.value >= 90 ? 'ok' : successRate.value >= 50 ? 'mid' : 'bad'
)

const lastExecutionDisplay = computed(() => {
  const at = stats.value?.lastExecutionAt
  if (!at) return ''
  const diffMs = props.now - new Date(at).getTime()
  const mins = Math.floor(diffMs / 60000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours}h ago`
  return `${Math.floor(hours / 24)}d ago`
})

// --- Zone 5: actions ---
const autonomyLoading = ref(false)
const runningLoading = computed(() => networkStore.isTogglingRunning(name.value))

function viewDetails() {
  if (isSystemAgent.value) router.push('/system-agent')
  else router.push(`/agents/${name.value}`)
}

async function handleRunningToggle() {
  if (runningLoading.value || isSystemAgent.value) return
  await networkStore.toggleAgentRunning(name.value)
}

async function handleAutonomyToggle() {
  if (autonomyLoading.value || isSystemAgent.value) return
  autonomyLoading.value = true
  try {
    await networkStore.toggleAutonomy(name.value)
  } finally {
    autonomyLoading.value = false
  }
}

// Ask the grid store for analytics whenever the tile is (re)shown — the
// store dedupes and serves cache instantly, so this is cheap.
watch(
  name,
  (n) => {
    if (n) gridStore.hydrate(n)
  },
  { immediate: true }
)
</script>

<style scoped>
/*
 * Tile visual spec from the trinity-enterprise#47 design of record.
 * Color tokens (--gv-*) are defined once on the FleetGrid root (light +
 * dark) and inherited here via CSS custom-property cascade.
 */
.gtile {
  height: 100%;
  padding: 13px 15px 12px 40px; /* left pad clears the half-inset avatar */
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  user-select: none;
  -webkit-user-select: none;
}

.gtile-avatar {
  position: absolute;
  left: 0;
  top: 12px;
  transform: translateX(-50%);
  z-index: 3;
}

/* Zone 1 */
.t-idrow {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}
.t-id {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 1px;
}
.t-nameline {
  display: flex;
  align-items: center;
  gap: 7px;
  min-width: 0;
}
.t-name {
  font-size: 14.5px;
  font-weight: 700;
  color: var(--gv-text);
  letter-spacing: -0.005em;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  cursor: pointer;
}
.t-name:hover {
  color: var(--gv-blue);
  text-decoration: underline;
}
.sys-badge {
  flex: none;
  font-size: 9px;
  font-weight: 600;
  border-radius: 4px;
  padding: 1px 5px;
  background: var(--gv-badge-sys-bg);
  color: var(--gv-badge-sys-tx);
}
.t-repo {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 10.5px;
  color: var(--gv-muted);
  min-width: 0;
}
.t-repo svg {
  width: 11px;
  height: 11px;
  fill: currentColor;
  flex: none;
}
.t-repo.local {
  color: var(--gv-ghost);
}
.t-repo span {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.t-staterow {
  flex: none;
  display: flex;
  align-items: center;
  gap: 6px;
  align-self: flex-start;
  padding-top: 2px;
}
.t-state {
  font-size: 10.5px;
  font-weight: 500;
  color: var(--gv-green-text);
}
.t-state.off {
  color: var(--gv-faint);
}
.t-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  flex: none;
  background: var(--gv-faint);
}
.t-dot.green {
  background: var(--gv-dot-active);
}
.t-dot.active {
  background: var(--gv-dot-active);
  animation: gtile-active-pulse 0.8s ease-in-out infinite;
  box-shadow: 0 0 8px 2px rgba(16, 185, 129, 0.6);
}
@keyframes gtile-active-pulse {
  0%, 100% { transform: scale(1); opacity: 1; box-shadow: 0 0 8px 2px rgba(16, 185, 129, 0.6); }
  50% { transform: scale(1.3); opacity: 0.8; box-shadow: 0 0 16px 4px rgba(16, 185, 129, 0.9); }
}

/* Zone 2: chips */
.t-chips {
  display: flex;
  align-items: center;
  gap: 5px;
  min-width: 0;
  overflow: hidden;
}
.chip {
  flex: none;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 9.5px;
  font-weight: 600;
  border-radius: 5px;
  padding: 2px 7px;
  white-space: nowrap;
  border: 1px solid transparent;
}
.chip.calm {
  background: var(--gv-seg-bg);
  color: var(--gv-muted);
  border-color: var(--gv-border);
  font-weight: 500;
}
.chip.work {
  background: color-mix(in srgb, var(--gv-green) 12%, transparent);
  color: var(--gv-green-text);
}
.chip.warn {
  background: var(--gv-badge-warn-bg);
  color: var(--gv-badge-warn-tx);
}
.chip.crit {
  background: color-mix(in srgb, var(--gv-red) 12%, transparent);
  color: var(--gv-red-text);
}
.chip .tmr {
  font-variant-numeric: tabular-nums;
}

/* Zone 3: charts */
.t-charts {
  display: flex;
  gap: 14px;
}
.mini {
  flex: 1;
  min-width: 0;
}
.mini .mlbl {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  font-size: 9px;
  font-weight: 600;
  letter-spacing: 0.07em;
  text-transform: uppercase;
  color: var(--gv-faint);
  margin-bottom: 3px;
}
.mini .mlbl b {
  font-size: 10.5px;
  font-weight: 600;
  letter-spacing: 0;
}
.mini .mlbl b.info { color: var(--gv-blue); }
.mini .mlbl b.mid { color: var(--gv-yellow-text); }
.mini .mlbl b.hot { color: var(--gv-red-text); }
.mini .mlbl b.na { color: var(--gv-faint); }
.chartbox {
  height: 32px;
  position: relative;
}
.chartbox.skeleton {
  border-radius: 4px;
  background: var(--gv-bar-track);
  animation: gtile-skeleton 1.5s ease-in-out infinite;
}
@keyframes gtile-skeleton {
  0%, 100% { opacity: 0.5; }
  50% { opacity: 0.9; }
}

.stack {
  display: flex;
  align-items: flex-end;
  gap: 2px;
  height: 32px;
}
.stack .col {
  flex: 1;
  display: flex;
  flex-direction: column-reverse;
  gap: 1px;
  min-width: 3px;
}
.stack .col:not(:last-child) {
  opacity: 0.82;
}
.stack .col i {
  display: block;
  border-radius: 1px;
}
.stack .col i.bs { background: var(--gv-bk-sched); }
.stack .col i.bm { background: var(--gv-bk-man); }
.stack .col i.be { background: var(--gv-bk-ext); }
/* faint baseline stub for a day with zero executions */
.stack .col i.stub {
  height: 2px;
  background: var(--gv-bar-track);
  opacity: 0.7;
}
.stack.flat {
  align-items: center;
}
.stack.flat .flatline {
  height: 2px;
  width: 100%;
  background: var(--gv-bar-track);
  border-radius: 2px;
}

.ctxsvg {
  width: 100%;
  height: 100%;
  display: block;
  overflow: visible;
}
.ctxline {
  fill: none;
  stroke-width: 1.8;
  vector-effect: non-scaling-stroke;
}
.ctxline.info { stroke: var(--gv-blue); }
.ctxline.mid { stroke: var(--gv-yellow); }
.ctxline.hot { stroke: var(--gv-red); }
.ctxarea.info { fill: var(--gv-blue); opacity: 0.13; }
.ctxarea.mid { fill: var(--gv-yellow); opacity: 0.15; }
.ctxarea.hot { fill: var(--gv-red); opacity: 0.13; }
.ctxdot.info { fill: var(--gv-blue); }
.ctxdot.mid { fill: var(--gv-yellow); }
.ctxdot.hot { fill: var(--gv-red); }
/* empty context state: baseline at the bottom (mid-height read as a divider) */
.ctxflat {
  height: 2px;
  width: 100%;
  background: var(--gv-bar-track);
  border-radius: 2px;
  position: absolute;
  bottom: 3px;
  opacity: 0.7;
}

/* Zone 4: success + stats */
.t-statrow {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.sucmini {
  display: flex;
  align-items: center;
  gap: 6px;
  flex: none;
}
.sucmini .slbl {
  font-size: 9px;
  font-weight: 600;
  letter-spacing: 0.07em;
  text-transform: uppercase;
  color: var(--gv-faint);
}
.sucmini .sbar {
  width: 52px;
  height: 5px;
  border-radius: 999px;
  background: var(--gv-bar-track);
  overflow: hidden;
}
.sucmini .sbar i {
  display: block;
  height: 100%;
  border-radius: 999px;
}
.sucmini .sbar i.ok { background: var(--gv-green); }
.sucmini .sbar i.mid { background: var(--gv-yellow); }
.sucmini .sbar i.bad { background: var(--gv-red); }
.sucmini b {
  font-size: 10.5px;
  font-weight: 600;
}
.sucmini b.ok { color: var(--gv-green-text); }
.sucmini b.mid { color: var(--gv-yellow-text); }
.sucmini b.bad { color: var(--gv-red-text); }
.sucmini b.na {
  color: var(--gv-faint);
  font-weight: 500;
}
.t-stats {
  font-size: 10.5px;
  color: var(--gv-muted);
  text-align: right;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  font-variant-numeric: tabular-nums;
}
.t-stats b {
  color: var(--gv-text);
  font-weight: 600;
}
.t-stats .dim {
  color: var(--gv-ghost);
}
.t-stats.empty {
  color: var(--gv-faint);
}

/* Zone 5: actions */
.t-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.tgl-group {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 10px;
  color: var(--gv-faint);
}
.tgl-group label {
  display: flex;
  align-items: center;
  gap: 5px;
}
.t-sysnote {
  font-size: 10.5px;
  color: var(--gv-badge-sys-tx);
  font-weight: 500;
}
.t-btn {
  padding: 5px 14px;
  border-radius: 8px;
  background: var(--gv-btn-bg);
  color: var(--gv-btn-text);
  border: 1px solid var(--gv-btn-border);
  font-size: 11px;
  font-weight: 600;
  cursor: pointer;
  transition: background 0.15s ease;
  text-decoration: none;
}
.t-btn:hover {
  background: var(--gv-btn-bg-hover);
}
.t-btn:focus-visible {
  outline: 2px solid var(--gv-blue);
  outline-offset: 2px;
}
.t-btn.sys {
  background: var(--gv-sys-btn-bg);
  color: var(--gv-sys-btn-tx);
  border-color: var(--gv-sys-btn-bd);
}

@media (prefers-reduced-motion: reduce) {
  .t-dot.active {
    animation: none;
  }
  .chartbox.skeleton {
    animation: none;
  }
}
</style>
