<template>
  <!--
    Agent → Payments → Spending (abilityai/trinity-enterprise#967; requirements
    §23.8 FR-5). The buyer side of Nevermined: Nevermined holds the card and the
    mandate; Trinity records only the mandate id. Rules in
    utils/neverminedSpending.js. Semantic tokens + gray neutrals only.
  -->
  <section class="px-6 pb-6" data-testid="nvm-spending-panel">
    <div class="rounded-lg border border-gray-200 dark:border-gray-750 bg-white dark:bg-gray-800">
      <header class="flex flex-wrap items-start justify-between gap-3 px-4 py-3 border-b border-gray-200 dark:border-gray-750">
        <div class="min-w-0">
          <div class="flex flex-wrap items-center gap-2">
            <h3 class="text-lg font-semibold text-gray-900 dark:text-gray-100">{{ COPY.title }}</h3>
            <BaseBadge
              v-if="view.state === 'funded'"
              :variant="badge.variant"
              data-testid="nvm-spending-status"
            >
              <span aria-hidden="true">{{ badge.glyph }}</span>
              {{ badge.label }}
            </BaseBadge>
            <BaseBadge v-else-if="view.state === 'funded_unavailable'" variant="warning" data-testid="nvm-spending-unavailable">
              <span aria-hidden="true">?</span>
              {{ COPY.unavailableBadge }}
            </BaseBadge>
          </div>
          <p class="mt-1 text-sm text-gray-600 dark:text-gray-300">{{ COPY.subtitle }}</p>
        </div>
        <BaseButton
          v-if="isReady"
          variant="ghost"
          size="sm"
          :loading="loading"
          loading-label="Refreshing…"
          data-testid="nvm-spending-refresh"
          @click="load"
        >
          {{ COPY.refresh }}
        </BaseButton>
      </header>

      <div class="p-4 space-y-4 min-h-[8rem]">
        <div v-if="view.state === 'loading'" aria-busy="true" data-testid="nvm-spending-loading">
          <span class="sr-only">{{ COPY.loading }}</span>
          <SkeletonLoader :count="3" height="1.75rem" gap="0.75rem" />
        </div>

        <LoadFailed
          v-else-if="view.state === 'failed'"
          dense
          :title="COPY.failedTitle"
          :message="COPY.failedMessage"
          :detail="error"
          :retrying="loading"
          @retry="load"
        />

        <template v-else>
          <InlineError
            v-if="view.stale"
            :message="staleMessage"
            retryable
            data-testid="nvm-spending-stale"
            @retry="load"
            @dismiss="error = ''"
          />

          <!-- Not configured: the organization is an admin, instance-wide setting (FR-1). -->
          <div v-if="view.state === 'not_configured'" class="space-y-2 text-sm" data-testid="nvm-spending-not-configured">
            <p class="text-gray-900 dark:text-gray-100">{{ COPY.notConfigured }}</p>
            <RouterLink
              v-if="isAdmin"
              to="/settings?tab=integrations"
              class="font-medium text-action-primary-700 dark:text-action-primary-300 hover:underline"
            >
              {{ COPY.notConfiguredAdminLink }}
            </RouterLink>
            <p v-else class="text-gray-600 dark:text-gray-300">{{ COPY.notConfiguredOther }}</p>
          </div>

          <!-- Not funded: Enroll for the owner of a running agent; text for everyone else. -->
          <div v-else-if="view.state === 'not_funded'" class="space-y-3 text-sm" data-testid="nvm-spending-not-funded">
            <p class="text-gray-900 dark:text-gray-100">{{ COPY.notFunded }}</p>
            <p class="text-gray-600 dark:text-gray-300">{{ COPY.expectation }}</p>
            <template v-if="canEdit">
              <BaseButton
                variant="primary"
                :disabled="!view.canEnroll"
                data-testid="nvm-spending-enroll"
                @click="startEnroll"
              >
                {{ COPY.enroll }}
              </BaseButton>
              <p
                v-if="view.enrollBlocked === 'not_running'"
                class="text-gray-600 dark:text-gray-300"
                data-testid="nvm-spending-not-running"
              >
                {{ COPY.notRunning }}
              </p>
            </template>
            <p v-else class="text-gray-600 dark:text-gray-300">{{ COPY.sharedViewer }}</p>
          </div>

          <!-- Funded: Nevermined's live numbers, never a local estimate. Revoke only — no replace. -->
          <div v-else class="space-y-4 text-sm" data-testid="nvm-spending-funded">
            <p v-if="view.state === 'funded_unavailable'" class="text-gray-600 dark:text-gray-300">
              {{ COPY.unavailable }}
            </p>

            <dl class="grid grid-cols-2 sm:grid-cols-3 gap-3" data-testid="nvm-spending-kpis">
              <div v-for="kpi in kpis" :key="kpi.label" class="min-w-0">
                <dt class="font-mono text-[11px] uppercase tracking-wide text-gray-500 dark:text-gray-400">{{ kpi.label }}</dt>
                <dd
                  class="mt-0.5 font-medium tabular-nums text-gray-900 dark:text-gray-100 truncate"
                  :title="kpi.title || undefined"
                  :data-testid="kpi.testid || undefined"
                >
                  {{ kpi.value }}
                </dd>
              </div>
            </dl>

            <div class="space-y-1 text-[12.5px] text-gray-600 dark:text-gray-300">
              <p>{{ COPY.nextRun }}</p>
              <p v-if="live?.fetched_at" :title="absolute(live.fetched_at)">
                {{ COPY.readFrom }} {{ relative(live.fetched_at) }}
              </p>
              <p class="break-all">
                {{ COPY.mandateId }}
                <span class="font-mono">{{ idTail(delegation?.delegation_id) }}</span>
                <template v-if="delegation?.provider"> · {{ delegation.provider }}</template>
                <template v-if="delegation?.environment"> · {{ delegation.environment }}</template>
              </p>
              <p v-if="canEdit && delegation?.created_by" data-testid="nvm-spending-bound-by">
                {{ COPY.boundBy }} {{ delegation.created_by }}
                <span :title="absolute(delegation.created_at)">{{ relative(delegation.created_at) }}</span>
              </p>
              <a
                href="https://nevermined.app"
                target="_blank"
                rel="noopener noreferrer"
                class="inline-block font-medium text-action-primary-700 dark:text-action-primary-300 hover:underline"
              >
                {{ COPY.transactionsLink }} ↗
              </a>
            </div>

            <BaseButton
              v-if="view.canRevoke"
              variant="danger"
              size="sm"
              data-testid="nvm-spending-revoke"
              @click="startRevoke"
            >
              {{ COPY.revoke }}
            </BaseButton>
          </div>

          <!-- Past mandates: collapsed, bounded (≤10 rows from the GET), total stated. -->
          <details
            v-if="history.length"
            class="text-[12.5px] text-gray-600 dark:text-gray-300"
            data-testid="nvm-spending-history"
          >
            <summary class="cursor-pointer select-none font-medium">
              {{ COPY.pastMandates }} ({{ history.length }})
            </summary>
            <div class="mt-2 max-h-48 overflow-y-auto rounded-md border border-gray-200 dark:border-gray-750">
              <table class="w-full text-left">
                <thead class="sticky top-0 bg-gray-100 dark:bg-gray-750">
                  <tr class="font-mono text-[11px] uppercase tracking-wide text-gray-600 dark:text-gray-300">
                    <th class="px-3 py-1.5 font-normal">Mandate</th>
                    <th class="px-3 py-1.5 font-normal">Created</th>
                    <th class="px-3 py-1.5 font-normal">Revoked</th>
                  </tr>
                </thead>
                <tbody>
                  <tr
                    v-for="row in history"
                    :key="row.delegation_id + row.created_at"
                    class="border-t border-gray-200 dark:border-gray-750"
                  >
                    <td class="px-3 py-1.5 font-mono">{{ idTail(row.delegation_id) }}</td>
                    <td class="px-3 py-1.5 tabular-nums" :title="absolute(row.created_at)">{{ relative(row.created_at) }}</td>
                    <td class="px-3 py-1.5 tabular-nums" :title="absolute(row.revoked_at)">
                      {{ row.revoked_at ? relative(row.revoked_at) : 'active' }}
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </details>
        </template>
      </div>
    </div>
  </section>
</template>

<script setup>
import { computed, onActivated, onMounted, onUnmounted, ref, watch } from 'vue'
import api from '../api'
import { useAuthStore } from '../stores/auth'
import BaseBadge from './base/BaseBadge.vue'
import BaseButton from './base/BaseButton.vue'
import InlineError from './InlineError.vue'
import LoadFailed from './LoadFailed.vue'
import SkeletonLoader from './SkeletonLoader.vue'
import { apiErrorMessage } from '../utils/apiError'
import { staleBannerMessage } from '../utils/loadingState'
import { formatLocalDateTime, formatRelativeTime } from '../utils/timestamps'
import {
  COPY,
  UNKNOWN,
  expiryParts,
  formatCents,
  formatCount,
  idTail,
  spendingView,
  statusBadge,
} from '../utils/neverminedSpending'

const props = defineProps({
  agentName: { type: String, required: true },
  // Owner tier (`agent.can_share`): gates Enroll / Revoke. UX only — the backend enforces.
  canEdit: { type: Boolean, default: false },
  // `agent.status`: Enroll needs a running agent (bind writes its .env, FR-3).
  agentStatus: { type: String, default: '' },
})

const authStore = useAuthStore()
// The `role` getter, not `userRole` (the ent#437 trap).
const isAdmin = computed(() => authStore.role === 'admin')

const data = ref(null)
const hasLoaded = ref(false)
const loading = ref(false)
const error = ref('')
const lastLoadedAt = ref(null)

const view = computed(() => spendingView({
  data: data.value,
  hasLoaded: hasLoaded.value,
  error: error.value,
  canEdit: props.canEdit,
  agentStatus: props.agentStatus,
}))
const isReady = computed(() => !['loading', 'failed'].includes(view.value.state))
const staleMessage = computed(() => staleBannerMessage(COPY.staleSubject, lastLoadedAt.value))

const delegation = computed(() => data.value?.delegation || null)
const live = computed(() => data.value?.live || null)
const history = computed(() => (Array.isArray(data.value?.history) ? data.value.history : []))
const badge = computed(() => statusBadge(live.value?.status))

const kpis = computed(() => {
  const l = live.value || {}
  const known = view.value.state === 'funded'
  const money = (c) => (known ? formatCents(c, l.currency) : UNKNOWN)
  const exp = expiryParts(l.expires_at)
  let purchases = UNKNOWN
  if (known) {
    purchases = typeof l.max_transactions === 'number'
      ? `${formatCount(l.transactions_used)} of ${l.max_transactions}`
      : formatCount(l.transactions_used)
  }
  return [
    { label: 'Remaining', value: money(l.remaining_cents) },
    { label: 'Limit', value: money(l.spending_limit_cents) },
    { label: 'Spent', value: money(l.spent_cents) },
    { label: 'Purchases', value: purchases },
    {
      label: 'Expires',
      value: known ? exp.relative : UNKNOWN,
      title: known ? exp.absolute : '',
      testid: 'nvm-spending-expires',
    },
  ]
})

function relative(ts) { return ts ? formatRelativeTime(ts) : '' }
function absolute(ts) { return ts ? formatLocalDateTime(ts) : undefined }

// Generation guard: a read for a superseded request, another agent, or an
// unmounted panel is dropped at every exit (try / catch / finally).
let generation = 0

async function load() {
  const mine = ++generation
  const name = props.agentName
  loading.value = true
  try {
    const { data: body } = await api.get(`/api/nevermined/agents/${name}/delegation`)
    if (mine !== generation) return
    if (!body || body.agent_name !== name) {
      error.value = 'Nevermined spending answered for a different agent.'
      return
    }
    data.value = body
    hasLoaded.value = true
    lastLoadedAt.value = Date.now()
    error.value = ''
  } catch (err) {
    if (mine !== generation) return
    // Keep the last data on screen (ent#253): never a synthetic empty payload.
    error.value = apiErrorMessage(err, 'The request failed.')
  } finally {
    if (mine === generation) loading.value = false
  }
}

function reset() {
  generation++
  data.value = null
  hasLoaded.value = false
  loading.value = false
  error.value = ''
  lastLoadedAt.value = null
}

// Checkpoint A stubs: the Nevermined card form (setupCard) and the revoke
// confirmation arrive in checkpoint B, which replaces both handlers.
function startEnroll() {}
function startRevoke() {}

// The panel lives inside the KeepAlive-cached AgentDetail: onMounted and
// onActivated both fire on first entry, so the first activate is skipped.
let skipActivate = false
onMounted(() => { skipActivate = true; load() })
onActivated(() => {
  if (skipActivate) { skipActivate = false; return }
  load() // background re-read: data stays, no skeleton
})
onUnmounted(() => { generation++ })

watch(() => props.agentName, () => { reset(); load() })
</script>
