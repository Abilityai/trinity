<!--
  Workspace loops — the rail's Loops tab body (ent#458, re-homed by
  trinity-enterprise#475 into the ent#474 rail).

  What a person can do here is what the strip above the composer let them do:
  see what is running with how much guardrail is left, Stop it (always
  available), and Start one with the guardrails visible BEFORE Start. A room
  groups by participating agent, and an agent with nothing running still gets
  its row (absence is visible — the rail's own rule).

  What moved out: the collapsed-strip chrome (the rail owns collapse now) and
  the ownership of `stores/portalLoops.js`. The shell feeds the store through
  `composables/usePortalRailFeeds.js` — ONE owner, keyed on the door and the
  participant set — so this body only READS it, and the collapsed rail can
  signal "1 running" with this body unmounted. That is also why the ent#458
  owned-key / incoming-vs-outgoing races are gone rather than guarded: there
  is one mount point, and it is not this file.

  Platform-authenticated door ONLY (ent#78's auth-path invariant, restated by
  ent#458): the rail never renders this tab for an external client, and the
  `visible` gate below is belt-and-braces under that — hidden, not disabled,
  since a disabled control advertises a capability a portal token can never
  satisfy. All decidable rules live in `portalLoopUtils.js` / `portalRail.js`;
  this file is a dispatcher over them (vitest runs node-env, no mount harness).
-->
<template>
  <div v-if="visible" class="space-y-3" data-testid="portal-loops">
    <!-- No verdict yet: a skeleton, never a spinner (#2540 / AC 6). -->
    <PortalSkeleton v-if="view.state === 'loading'" variant="rail" />

    <LoadFailed
      v-else-if="view.state === 'failed'"
      title="Couldn't load loops"
      :message="store.error || 'The loops for this chat could not be read.'"
      :retrying="store.loading"
      @retry="store.fetchLoops()"
    />

    <template v-else>
      <!-- A failed REFRESH keeps the rows and says so (#1926). -->
      <InlineError v-if="view.stale" :message="store.error" @dismiss="store.error = null" />

      <!-- Empty state that teaches (AC #5) -->
      <div
        v-if="view.state === 'empty' && !showForm"
        class="py-6 text-center"
        data-testid="portal-loops-empty"
      >
        <p class="text-sm font-semibold">{{ emptyCopy.title }}</p>
        <p class="mt-1.5 text-xs text-gray-500 dark:text-gray-400 max-w-[36ch] mx-auto">{{ emptyCopy.body }}</p>
        <BaseButton size="sm" variant="primary" class="mt-4" @click="showForm = true">
          Start a loop
        </BaseButton>
      </div>

      <!-- Active + recent loops, one group per participant -->
      <div v-for="[agent, list] in grouped" :key="agent" class="space-y-2">
        <div v-if="participants.length > 1" class="flex items-center gap-2 min-w-0">
          <PortalAvatar :name="agent" :size="18" />
          <span class="text-[11px] font-semibold uppercase tracking-wide text-gray-400 truncate">{{ agent }}</span>
          <BaseBadge v-if="rowState(agent).live" variant="info" dot class="ml-auto">{{ rowState(agent).label }}</BaseBadge>
          <span v-else class="ml-auto text-xs text-gray-400">{{ rowState(agent).label }}</span>
        </div>
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
        v-else-if="view.state === 'ready'"
        size="sm"
        variant="secondary"
        @click="showForm = true"
      >
        Start another loop
      </BaseButton>
    </template>
  </div>
</template>

<script setup>
import { ref, reactive, computed, watch } from 'vue'
import { usePortalLoopsStore } from '@/stores/portalLoops'
import { useClientPortalStore } from '@/stores/clientPortal'
import BaseButton from '@/components/base/BaseButton.vue'
import BaseBadge from '@/components/base/BaseBadge.vue'
import LoadFailed from '@/components/LoadFailed.vue'
import InlineError from '@/components/InlineError.vue'
import PortalAvatar from './PortalAvatar.vue'
import PortalSkeleton from './PortalSkeleton.vue'
import {
  FORM_INITIAL, GUARDRAIL_DEFAULTS, loopHeadroom, loopStatusLabel,
  loopStatusTone, startErrorMessage, validateStartForm,
} from './portalLoopUtils'
import { feedView, groupByParticipant, loopsSignalFrom, participantState, railEmptyCopy } from './portalRail'

const props = defineProps({
  // The chat's participants — the rail passes its own (`railParticipants`).
  participants: { type: Array, default: () => [] },
  // The registry entry, for the empty copy. Optional so the body also renders
  // outside the rail's slot (tests, a future host).
  tab: { type: Object, default: null },
})

const store = usePortalLoopsStore()
const portal = useClientPortalStore()

const showForm = ref(false)
const startError = ref(null)
const errors = ref({})
const form = reactive({ ...FORM_INITIAL, agent: null })

// Platform door only — belt-and-braces under the rail's `visibleTabs` gate.
const visible = computed(() => portal.isPlatformSession && props.participants.length > 0)

const participants = computed(() => props.participants)

// The verdict, never the in-flight flag (#1927): loading = no data yet.
const view = computed(() => feedView({
  participants: participants.value,
  hasLoaded: store.hasLoaded,
  error: store.error,
  count: store.loops.length,
}))

// One grouping rule for every tab (ent#474): a row per participant in
// participant order, absence visible. Loops within a group are active-first.
const grouped = computed(() => groupByParticipant(store.loops, participants.value))
const liveSignal = computed(() => loopsSignalFrom(store.active))
function rowState(agent) { return participantState(agent, liveSignal.value) }

// The registry's copy when docked in the rail; this fallback only when hosted
// elsewhere without a `tab`.
const EMPTY_FALLBACK = {
  title: 'No loops running',
  body: 'A loop runs the same instruction several times in a row — working through a list, retrying until something succeeds, refining a draft. It stops at its run limit, and you can stop it any time.',
  action: 'Start a loop',
}
const emptyCopy = computed(() => railEmptyCopy(props.tab || { empty: EMPTY_FALLBACK }, participants.value))

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

// The form's agent follows the participant SET, and only the set: keyed on the
// joined names, not the array identity both parents rebuild every render, so
// a poll tick cannot silently discard the agent the user picked (the ent#458
// review finding, kept). Fetching is not this file's job any more — see the
// header — so this is the only watch left.
const participantsKey = computed(() => participants.value.join(' '))
watch(participantsKey, () => {
  const names = participants.value
  if (!names.includes(form.agent)) form.agent = names[0] || null
}, { immediate: true })
</script>
