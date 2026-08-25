<!--
  Workspace loop panel (ent#458).

  A collapsed strip above the composer that stays quiet until an agent in this
  chat is actually looping, then shows what is running, how much guardrail is
  left, and a Stop that is always available. Start lives behind the same strip
  so a loop is begun from the conversation it belongs to.

  Platform-authenticated door ONLY (ent#78's auth-path invariant, restated by
  ent#458): this renders for a signed-in platform session and never for an
  external client holding a portal token. That is also why it needs no new
  backend surface — it calls the existing operator loop endpoints with the
  operator's own JWT.

  All decidable rules live in `portalLoopUtils.js`; this file is a dispatcher
  over them (vitest runs node-env here, with no component-mount harness).
-->
<template>
  <div v-if="visible" class="border-t border-gray-200 dark:border-gray-700" data-testid="portal-loops">
    <!-- Collapsed strip -->
    <button
      type="button"
      class="w-full flex items-center gap-2 px-4 py-2 text-xs text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800"
      :aria-expanded="expanded"
      data-testid="portal-loops-toggle"
      @click="expanded = !expanded"
    >
      <span
        v-if="store.hasActive"
        class="inline-block w-1.5 h-1.5 rounded-full bg-action-primary-500 animate-pulse"
        aria-hidden="true"
      ></span>
      <span class="font-medium">{{ summary || 'Loops' }}</span>
      <span class="ml-auto text-gray-400" aria-hidden="true">{{ expanded ? '▾' : '▸' }}</span>
    </button>

    <div v-if="expanded" class="px-4 pb-3 space-y-3">
      <p v-if="store.error" class="text-xs text-status-warning-600 dark:text-status-warning-400" role="status">
        {{ store.error }}
      </p>

      <!-- Empty state that teaches (AC #5) -->
      <div
        v-if="store.hasLoaded && !store.loops.length && !showForm"
        class="rounded-lg border border-dashed border-gray-300 dark:border-gray-600 p-3"
        data-testid="portal-loops-empty"
      >
        <p class="text-xs text-gray-600 dark:text-gray-300">
          A loop runs the same instruction several times in a row — useful for working
          through a list, retrying until something succeeds, or refining a draft. It stops
          on its own at the run limit, and you can stop it at any time.
        </p>
        <BaseButton size="sm" variant="primary" class="mt-2" @click="showForm = true">
          Start a loop
        </BaseButton>
      </div>

      <!-- Active + recent loops -->
      <div v-for="[agent, list] in grouped" :key="agent" class="space-y-2">
        <p v-if="participants.length > 1" class="text-[11px] uppercase tracking-wide text-gray-400">
          {{ agent }}
        </p>
        <div
          v-for="loop in list"
          :key="loop.loop_id"
          class="rounded-lg border border-gray-200 dark:border-gray-700 p-2.5"
          data-testid="portal-loop-row"
        >
          <div class="flex items-start gap-2">
            <div class="min-w-0 flex-1">
              <p class="text-xs font-medium" :class="toneClass(loop)">{{ statusLabel(loop) }}</p>
              <p class="text-[11px] text-gray-500 dark:text-gray-400">
                Run {{ loop.runs_completed }} of {{ loop.max_runs }}
                <span v-if="loop.failed_runs"> · {{ loop.failed_runs }} failed</span>
              </p>
            </div>
            <BaseButton
              v-if="store.isActive(loop)"
              size="sm"
              variant="secondary"
              :disabled="store.stoppingIds.includes(loop.loop_id)"
              :data-testid="`portal-loop-stop-${loop.loop_id}`"
              @click="onStop(loop)"
            >
              {{ store.stoppingIds.includes(loop.loop_id) ? 'Stopping…' : 'Stop' }}
            </BaseButton>
          </div>

          <!-- Guardrail headroom -->
          <dl class="mt-2 grid grid-cols-3 gap-2 text-[11px]">
            <div v-for="bar in bars(loop)" :key="bar.key">
              <dt class="text-gray-500 dark:text-gray-400">{{ bar.label }}</dt>
              <dd class="mt-0.5">
                <div class="h-1 rounded bg-gray-200 dark:bg-gray-700 overflow-hidden">
                  <div class="h-full bg-action-primary-500" :style="{ width: `${Math.round(bar.fraction * 100)}%` }"></div>
                </div>
                <span class="text-gray-600 dark:text-gray-300">{{ bar.text }}</span>
              </dd>
            </div>
          </dl>
        </div>
      </div>

      <!-- Start form: guardrails visible BEFORE start (AC #1) -->
      <div v-if="showForm" class="rounded-lg border border-gray-200 dark:border-gray-700 p-3 space-y-2" data-testid="portal-loop-form">
        <label class="block text-[11px] text-gray-500 dark:text-gray-400">
          Agent
          <select v-model="form.agent" class="mt-0.5 w-full text-xs rounded border-gray-300 dark:border-gray-600 dark:bg-gray-800">
            <option v-for="p in participants" :key="p" :value="p">{{ p }}</option>
          </select>
        </label>
        <label class="block text-[11px] text-gray-500 dark:text-gray-400">
          Do this each run
          <textarea
            v-model="form.message"
            rows="2"
            data-testid="portal-loop-message"
            class="mt-0.5 w-full text-xs rounded border-gray-300 dark:border-gray-600 dark:bg-gray-800"
            placeholder="Take the next item and…"
          ></textarea>
        </label>
        <p v-if="errors.message" class="text-[11px] text-status-danger-600 dark:text-status-danger-400">{{ errors.message }}</p>

        <div class="grid grid-cols-2 gap-2">
          <label class="block text-[11px] text-gray-500 dark:text-gray-400">
            Runs
            <input v-model.number="form.max_runs" type="number" min="1" max="100"
                   class="mt-0.5 w-full text-xs rounded border-gray-300 dark:border-gray-600 dark:bg-gray-800" />
          </label>
          <label class="block text-[11px] text-gray-500 dark:text-gray-400">
            Cost budget (USD)
            <input v-model="form.max_cost_usd" type="number" step="0.01" min="0" placeholder="none"
                   class="mt-0.5 w-full text-xs rounded border-gray-300 dark:border-gray-600 dark:bg-gray-800" />
          </label>
        </div>
        <p v-if="errors.max_runs" class="text-[11px] text-status-danger-600 dark:text-status-danger-400">{{ errors.max_runs }}</p>
        <p v-if="errors.max_cost_usd" class="text-[11px] text-status-danger-600 dark:text-status-danger-400">{{ errors.max_cost_usd }}</p>

        <p class="text-[11px] text-gray-500 dark:text-gray-400">
          It also stops by itself after
          <strong>{{ GUARDRAIL_DEFAULTS.no_progress_threshold }}</strong>
          identical replies in a row, and after
          <strong>{{ GUARDRAIL_DEFAULTS.max_consecutive_failures }}</strong>
          consecutive failures. No time limit unless you set one.
        </p>

        <p v-if="startError" class="text-[11px] text-status-danger-600 dark:text-status-danger-400" role="alert">{{ startError }}</p>
        <div class="flex gap-2">
          <BaseButton size="sm" variant="primary" :disabled="store.starting" @click="onStart">
            {{ store.starting ? 'Starting…' : 'Start' }}
          </BaseButton>
          <BaseButton size="sm" variant="secondary" @click="showForm = false">Cancel</BaseButton>
        </div>
      </div>

      <BaseButton
        v-else-if="store.loops.length"
        size="sm"
        variant="secondary"
        @click="showForm = true"
      >
        Start another loop
      </BaseButton>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, watch, onUnmounted } from 'vue'
import { usePortalLoopsStore } from '@/stores/portalLoops'
import { useClientPortalStore } from '@/stores/clientPortal'
import BaseButton from '@/components/base/BaseButton.vue'
import {
  FORM_INITIAL, GUARDRAIL_DEFAULTS, byAgent, loopHeadroom, loopStatusLabel,
  loopStatusTone, startErrorMessage, stripSummary, validateStartForm,
} from './portalLoopUtils'

const props = defineProps({
  participants: { type: Array, default: () => [] },
})

const store = usePortalLoopsStore()
const portal = useClientPortalStore()

const expanded = ref(false)
const showForm = ref(false)
const startError = ref(null)
const errors = ref({})
// What this instance last handed the shared store — see onUnmounted.
const ownedKey = ref(null)
const form = reactive({ ...FORM_INITIAL, agent: null })

// Platform door only. An external client never sees the strip at all — not a
// disabled control, which would advertise a capability their credential can
// never satisfy.
const visible = computed(() => portal.isPlatformSession && props.participants.length > 0)

const participants = computed(() => props.participants)
const summary = computed(() => stripSummary(store.loops))
const grouped = computed(() => Array.from(byAgent(store.loops, participants.value))
  .filter(([, list]) => list.length))

function statusLabel(loop) { return loopStatusLabel(loop) }

function toneClass(loop) {
  switch (loopStatusTone(loop)) {
    case 'active': return 'text-action-primary-600 dark:text-action-primary-400'
    case 'ok': return 'text-status-success-600 dark:text-status-success-400'
    case 'warn': return 'text-status-warning-600 dark:text-status-warning-400'
    case 'danger': return 'text-status-danger-600 dark:text-status-danger-400'
    default: return 'text-gray-600 dark:text-gray-300'
  }
}

function bars(loop) {
  const h = loopHeadroom(loop)
  const out = []
  if (h.runs) out.push({ key: 'runs', label: 'Runs', fraction: h.runs.fraction, text: `${h.runs.used}/${h.runs.limit}` })
  if (h.cost) out.push({ key: 'cost', label: 'Budget', fraction: h.cost.fraction, text: `$${h.cost.used.toFixed(2)} / $${h.cost.limit}` })
  if (h.time) out.push({ key: 'time', label: 'Time', fraction: h.time.fraction, text: `${h.time.used}s / ${h.time.limit}s` })
  return out
}

async function onStart() {
  startError.value = null
  const check = validateStartForm(form)
  errors.value = check.errors
  if (!check.valid) return
  const agent = form.agent || participants.value[0]
  const res = await store.startLoop(agent, form)
  if (res.success) {
    showForm.value = false
    form.message = ''
  } else {
    startError.value = startErrorMessage(res.error)
  }
}

async function onStop(loop) {
  const res = await store.stopLoop(loop.loop_id)
  if (!res.success) store.error = 'Could not stop that loop.'
}

// Watch `visible` alongside the participants, not the participants alone.
// `isPlatformSession` derives from `authStore.isAuthenticated` and the portal
// token, both of which settle asynchronously — so at mount `visible` is
// routinely still false. Guarding on it inside a participants-only watch meant
// a late auth confirmation left the strip rendered and permanently empty: the
// guard returned, and nothing re-ran it because the participants never changed.
// Same class as `AdminEmailNudge`'s `profileVerified` and the ent#384 tile's
// key-appearance watch — a derived auth flag needs a watcher, not a read.
// Review finding: the source was `participants`, a computed over `props.participants`
// — and both parents build that array fresh on every render (`PortalConversation`
// passes the literal `[agent.name]`; `PortalRoom` passes a computed re-derived
// from `room.value`, which its 3s poll reassigns). A non-deep watch compares
// with `Object.is`, so a new array identity always fired: typing a 40-character
// message issued 40 `GET /loops` per participant, and a room polled
// unconditionally every 3s with nothing running. `store.setParticipants`
// early-returns on unchanged content, but `fetchLoops()` did not.
//
// Watching the JOINED KEY makes the watch fire on what actually changed. The
// same re-fire also reset `form.agent` to the first participant mid-selection,
// so a Start clicked in that window ran the loop on the wrong agent.
const participantsKey = computed(() => participants.value.join('\u0000'))

watch([participantsKey, visible], ([key, isVisible], previous) => {
  if (!isVisible) return
  const names = participants.value
  const keyChanged = !previous || previous[0] !== key
  store.setParticipants(names)
  ownedKey.value = key
  // Only when the SET changed — otherwise a poll tick silently discards the
  // agent the user picked in the start form.
  if (keyChanged) form.agent = names[0] || null
  store.fetchLoops()
}, { immediate: true })

// Review finding: this used to read `store.hasActive` in `onMounted`, where it
// is always false — the immediate watcher above only STARTS the async
// `fetchLoops()`, and the matching-key branch of `onUnmounted` calls
// `store.clear()`, so nothing can be left over from a previous mount either. So
// the strip rendered collapsed saying "N loops running" and never expanded, and
// the documented "not asked to go looking for work that is in flight" never
// happened. One-shot: the first time the store reports something active.
const autoExpanded = ref(false)
watch(() => store.hasActive, (active) => {
  if (!active || autoExpanded.value) return
  autoExpanded.value = true
  expanded.value = true
}, { immediate: true })

onUnmounted(() => {
  // Clear only if the store still holds THIS instance's participants.
  // PortalConversation and PortalRoom share one store singleton, and switching
  // between a room and a 1:1 can mount the new panel before the old one
  // unmounts — an unconditional clear would then wipe the list the new panel
  // just set and leave it empty with no watcher left to re-run.
  if (ownedKey.value && store.participants.join('\u0000') === ownedKey.value) {
    store.clear()
  }
  // Review finding: the else-branch used to call `store.stopPolling()`, and
  // that branch is reached exactly when the store belongs to the INCOMING
  // panel. `_ensurePolling()` only runs from `fetchLoops`'s finally, so if the
  // new panel's first fetch had already armed the 12s timer, the outgoing panel
  // cleared it and nothing re-armed — leaving the new panel with WS as its only
  // update path, which is precisely the failure the backstop exists to cover.
  // The store is not ours any more: do nothing.
})
</script>
