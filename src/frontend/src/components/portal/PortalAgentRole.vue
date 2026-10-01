<template>
  <!-- ent#527: no role → nothing rendered at all (the panel is unchanged). -->
  <section v-if="card && (card.role || card.unavailable)" data-testid="portal-agent-role">
    <h2 class="text-[11px] font-semibold uppercase tracking-wide text-gray-400 mb-2">Role</h2>

    <!-- A stopped agent: the files live in the container. -->
    <p v-if="card.unavailable" class="text-sm text-gray-400" data-testid="portal-role-unavailable">
      {{ card.unavailable === 'agent_stopped'
        ? 'The agent is stopped — the role card reads its files when it runs.'
        : 'The agent is unreachable right now — the role card reads its files when it runs.' }}
    </p>

    <template v-else>
      <!-- Role: the file, or the reason it did not load. -->
      <div class="rounded-xl border border-gray-200 dark:border-gray-800 px-3 py-2.5">
        <div class="flex items-center gap-2 flex-wrap">
          <span class="text-sm font-medium" data-testid="portal-role-title">{{ card.role.title || card.role.id || 'Role' }}</span>
          <BaseBadge v-if="card.role.status" :variant="card.role.status === 'active' ? 'success' : 'warning'">{{ card.role.status }}</BaseBadge>
          <BaseBadge v-if="card.role.stale" variant="warning" dot>past review</BaseBadge>
        </div>
        <p v-if="card.role.error" class="mt-1 text-xs text-status-warning-700 dark:text-status-warning-300" data-testid="portal-role-error">
          {{ roleErrorText(card.role.error) }}
        </p>
        <p v-else-if="card.role.mission" class="mt-1 text-sm">{{ card.role.mission }}</p>
        <p v-if="card.role.path" class="mt-1 text-[11px] text-gray-400 font-mono break-all">{{ card.role.path }}</p>
        <p v-if="card.seat" class="mt-1 text-[11px] text-gray-400">Seat · {{ card.seat }}</p>
      </div>

      <!-- Objectives with metric freshness — the portal projection of the ONE
           objective ↔ metric join (ent#676). An agent that simply has none
           renders nothing here, as before. -->
      <template v-if="card.objectives.length || card.objectives_error">
        <h3 class="mt-3 mb-1 text-[11px] font-medium text-gray-400">Objectives</h3>
        <!-- Zero objectives for a NAMED reason: a read that did not happen is
             never shown as "this agent has none" (principle 15). -->
        <p v-if="!card.objectives.length" class="text-[12.5px] text-status-warning-700 dark:text-status-warning-300" data-testid="portal-role-objectives-error">
          {{ objectivesErrorText(card.objectives_error) }}
        </p>
        <ul v-else class="space-y-1.5">
          <li v-for="o in card.objectives" :key="o.id"
              class="rounded-lg border border-gray-200 dark:border-gray-800 px-3 py-2 text-[12.5px]"
              data-testid="portal-role-objective">
            <div class="flex items-center gap-2 flex-wrap">
              <span class="font-medium">{{ o.statement || o.id }}</span>
              <span class="text-gray-400">{{ o.owned ? 'owns' : 'supports' }}<template v-if="o.horizon"> · {{ o.horizon }}</template></span>
            </div>
            <ul v-if="o.metrics.length" class="mt-1 space-y-0.5">
              <li v-for="m in o.metrics" :key="m.name"
                  :data-testid="m.stale ? 'portal-role-metric-stale' : 'portal-role-metric'">
                <!-- Wraps: two badges beside the numbers must not crush the name in a narrow rail. -->
                <div class="flex items-center gap-x-2 gap-y-0.5 flex-wrap tabular-nums">
                  <span class="font-mono text-[11.5px] min-w-0 truncate">{{ m.name }}</span>
                  <span class="text-gray-400">{{ shown(m.actual, m) }}<template v-if="m.target != null"> / {{ shown(m.target, m) }}</template></span>
                  <!-- Position against the target, never pace. Stale is orthogonal: both can show. -->
                  <BaseBadge v-if="gapBadge(m)" :variant="gapBadge(m).variant" dot data-testid="portal-role-metric-gap">{{ gapBadge(m).label }}</BaseBadge>
                  <!-- Freshness is the backend's verdict (the one 2× cadence rule) — never recomputed here. -->
                  <BaseBadge v-if="m.stale" variant="warning" dot :title="m.last_point_at || undefined">stale</BaseBadge>
                  <span v-else-if="m.freshness === 'no_points'" class="text-[11px] text-gray-400">no points yet</span>
                  <span v-else-if="m.last_point_at" class="text-[11px] text-gray-400" :title="m.last_point_at">as of {{ relative(m.last_point_at) }}</span>
                </div>
                <!-- A finding is never a blank: the Workspace's own sentence for its code. -->
                <p v-if="m.finding" class="text-[11px] text-status-warning-700 dark:text-status-warning-300" data-testid="portal-role-metric-finding">
                  {{ findingText(m.finding.code) }}
                </p>
              </li>
            </ul>
          </li>
        </ul>
        <!-- Some objectives joined but some files were not read: a partial list
             must not look like a complete one (principle 15). -->
        <p v-if="card.objectives.length && card.objectives_partial"
           class="mt-1.5 text-[12.5px] text-status-warning-700 dark:text-status-warning-300"
           data-testid="portal-role-objectives-partial">
          Some objective files in the agent's canon couldn't be read, so this list may be incomplete.
        </p>
      </template>

      <!-- Your relationship (ent#500 when it lands; stated, never blank). -->
      <p class="mt-3 text-[12.5px]" data-testid="portal-role-relationship">
        <span class="text-gray-400">Your relationship · </span>{{ card.relationship || 'no assignment recorded' }}
      </p>

      <!-- Readiness: the owner's stamp, never the template's word. -->
      <div class="mt-3 rounded-lg border border-gray-200 dark:border-gray-800 px-3 py-2 text-[12.5px]" data-testid="portal-role-readiness">
        <div class="flex items-center gap-2 flex-wrap">
          <span class="text-gray-400">Readiness</span>
          <BaseBadge :variant="readiness.status === 'ready' ? 'success' : 'warning'" dot>{{ readiness.status }}</BaseBadge>
          <span v-if="readiness.changed_at" class="text-gray-400">
            since <span :title="readiness.changed_at">{{ relative(readiness.changed_at) }}</span>
            <template v-if="readiness.source === 'rollout'"> · carried over when the readiness gate shipped</template>
            <template v-else-if="readiness.changed_by"> · by {{ readiness.changed_by }}</template>
          </span>
        </div>
        <!-- ent#689: the gate holds a calibrating companion's scheduled brief;
             the card says so beside the control that releases it. -->
        <p v-if="card.brief_held" class="mt-1 text-status-warning-700 dark:text-status-warning-300" data-testid="portal-role-brief-held">
          Its scheduled brief is paused until {{ card.can_flip_readiness ? 'you mark' : 'its owner marks' }} it ready.
        </p>
        <p v-if="readiness.unstamped_ready" class="mt-1 text-status-warning-700 dark:text-status-warning-300" data-testid="portal-role-unstamped">
          The agent's file says <code class="font-mono">ready</code>, but no owner has stamped it — only the owner's stamp counts.
        </p>
        <p v-if="readiness.status === 'calibrating' && card.walkthrough && !card.walkthrough.unavailable" class="mt-1 text-gray-400" data-testid="portal-role-walkthrough">
          Your walkthrough: {{ card.walkthrough.asks }} of {{ card.walkthrough.target }} asks
          <template v-if="card.walkthrough.rated_down"> · {{ card.walkthrough.rated_down }} rated down</template>
        </p>
        <InlineError v-if="flipError" class="mt-2" :message="flipError" @dismiss="store.roleFlipError = null" />
        <div v-if="card.can_flip_readiness" class="mt-2">
          <BaseButton
            size="sm"
            :variant="readiness.status === 'ready' ? 'secondary' : 'primary'"
            data-testid="portal-role-flip"
            :loading="store.roleFlipping"
            :loading-label="readiness.status === 'ready' ? 'Reverting…' : 'Marking ready…'"
            @click="confirmOpen = true"
          >{{ readiness.status === 'ready' ? 'Back to calibrating' : 'Mark ready' }}</BaseButton>
        </div>
      </div>

      <ConfirmDialog
        v-model:visible="confirmOpen"
        :title="readiness.status === 'ready' ? 'Back to calibrating?' : 'Mark this companion ready?'"
        :message="readiness.status === 'ready'
          ? 'Its readiness returns to calibrating, stamped with your name and today. Nothing else changes.'
          : 'This records that the walkthrough passed — stamped with your name and today. It does not switch any schedule on; that stays a deliberate act.'"
        :confirm-text="readiness.status === 'ready' ? 'Back to calibrating' : 'Mark ready'"
        variant="warning"
        @confirm="flip"
      />
    </template>
  </section>
</template>

<script setup>
/**
 * ent#527 / #663 — the role card: a PROJECTION of the agent's own files (role,
 * objectives, metric freshness) plus the one platform fact — the owner's
 * readiness stamp. Split out of PortalAgentDetails so the flip verb is proven
 * by mounting it (#2918).
 *
 * ent#676 — the objectives are the portal projection of the one objective ↔
 * metric join: numbers, `gap.status` and `freshness` arrive decided, and a
 * finding arrives as a CODE. The sentences for those codes live here, because
 * the operator's own are remediation a Workspace client cannot act on.
 */
import { computed, ref, watch, onMounted } from 'vue'
import { useClientPortalStore } from '@/stores/clientPortal'
import { formatMetricValue, metricUnitSuffix } from '@/utils/metricFormat'
import InlineError from '@/components/InlineError.vue'
import ConfirmDialog from '@/components/ConfirmDialog.vue'
import BaseBadge from '@/components/base/BaseBadge.vue'
import BaseButton from '@/components/base/BaseButton.vue'

const props = defineProps({
  agentName: { type: String, required: true },
})

const store = useClientPortalStore()
const confirmOpen = ref(false)

const mine = computed(() => store.roleAgent === props.agentName)
const card = computed(() => (mine.value ? store.role : null))
const readiness = computed(() => card.value?.readiness || { status: 'calibrating', source: 'template' })
const flipError = computed(() => (mine.value ? store.roleFlipError : null))

function load() { return store.loadAgentRole(props.agentName) }
function flip() {
  const next = readiness.value.status === 'ready' ? 'calibrating' : 'ready'
  return store.flipAgentReadiness(props.agentName, next)
}

function roleErrorText(code) {
  return {
    role_file_not_found: "The role file is not in the agent's canon yet — it will show once the file exists.",
    role_file_unreadable: 'The role file could not be read from the agent. Try again in a moment.',
    role_file_invalid: 'The role file is not valid YAML (or not a role) — fix the file; nothing is edited here.',
    role_id_invalid: 'The role id in template.yaml is not a valid id.',
    canon_path_invalid: 'The canon path in template.yaml is not a valid path.',
  }[code] || 'The role could not be loaded.'
}

/** A value or target the way its declared type reads (the operator tiles' formatter). */
function shown(value, m) {
  if (value === null || value === undefined) return '—'
  if (typeof value !== 'number') return String(value)
  const text = formatMetricValue(value, m.type)
  const suffix = metricUnitSuffix(value, m.type, m.unit)
  if (!suffix) return text
  return suffix === '%' ? `${text}%` : `${text} ${suffix}`
}

const GAP_BADGES = {
  behind: { variant: 'warning', label: 'behind' },
  off_target: { variant: 'warning', label: 'off target' },
  ahead: { variant: 'success', label: 'ahead' },
  on_target: { variant: 'success', label: 'on target' },
}
/** `not_computable` (and anything unknown) shows no badge — the finding line says why. */
function gapBadge(m) {
  return GAP_BADGES[m.gap?.status] || null
}

function findingText(code) {
  return {
    metric_undeclared: "This metric isn't being measured yet, so there is no number to show.",
    metric_not_declared_here: 'Another agent measures this one. This agent supports the objective without tracking the number.',
    metric_retired: "This metric is no longer measured, so its last number isn't shown.",
    direction_mismatch: "The objective and the metric disagree on which way is good. The comparison follows the metric's own setting.",
    direction_undeclared: "Nothing says whether higher or lower is better here, so it can't be compared with its target.",
  }[code] || "This metric can't be compared with its target right now."
}

function objectivesErrorText(code) {
  return {
    objectives_rate_limited: "The agent's objectives are being read a lot right now. Try again in a minute.",
    objectives_timeout: "The agent took too long to answer, so its objectives couldn't be read. Try again in a moment.",
    objectives_unreadable: "The agent's objectives couldn't be read right now. Try again in a moment.",
    agent_unreachable: 'The agent stopped answering while its objectives were being read. Try again in a moment.',
    objectives_incomplete: "Some objective files in the agent's canon couldn't be read, so none are shown here.",
  }[code] || "The agent's objectives couldn't be read."
}

watch(() => props.agentName, load)
onMounted(load)

function relative(iso) {
  if (!iso) return ''
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return ''
  const mins = Math.round((Date.now() - then) / 60000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  const hrs = Math.round(mins / 60)
  if (hrs < 24) return `${hrs}h ago`
  const days = Math.round(hrs / 24)
  return days < 30 ? `${days}d ago` : new Date(iso).toLocaleDateString()
}
</script>
