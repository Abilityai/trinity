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

      <!-- Objectives with metric freshness. -->
      <template v-if="card.objectives.length">
        <h3 class="mt-3 mb-1 text-[11px] font-medium text-gray-400">Objectives</h3>
        <ul class="space-y-1.5">
          <li v-for="o in card.objectives" :key="o.id"
              class="rounded-lg border border-gray-200 dark:border-gray-800 px-3 py-2 text-[12.5px]"
              data-testid="portal-role-objective">
            <div class="flex items-center gap-2 flex-wrap">
              <span class="font-medium">{{ o.statement || o.id }}</span>
              <span class="text-gray-400">{{ o.owned ? 'owns' : 'supports' }}<template v-if="o.horizon"> · {{ o.horizon }}</template></span>
            </div>
            <ul v-if="o.metrics.length" class="mt-1 space-y-0.5">
              <li v-for="m in o.metrics" :key="m.name" class="flex items-center gap-2 tabular-nums"
                  :data-testid="m.stale ? 'portal-role-metric-stale' : 'portal-role-metric'">
                <span class="font-mono text-[11.5px] min-w-0 truncate">{{ m.name }}</span>
                <span class="text-gray-400">{{ m.value ?? '—' }}<template v-if="m.target != null"> / {{ m.target }}</template></span>
                <BaseBadge v-if="m.stale" variant="warning" dot>stale</BaseBadge>
                <span v-else class="text-[11px] text-gray-400" :title="m.as_of">as of {{ relative(m.as_of) }}</span>
              </li>
            </ul>
          </li>
        </ul>
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
            <template v-if="readiness.changed_by"> · by {{ readiness.changed_by }}</template>
          </span>
        </div>
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
 */
import { computed, ref, watch, onMounted } from 'vue'
import { useClientPortalStore } from '@/stores/clientPortal'
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
