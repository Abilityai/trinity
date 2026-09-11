<!--
  First-run overlay — the chassis (ent#581, epic ent#54).

  ONE blocking overlay owns the whole initial setup: it says up front what will
  be configured, walks a fixed order over the steps THIS install has, and never
  moves the page beneath it. It absorbs the hardening guide (#2380), the
  finish-setup card (#2381 / ent#437), the front desk (ent#319) and the ent#52
  wizard; `ActivationChecklist` (ent#238) deliberately stays inline.

  Every decision — which steps, when it opens, what counts as done, whether
  Continue is live — is in `firstRunSteps.js`, where the node-only spec can
  reach it. This file is a dispatcher: it builds `ctx` from the stores it
  already reads, snapshots the step set when it opens, and routes events.

  Step contract (shared with ent#582): each step gets `ctx` (read-only) and
  emits `complete` when its own work is done; the chassis then refreshes every
  store `ctx` derives from, and a required step's Continue goes live once the
  step emitted `complete` OR its predicate went false after that refresh.
-->
<template>
  <!-- Teleport to <body>: the Dashboard's nested layout creates stacking
       contexts that z-index alone cannot beat — the ent#52 wizard rationale,
       which is still true. -->
  <Teleport to="body">
    <div
      v-if="open"
      ref="rootEl"
      class="fixed inset-0 z-50 flex items-center justify-center p-5 max-sm:p-2
             bg-gray-950/55 backdrop-blur-[3px]"
      role="dialog"
      aria-modal="true"
      aria-labelledby="first-run-title"
      data-testid="first-run-overlay"
    >
      <!-- Ambient depth carried over from the /setup aurora, dialled far down.
           Both stops are token-derived, so this themes and cannot drift. -->
      <div class="pointer-events-none absolute inset-0 first-run-aurora" aria-hidden="true"></div>

      <!-- Fixed height, not min-height: the PANEL never resizes between steps
           (contract p.4/6) — a long step scrolls its own body instead. -->
      <div
        class="relative grid h-[600px] max-h-full w-full max-w-[940px]
               grid-cols-1 grid-rows-[auto_minmax(0,1fr)] sm:grid-cols-[264px_minmax(0,1fr)] sm:grid-rows-1
               overflow-hidden rounded-[10px] border border-gray-200 dark:border-gray-750
               bg-white dark:bg-gray-800 shadow-2xl"
      >
        <FirstRunRail
          :steps="steps"
          :states="states"
          :reachable="reachable"
          @go="goTo"
          @close="requestClose"
        />

        <div class="flex min-h-0 min-w-0 flex-col">
          <div ref="bodyEl" class="min-h-0 flex-1 overflow-auto px-8 py-7 max-sm:px-4 max-sm:py-5">
            <StepWelcome
              v-if="currentKey === 'welcome'"
              :steps="steps"
              :states="states"
              :lit="lit"
            />
            <StepDone
              v-else-if="currentKey === 'done'"
              :steps="steps"
              :states="states"
              :lit="lit"
              :next-label="nextLabel"
            />
            <component
              :is="STEP_COMPONENTS[currentKey]"
              v-else
              :key="currentKey"
              :ctx="ctx"
              @complete="onStepComplete"
              @skip="skipCurrent"
            />
          </div>

          <footer
            class="flex flex-wrap items-center gap-2 border-t border-gray-200 dark:border-gray-750
                   px-8 py-3.5 max-sm:px-4 max-sm:py-3"
          >
            <BaseButton v-if="!isFirst" variant="ghost" size="sm" data-testid="first-run-back" @click="back">
              &larr; Back
            </BaseButton>
            <!-- Phone: the rail's "Finish later" is hidden, so it lives here. -->
            <BaseButton
              variant="ghost"
              size="sm"
              class="sm:hidden"
              data-testid="first-run-finish-later-footer"
              @click="requestClose"
            >
              Finish later
            </BaseButton>
            <span class="flex-1"></span>

            <!-- Skip names its destination, always. Never a grey "maybe later".
                 Absent on a step that is already done — there is nothing to skip. -->
            <BaseButton
              v-if="currentStep && !currentStep.required && !currentDone"
              variant="ghost"
              size="sm"
              :data-testid="`first-run-skip-${currentKey}`"
              @click="skipCurrent"
            >
              Skip &mdash; later in {{ currentStep.settingsPath }}
            </BaseButton>

            <BaseButton
              ref="continueBtn"
              variant="primary"
              size="sm"
              :disabled="!continueEnabled"
              :data-testid="`first-run-next-${currentKey}`"
              @click="next"
            >
              {{ continueLabel }}
            </BaseButton>
          </footer>
        </div>
      </div>

      <!-- Deliberate dismissal (AC 9): no X, no backdrop close. "Finish later"
           and Esc both land here, and it states the consequence. -->
      <ConfirmDialog
        v-model:visible="confirming"
        variant="warning"
        title="Finish setup later?"
        :message="closeConsequence"
        confirm-text="Finish later"
        cancel-text="Keep setting up"
        @confirm="finishLater"
        @cancel="focusSafeAction"
      />
    </div>
  </Teleport>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '../../stores/auth'
import { useSessionsStore } from '../../stores/sessions'
import { useFirstRunStore } from '../../stores/firstRun'
import { useProductTelemetryStore } from '../../stores/productTelemetry'
import BaseButton from '../base/BaseButton.vue'
import ConfirmDialog from '../ConfirmDialog.vue'
import FirstRunRail from './FirstRunRail.vue'
import StepWelcome from './steps/StepWelcome.vue'
import StepDone from './steps/StepDone.vue'
import StepSecure from './steps/StepSecure.vue'
import StepEmail from './steps/StepEmail.vue'
import StepClaude from './steps/StepClaude.vue'
import StepKeys from './steps/StepKeys.vue'
import StepAgent from './steps/StepAgent.vue'
import StepSharing from './steps/StepSharing.vue'
import {
  FIRST_RUN_STEPS,
  canContinue,
  countsAsSetupStart,
  isFirstRunOverlayVisible,
  litNodes,
  persistFirstRunClosed,
  persistFirstRunSkipped,
  readFirstRunClosed,
  readFirstRunSkipped,
  stepState,
  stepsForSession,
} from './firstRunSteps'

const STEP_COMPONENTS = {
  secure: StepSecure,
  email: StepEmail,
  claude: StepClaude,
  keys: StepKeys,
  agent: StepAgent,
  sharing: StepSharing,
}

// The parent binds this so its own hotkeys stand down while setup is open.
const open = defineModel('open', { type: Boolean, default: false })

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const sessions = useSessionsStore()
const firstRun = useFirstRunStore()
const telemetry = useProductTelemetryStore()

// Per-browser, read once so a change in another tab cannot pop it mid-render.
const closed = ref(readFirstRunClosed())
const skipped = ref(readFirstRunSkipped())
// Session-only: steps that emitted `complete` since the overlay opened. Never
// persisted — completion is otherwise derived from state.
const completed = ref([])

const forced = computed(() => route.query.onboarding === '1')

/**
 * The registry context. Step components receive it read-only; C's credential
 * steps (ent#582) read `claudeAuthConfigured` and `isAdmin` off it.
 */
const ctx = computed(() => ({
  // "Loaded" means the read SUCCEEDED — a failed flags fetch reports Claude as
  // unconfigured, and the one blocking step must not trap a working fleet.
  flagsLoaded: sessions.featureFlagsLoaded && !sessions.featureFlagsFailed,
  profileVerified: auth.profileVerified,
  firstRunLoaded: firstRun.loaded,
  // `role` is the getter that exists; #2380 and #2381 read one that did not,
  // and shipped permanently hidden (authRoleGetterContract.spec.js).
  isAdmin: auth.profileVerified && auth.role === 'admin',
  marketplaceInstall: sessions.hardeningGuideEligible,
  tlsPosture: sessions.installTlsPosture,
  hasEmail: !!auth.userEmail,
  userEmail: auth.userEmail,
  claudeAuthConfigured: sessions.claudeAuthConfigured,
  telemetryEnabled: sessions.telemetrySharingEnabled,
  telemetryHardDisabled: sessions.telemetrySharingHardDisabled,
  telemetryDismissed: sessions.telemetrySharingDismissed,
  telemetryFirstValue: sessions.telemetrySharingFirstValue,
  firstRun: firstRun.firstRun,
  demoAgent: firstRun.demoAgent,
  forced: forced.value,
  closed: closed.value,
  skipped: skipped.value,
}))

// Snapshotted at open (see `stepsForSession`): membership is fixed for the
// session, so a step done mid-way keeps its row and shows a check.
const stepKeys = ref([])
const steps = computed(() => FIRST_RUN_STEPS.filter((s) => stepKeys.value.includes(s.key)))
const order = computed(() => ['welcome', ...stepKeys.value, 'done'])
const currentKey = ref('welcome')
const furthest = ref(0)

const currentStep = computed(() => FIRST_RUN_STEPS.find((s) => s.key === currentKey.value) || null)
const currentDone = computed(
  () => !!currentStep.value &&
    (!currentStep.value.applies(ctx.value) || completed.value.includes(currentKey.value))
)
const isFirst = computed(() => currentKey.value === 'welcome')

const states = computed(() =>
  Object.fromEntries(
    steps.value.map((step) => [
      step.key,
      stepState({
        step,
        currentKey: currentKey.value,
        ctx: ctx.value,
        skipped: skipped.value,
        completed: completed.value,
      }),
    ])
  )
)
const lit = computed(() => litNodes(states.value))
const reachable = computed(() => order.value.filter((_, i) => i <= furthest.value))

const continueEnabled = computed(
  () => !currentStep.value || canContinue({ step: currentStep.value, ctx: ctx.value, completed: completed.value })
)
const continueLabel = computed(() =>
  currentKey.value === 'welcome' ? 'Get started' : currentKey.value === 'done' ? 'Done' : 'Continue'
)

// Where "Done" goes, when a step chose a destination (the agent step's doors).
const nextRoute = ref(null)
const nextLabel = ref('')

const closeConsequence = computed(() => {
  const claude = steps.value.find((s) => s.key === 'claude')
  const idle = claude && states.value.claude !== 'done'
    ? 'Your agents stay idle until you connect Claude in Settings → Integrations. '
    : ''
  return `${idle}Anything you have not done stays where each step said — and Settings → General → Re-run setup brings this back.`
})

// ---- open / close -----------------------------------------------------------

function start() {
  stepKeys.value = stepsForSession(ctx.value).map((s) => s.key)
  currentKey.value = 'welcome'
  furthest.value = 0
  open.value = true
  if (countsAsSetupStart(ctx.value)) telemetry.record('setup_started')
}

// The predicate only OPENS the overlay. Once open it stays open until the
// operator leaves, or completing the last step would yank it mid-sentence.
watch(
  () => isFirstRunOverlayVisible(ctx.value),
  (visible) => {
    if (visible && !open.value) start()
  },
  { immediate: true }
)

function close() {
  persistFirstRunClosed()
  closed.value = true
  open.value = false
  if (nextRoute.value) {
    router.push(nextRoute.value).catch(() => {})
  } else if (forced.value) {
    // Consumed: a reload must not reopen it.
    const { onboarding: _consumed, ...rest } = route.query
    router.replace({ query: rest }).catch(() => {})
  }
  nextRoute.value = null
  nextLabel.value = ''
}

function requestClose() {
  confirming.value = true
  // Initial focus on the SAFE action (contract: modal rule).
  nextTick(() => document.querySelector('[data-testid="confirm-dialog-cancel"]')?.focus())
}

function finishLater() {
  telemetry.record('setup_dismissed', { via: 'finish_later' })
  close()
}

// ---- navigation ------------------------------------------------------------

function goTo(key) {
  if (reachable.value.includes(key)) currentKey.value = key
}

function advance() {
  const i = order.value.indexOf(currentKey.value)
  currentKey.value = order.value[Math.min(i + 1, order.value.length - 1)]
  furthest.value = Math.max(furthest.value, i + 1)
  if (currentKey.value === 'claude') telemetry.record('setup_step_credential')
}

function back() {
  const i = order.value.indexOf(currentKey.value)
  if (i > 0) currentKey.value = order.value[i - 1]
  // Back does not exist on the first panel, so focus must not stay on it.
  if (isFirst.value) focusSafeAction()
}

function markSkipped(key) {
  if (!skipped.value.includes(key)) skipped.value = [...skipped.value, key]
  persistFirstRunSkipped(skipped.value)
}

function skipCurrent() {
  if (!currentStep.value || currentStep.value.required) return
  markSkipped(currentKey.value)
  advance()
}

function next() {
  if (currentKey.value === 'done') {
    telemetry.record('setup_completed', { via: nextRoute.value ? 'next_step' : 'done' })
    close()
    return
  }
  // Moving past an optional step that is still pending IS skipping it — the
  // rail says so rather than leaving a hollow circle behind the operator. (A
  // required step cannot get here pending: Continue is disabled there.)
  if (currentStep.value && !currentDone.value) markSkipped(currentKey.value)
  advance()
}

// ---- step events -----------------------------------------------------------

async function refreshCtx() {
  // Everything `ctx` derives from, forced past each store's once-cache.
  await Promise.allSettled([
    sessions.loadFeatureFlags(true),
    auth.fetchUserProfile(),
    firstRun.fetchState(true),
  ])
}

function onStepComplete(payload) {
  const key = currentKey.value
  if (!completed.value.includes(key)) completed.value = [...completed.value, key]
  if (payload?.next) {
    nextRoute.value = payload.next
    nextLabel.value = payload.label || ''
  }
  refreshCtx()
}

// ---- modal a11y: focus trap, scroll lock, Esc -------------------------------
// Lifted from the ent#52 wizard, with two changes only: Esc asks (the same
// confirm as "Finish later") instead of dismissing, and initial focus lands on
// the safe action.

const rootEl = ref(null)
const bodyEl = ref(null)
const continueBtn = ref(null)
const confirming = ref(false)

function trapRoot() {
  return confirming.value
    ? document.querySelector('[data-testid="confirm-dialog-content"]')
    : rootEl.value
}

function focusable(root) {
  if (!root) return []
  return Array.from(
    root.querySelectorAll(
      'a[href], button:not([disabled]), input, select, textarea, [tabindex]:not([tabindex="-1"])'
    )
  )
}

function focusSafeAction() {
  nextTick(() => continueBtn.value?.$el?.focus())
}

function onKeydown(e) {
  const root = trapRoot()
  if (!root) return
  // A modal a step opened on top (the real CreateAgentModal) owns the keyboard.
  if (e.target !== document.body && !root.contains(e.target)) return
  if (e.key === 'Escape') {
    e.preventDefault()
    if (confirming.value) {
      confirming.value = false
      focusSafeAction()
    } else {
      requestClose()
    }
    return
  }
  if (e.key === 'Tab') {
    const els = focusable(root)
    if (!els.length) return
    const first = els[0]
    const last = els[els.length - 1]
    // Focus fell to <body> (the Continue it sat on just became disabled on the
    // required step): pull it back in rather than let Tab walk the page behind.
    if (!root.contains(document.activeElement)) {
      e.preventDefault()
      ;(e.shiftKey ? last : first).focus()
    } else if (e.shiftKey && document.activeElement === first) {
      e.preventDefault()
      last.focus()
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault()
      first.focus()
    }
  }
}

function lock(on) {
  if (on) {
    document.addEventListener('keydown', onKeydown)
    // Scroll-lock the page behind the overlay; restored on close/unmount.
    document.body.style.overflow = 'hidden'
    focusSafeAction()
  } else {
    document.removeEventListener('keydown', onKeydown)
    document.body.style.overflow = ''
  }
}

watch(open, (now) => lock(now))

// A new step starts at its own top; the panel itself never moves.
watch(currentKey, () => {
  if (bodyEl.value) bodyEl.value.scrollTop = 0
})

onMounted(() => {
  // Shared and cached: `once()` makes these free when another consumer got
  // there first. The profile is fetched by `initializeAuth`.
  sessions.loadFeatureFlags()
  firstRun.fetchState()
  if (open.value) lock(true)
})

onBeforeUnmount(() => lock(false))
</script>

<style scoped>
/* Tailwind cannot express a two-stop radial gradient at these alphas, so it
   lives here built from theme() — never a literal, so the ratchet has nothing
   to catch and it cannot drift from the tokens. Static: nothing to freeze
   under prefers-reduced-motion. */
.first-run-aurora {
  background:
    radial-gradient(620px circle at 28% 22%,
      theme('colors.action-primary.500 / 11%'), transparent 62%),
    radial-gradient(540px circle at 78% 82%,
      theme('colors.accent-purple.500 / 8%'), transparent 62%);
}
.dark .first-run-aurora {
  background:
    radial-gradient(620px circle at 28% 22%,
      theme('colors.action-primary.500 / 20%'), transparent 62%),
    radial-gradient(540px circle at 78% 82%,
      theme('colors.accent-purple.500 / 15%'), transparent 62%);
}
</style>
