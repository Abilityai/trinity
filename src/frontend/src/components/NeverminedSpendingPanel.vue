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
            <!-- After a revoke that could not edit the agent's .env: the backend's own sentence, verbatim. -->
            <p
              v-if="envNote"
              :class="envNote.tone === 'unreachable'
                ? 'text-status-warning-700 dark:text-status-warning-300'
                : 'text-gray-600 dark:text-gray-300'"
              :data-tone="envNote.tone"
              data-testid="nvm-spending-env-note"
            >
              <span aria-hidden="true">{{ envNote.tone === 'unreachable' ? '⚠' : 'ⓘ' }}</span>
              {{ envNote.text }}
            </p>
            <template v-if="canEdit">
              <!-- A mandate created at Nevermined but not bound here: bind the same ids, or let it go. -->
              <div
                v-if="showPendingBanner"
                class="space-y-2 rounded-md border border-status-warning-300 dark:border-status-warning-700 p-3"
                data-testid="nvm-spending-pending"
              >
                <p class="font-medium text-gray-900 dark:text-gray-100">{{ COPY.pendingBanner }}</p>
                <p class="text-[12.5px] text-gray-600 dark:text-gray-300">{{ COPY.pendingDiscardNote }}</p>
                <div class="flex flex-wrap gap-2">
                  <BaseButton size="sm" variant="primary" data-testid="nvm-spending-pending-retry" @click="retryBind">
                    {{ ACTIONS.retryBind }}
                  </BaseButton>
                  <BaseButton size="sm" variant="secondary" data-testid="nvm-spending-pending-discard" @click="discardPending">
                    {{ ACTIONS.discard }}
                  </BaseButton>
                </div>
              </div>
              <BaseButton
                v-if="!enrolling && !pending"
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
              <p
                v-if="phase === 'minting' || phase === 'widget-booting'"
                role="status"
                class="text-gray-600 dark:text-gray-300"
                data-testid="nvm-spending-opening"
              >
                {{ COPY.opening }}
              </p>
              <p v-else-if="phase === 'binding'" role="status" class="text-gray-600 dark:text-gray-300" data-testid="nvm-spending-binding">
                {{ COPY.binding }}
              </p>
              <p v-if="overlay && enrolling" class="text-gray-600 dark:text-gray-300" data-testid="nvm-spending-overlay-note">
                {{ COPY.overlayOpen }}
              </p>
              <InlineError
                v-if="enrollError"
                :message="enrollError.message"
                :detail="enrollError.detail"
                :retryable="Boolean(enrollError.retryLabel)"
                :retry-label="enrollError.retryLabel || ACTIONS.tryAgain"
                data-testid="nvm-spending-enroll-error"
                @retry="onEnrollRetry"
                @dismiss="enrollError = null"
              />
              <!--
                The Nevermined card form's slot: ONE stable element, v-show, no Vue
                children — the SDK appends its iframe here. A null ref would make the
                SDK pick its fullscreen overlay silently, and a v-if flip would unload
                a half-filled form (plan D25).
              -->
              <div
                ref="slotEl"
                v-show="enrolling && !overlay"
                class="nvm-widget-slot w-full overflow-x-auto"
                :style="slotStyle"
                data-testid="nvm-spending-widget-slot"
              ></div>
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
              :loading="revoking"
              loading-label="Revoking at Nevermined…"
              data-testid="nvm-spending-revoke"
              @click="startRevoke"
            >
              {{ COPY.revoke }}
            </BaseButton>
            <InlineError
              v-if="revokeError"
              :message="revokeError.message"
              :detail="revokeError.detail"
              :retryable="revokeError.retryable"
              data-testid="nvm-spending-revoke-error"
              @retry="doRevoke"
              @dismiss="revokeError = null"
            />
            <p v-if="data?.api_key_injected === false" class="text-[12.5px] text-gray-600 dark:text-gray-300" data-testid="nvm-spending-key-kept">
              {{ COPY.apiKeyKept }}
            </p>
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

    <ConfirmDialog
      v-model:visible="confirmRevoke"
      :title="COPY.revokeTitle"
      :message="revokeMessage(kpis[0].value)"
      :confirm-text="ACTIONS.revokeConfirm"
      variant="danger"
      @confirm="doRevoke"
    />
  </section>
</template>

<script setup>
import { computed, nextTick, onActivated, onDeactivated, onMounted, onUnmounted, ref, watch } from 'vue'
import api from '../api'
import { useAuthStore } from '../stores/auth'
import BaseBadge from './base/BaseBadge.vue'
import BaseButton from './base/BaseButton.vue'
import ConfirmDialog from './ConfirmDialog.vue'
import InlineError from './InlineError.vue'
import LoadFailed from './LoadFailed.vue'
import SkeletonLoader from './SkeletonLoader.vue'
import { apiErrorCode, apiErrorMessage } from '../utils/apiError'
import { staleBannerMessage } from '../utils/loadingState'
import { formatLocalDateTime, formatRelativeTime } from '../utils/timestamps'
import {
  ACTIONS,
  COPY,
  INLINE_HEIGHT,
  INLINE_MIN_WIDTH,
  UNKNOWN,
  clearPending,
  embedErrorMessage,
  envNoteTone,
  expiryParts,
  formatCents,
  formatCount,
  idTail,
  inlineWidth,
  persistPending,
  readPending,
  recoveryFor,
  retryAfterSeconds,
  revokeMessage,
  spendingView,
  statusBadge,
  unwrapSession,
  unwrapSuccess,
} from '../utils/neverminedSpending'

const props = defineProps({
  agentName: { type: String, required: true },
  // Owner tier (`agent.can_share`): gates Enroll / Revoke. UX only — the backend enforces.
  canEdit: { type: Boolean, default: false },
  // `agent.status`: Enroll needs a running agent (bind writes its .env, FR-3).
  agentStatus: { type: String, default: '' },
  // Positional (message, type) toast host — AgentDetail's showNotification.
  notify: { type: Function, default: null },
  // Seams with production defaults (plan §4.7 / D28): jsdom has no layout and
  // cannot fail a chunk load on demand.
  loadSdk: { type: Function, default: () => import('@nevermined-io/ui-widgets') },
  measureWidth: {
    type: Function,
    default: (el) => el.getBoundingClientRect().width || Infinity,
  },
  now: { type: Function, default: () => Date.now() },
})

const authStore = useAuthStore()
// The `role` getter, not `userRole` (the ent#437 trap).
const isAdmin = computed(() => authStore.role === 'admin')

const data = ref(null)
const hasLoaded = ref(false)
const loading = ref(false)
const error = ref('')
const lastLoadedAt = ref(null)

// Enroll flow (plan §4.3): idle | minting | widget-booting | widget-ready | binding
const phase = ref('idle')
const overlay = ref(false)
const inlineMounted = ref(false)
const slotEl = ref(null)
const enrollError = ref(null)
// A mandate Nevermined created that Trinity has not recorded yet (memory + sessionStorage).
const pending = ref(null)
const envNote = ref(null)
const confirmRevoke = ref(false)
const revoking = ref(false)
const revokeError = ref(null)

const enrolling = computed(() => phase.value !== 'idle')
const slotStyle = computed(() => (inlineMounted.value ? { minHeight: `${INLINE_HEIGHT}px` } : {}))

const view = computed(() => spendingView({
  data: data.value,
  hasLoaded: hasLoaded.value,
  error: error.value,
  canEdit: props.canEdit,
  agentStatus: props.agentStatus,
  hasPending: Boolean(pending.value),
}))
const showPendingBanner = computed(() => view.value.showPendingBanner && phase.value === 'idle' && !enrollError.value)
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
  enrollError.value = null
  envNote.value = null
  revokeError.value = null
  confirmRevoke.value = false
  pending.value = readPending(props.agentName)
}

function toast(message, type = 'success') {
  if (props.notify) props.notify(message, type)
}

// ---- Enroll: mint → initialize → setupCard → onSuccess → bind (plan §4.3) ----

let nvm = null
let handle = null
let bound = false
// A flow token: every await in the flow re-checks it, so a flow torn down
// (agent change, deactivate, unmount, close) never writes into its successor.
let flow = 0

function onKeydown(e) {
  // The overlay may outlive a failed bind (the form stays in its success state
  // until Trinity records the mandate), so Esc keys on the live overlay, not the phase.
  if (e.key === 'Escape' && overlay.value && nvm && phase.value !== 'binding') closeForm()
}
function onBeforeUnload(e) {
  e.preventDefault()
  e.returnValue = ''
}
function onSessionExpired() {
  // A mounted iframe speaks through its own onError; a refreshed token can
  // never reach it. With no iframe there is nothing to keep (plan D32).
  if (['widget-booting', 'widget-ready', 'binding'].includes(phase.value)) return
  teardown()
}

/** Timers, the SDK instance, listeners. Leaves `pending` and errors alone. */
function teardown() {
  flow++
  window.removeEventListener('keydown', onKeydown)
  window.removeEventListener('beforeunload', onBeforeUnload)
  if (nvm) {
    try { nvm.off?.('session-expired', onSessionExpired) } catch { /* SDK gone */ }
    try { nvm.destroy() } catch { /* already destroyed */ }
  }
  nvm = null
  handle = null
  overlay.value = false
  inlineMounted.value = false
}

function closeForm() {
  teardown()
  phase.value = 'idle'
}

// beforeunload only while the form is live or the bind is in flight (D37).
watch(phase, (p) => {
  if (p === 'widget-ready' || p === 'binding') window.addEventListener('beforeunload', onBeforeUnload)
  else window.removeEventListener('beforeunload', onBeforeUnload)
})

function failEnroll(message, { detail = '', retryLabel = ACTIONS.tryAgain, action = 'retry_mint' } = {}) {
  teardown()
  phase.value = 'idle'
  enrollError.value = { message, detail, retryLabel, action }
}

function startEnroll() {
  if (!view.value.canEnroll || enrolling.value) return
  enrollError.value = null
  envNote.value = null
  openForm(props.agentName, 0)
}

async function openForm(name, attempt) {
  teardown()
  const mine = flow
  const live = () => mine === flow && name === props.agentName
  phase.value = 'minting'

  let minted
  try {
    ({ data: minted } = await api.post(`/api/nevermined/agents/${name}/widget-session`))
  } catch (err) {
    if (!live()) return
    const code = apiErrorCode(err)
    if (recoveryFor(code, err?.response?.status, 'mint') === 'reread') {
      phase.value = 'idle'
      load()
      return
    }
    const wait = retryAfterSeconds(err)
    return failEnroll(apiErrorMessage(err, COPY.embedUnknown), {
      detail: wait ? `Nevermined asked Trinity to wait ${wait} s before trying again.` : '',
      retryLabel: code === 'email_required' ? '' : ACTIONS.tryAgain,
    })
  }
  if (!live()) return

  const session = unwrapSession(minted?.session)
  if (!session) return failEnroll(COPY.invalidSession, { detail: COPY.invalidSessionNote })

  let sdk
  try {
    sdk = await props.loadSdk()
  } catch {
    if (!live()) return
    return failEnroll(COPY.sdkLoadFailed)
  }
  if (!live()) return

  phase.value = 'widget-booting'
  let instance
  try {
    instance = await sdk.NeverminedWidgets.initialize({ session, environment: minted.environment })
  } catch (err) {
    if (!live()) return
    const detail = err?.name === 'WidgetInitError' && err.code
      ? `${err.code}. ${COPY.invalidSessionNote}`
      : COPY.invalidSessionNote
    return failEnroll(COPY.invalidSession, { detail })
  }
  if (!live()) { try { instance.destroy() } catch { /* noop */ } return }
  nvm = instance

  // An already-expired session fired its callback synchronously inside
  // initialize, before any listener could attach: re-mint once (D32).
  if (!nvm.hasValidSession) {
    if (attempt === 0) return openForm(name, 1)
    return failEnroll(COPY.sessionExpired, { retryLabel: ACTIONS.tryAgain })
  }
  nvm.on('session-expired', onSessionExpired)

  await nextTick()
  if (!live()) return
  const slot = slotEl.value
  if (!(slot instanceof HTMLElement)) return failEnroll(COPY.embedUnknown)
  const width = props.measureWidth(slot)
  const inline = width >= INLINE_MIN_WIDTH
  overlay.value = !inline
  inlineMounted.value = inline
  if (!inline) window.addEventListener('keydown', onKeydown)
  bound = false

  nvm.delegations.setupCard({
    // undefined ⇒ the SDK's fullscreen overlay (TD-4 / TD-10).
    container: inline ? slot : undefined,
    width: inline ? inlineWidth(width) : undefined,
    height: inline ? INLINE_HEIGHT : undefined,
    provider: 'stripe',
    onBooted: () => {},
    onReady: () => { if (live() && phase.value === 'widget-booting') phase.value = 'widget-ready' },
    onSuccess: ({ result, handle: h } = {}) => onFormSuccess(name, mine, result, h),
    onError: (e) => {
      if (!live() || phase.value === 'binding') return
      enrollError.value = { message: embedErrorMessage(e), detail: e?.apiCode || '', retryLabel: ACTIONS.tryAgain, action: 'retry_mint' }
    },
    onClose: () => {
      // handle.close() fires onClose synchronously; a close during the bind
      // must not wipe the flow (D29).
      if (phase.value === 'binding' || bound) return
      if (mine !== flow) return
      closeForm()
    },
  })
}

async function onFormSuccess(name, mine, result, h) {
  const ids = unwrapSuccess(result)
  if (!ids) {
    if (mine === flow && name === props.agentName) failEnroll(COPY.badIds, { retryLabel: COPY.newMandate })
    return
  }
  // Persist BEFORE the bind: a lost page still leaves a recoverable record.
  const record = { agent: name, ...ids, provider: 'stripe', at: props.now() }
  persistPending(record)
  if (mine !== flow || name !== props.agentName) {
    // D27: the panel moved to another agent; the record waits on this one's panel.
    teardown()
    return
  }
  pending.value = record
  handle = h || null
  enrollError.value = null
  await bind(name, record)
}

async function bind(name, record) {
  phase.value = 'binding'
  enrollError.value = null
  const body = { delegationId: record.delegationId, provider: record.provider || 'stripe' }
  if (record.paymentMethodId) body.paymentMethodId = record.paymentMethodId
  try {
    const { data: res } = await api.post(`/api/nevermined/agents/${name}/delegation`, body)
    if (name !== props.agentName) return
    // FUNDED renders from the 201 itself — no GET re-read (D33).
    generation++
    data.value = res
    hasLoaded.value = true
    lastLoadedAt.value = Date.now()
    error.value = ''
    clearPending(name)
    pending.value = null
    bound = true
    const h = handle
    if (h) { try { h.close() } catch { /* already closed */ } }
    teardown()
    phase.value = 'idle'
    toast(COPY.funded)
  } catch (err) {
    if (name !== props.agentName) return
    phase.value = 'idle'
    const code = apiErrorCode(err)
    const action = recoveryFor(code, err?.response?.status, 'bind')
    const message = apiErrorMessage(err, COPY.embedUnknown)
    if (action === 'reread') {
      if (code === 'delegation_active') toast(COPY.alreadyFunded, 'info')
      load()
      return
    }
    if (action === 'new_mandate') {
      clearPending(name)
      pending.value = null
      teardown()
      enrollError.value = { message, detail: '', retryLabel: COPY.newMandate, action }
      return
    }
    const wait = retryAfterSeconds(err)
    const detail = [COPY.mandateExists, wait ? `Nevermined asked Trinity to wait ${wait} s.` : '']
      .filter(Boolean).join(' ')
    enrollError.value = {
      message,
      detail,
      retryLabel: action === 'retry_bind' ? ACTIONS.retryBind : '',
      action,
    }
  }
}

/** Bind the SAME ids again — the Nevermined form is not mounted a second time. */
function retryBind() {
  const record = pending.value || readPending(props.agentName)
  if (!record || phase.value === 'binding') return
  pending.value = record
  bind(props.agentName, record)
}

function discardPending() {
  clearPending(props.agentName)
  pending.value = null
}

function onEnrollRetry() {
  const action = enrollError.value?.action
  if (action === 'retry_bind') return retryBind()
  enrollError.value = null
  if (action === 'new_mandate' || action === 'retry_mint') openForm(props.agentName, 0)
}

// ---- Revoke (plan §4.4) ----

function startRevoke() {
  revokeError.value = null
  confirmRevoke.value = true
}

async function doRevoke() {
  const name = props.agentName
  revoking.value = true
  revokeError.value = null
  try {
    const { data: res } = await api.delete(`/api/nevermined/agents/${name}/delegation`)
    if (name !== props.agentName) return
    toast(COPY.revoked)
    // The backend's own sentence, verbatim; the prefix picks only the tone (addendum W3).
    envNote.value = res?.env_removed === false && res?.env_note
      ? { text: res.env_note, tone: envNoteTone(res.env_note) }
      : null
    await load()
  } catch (err) {
    if (name !== props.agentName) return
    if (apiErrorCode(err) === 'no_active_delegation') {
      await load()
      return
    }
    revokeError.value = {
      message: apiErrorMessage(err, COPY.revokeFailed),
      detail: COPY.revokeFailed,
      retryable: recoveryFor(apiErrorCode(err), err?.response?.status, 'revoke') === 'retry_revoke',
    }
  } finally {
    if (name === props.agentName) revoking.value = false
  }
}

// The panel lives inside the KeepAlive-cached AgentDetail: onMounted and
// onActivated both fire on first entry, so the first activate is skipped.
let skipActivate = false
onMounted(() => { skipActivate = true; pending.value = readPending(props.agentName); load() })
onActivated(() => {
  if (skipActivate) { skipActivate = false; return }
  if (!pending.value) pending.value = readPending(props.agentName)
  load() // background re-read: data stays, no skeleton
})
// Leaving the tab ends the form, but lets an in-flight bind land (§4.6).
onDeactivated(() => { if (phase.value !== 'binding') closeForm() })
onUnmounted(() => { generation++; teardown() })

watch(() => props.agentName, () => { teardown(); phase.value = 'idle'; reset(); load() })
</script>

<style scoped>
/* The SDK styles its inline iframe with a light-only border; match the card's
   dark border (no raw hex here — a theme() token). */
:global(.dark) .nvm-widget-slot :deep(iframe) {
  border-color: theme('colors.gray.750') !important;
}
</style>
