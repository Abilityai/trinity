<!--
  The Workspace stage for /workspace/build: chat on the left, the person's world
  on the right. The chat is the ordinary `PortalConversation`, with the guide
  agent, in that agent's Main chat. This component only decides what sits around
  it, in three steps:

    1. the guide isn't on this instance: offer to set it up;
    2. the guide is there and hasn't met this person: ask the first questions;
    3. otherwise: the conversation, whose first message carries those answers.

  The world reads the guide's `build` canvas. There is no canvas event on the
  socket to listen for, so it rereads quickly while a turn runs and slowly
  otherwise (the voice canvas does the same). A failed reread keeps what is on
  screen and says so.
-->
<template>
  <div class="flex-1 min-h-0 flex flex-col" data-testid="portal-build">
    <div class="sm:hidden shrink-0 flex border-b border-gray-200 dark:border-gray-800" role="tablist" aria-label="Build view">
      <button
        v-for="v in MOBILE_VIEWS"
        :key="v.id"
        type="button"
        role="tab"
        :aria-selected="mobileView === v.id"
        class="flex-1 py-2 text-sm"
        :class="mobileView === v.id ? 'font-medium text-gray-900 dark:text-gray-100 border-b-2 border-action-primary-600 dark:border-action-primary-500' : 'text-gray-500 dark:text-gray-400'"
        @click="mobileView = v.id"
      >{{ v.label }}</button>
    </div>

    <div class="flex-1 min-h-0 flex">
      <section
        class="relative min-w-0 flex-col sm:flex sm:flex-1"
        :class="mobileView === 'chat' ? 'flex flex-1' : 'hidden'"
        aria-label="Chat"
        data-tour="chat"
        ref="chatSection"
        @focusout="onChatFocusOut"
      >
        <div
          v-if="grantHeldBack"
          class="shrink-0 px-4 py-3 border-b border-status-warning-200 dark:border-status-warning-500/30 bg-status-warning-50 dark:bg-status-warning-500/10 text-xs text-status-warning-800 dark:text-status-warning-300"
          role="status"
          data-testid="build-grant-held-back"
        >
          {{ guide?.display_label }} can't create agents yet. An administrator needs to turn on "Manage other agents" for it, under its Settings, Guardrails, Permissions.
        </div>
        <PortalConversation
          v-if="phase === 'chat'"
          :key="`${guide.name}:${chatGeneration}`"
          class="min-h-0"
          :agent="guide"
          :roster="roster"
          :session-id="sessionAtMount"
          :prefill="firstMessage || prefill"
          :auto-send-prefill="Boolean(firstMessage)"
          :threads="threads"
          :rename="rename"
          focus-on-mount="fine-pointer"
          focused
          :as-note="noteLine"
          :inert="firstReplyPending || null"
          data-testid="build-chat"
          @first-message-sent="firstMessage = ''"
          @session-adopted="(id) => { sessionAtMount = id }"
          @sessions-changed="onTurnDone"
          @work-state="onWorkState"
          @open-menu="$emit('open-menu')"
          @open-thread="(t) => $emit('open-thread', t)"
          @new-chat="$emit('new-chat')"
          @escalate-to-room="(...args) => $emit('escalate-to-room', ...args)"
        >
          <template #header-end>
            <span class="sm:hidden"><slot name="header-end" /></span>
          </template>
          <template #empty>
            <!-- Nothing said yet: the guide says what it is for. Not while the answers are
                 on their way, which would swap it out under the person a moment later. -->
            <PortalBuildWelcome
              v-if="!answersOnTheirWay"
              :guide="guide"
              :guide-label="guide.display_label || BUILD_GUIDE_NAME"
              :first-agent="levelOwn.length === 0"
              :audit-first="auditFirst"
              @start="startFromWelcome"
            />
          </template>
          <template #notice>
            <div class="shrink-0 px-3 sm:px-4 empty:hidden">
              <!-- Arrives the way a level does, so the eye goes to it (motion-safe only). -->
              <Transition
                appear
                enter-active-class="motion-safe:transition motion-safe:duration-300 motion-safe:ease-out"
                enter-from-class="motion-safe:opacity-0 motion-safe:-translate-y-1"
              >
                <PortalBuildFleetCard
                  v-if="activeCard"
                  :key="activeCard.id"
                  class="mb-2"
                  :card="activeCard"
                  :guide="guide"
                  :status="cardStatus"
                  :reason="cardReason"
                  @confirm="confirmCard"
                  @decline="declineCard"
                  @shown="onShowMe"
                  @opened="onWalkOpened"
                  @dismiss="closeCard"
                />
              </Transition>
            </div>
          </template>
        </PortalConversation>

        <template v-else>
          <header class="shrink-0 flex items-center gap-2 px-3 sm:px-4 h-14 border-b border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900">
            <button
              type="button"
              class="sm:hidden -ml-1 p-2 text-gray-500 hover:text-gray-800 dark:text-gray-400 dark:hover:text-gray-200"
              aria-label="Menu"
              @click="$emit('open-menu')"
            >
              <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6h16M4 12h16M4 18h16" /></svg>
            </button>
            <span class="text-sm font-medium text-gray-900 dark:text-gray-100">Build an agent</span>
          </header>

          <PortalSkeleton v-if="phase === 'loading'" variant="stage" />

          <div v-else-if="phase === 'intake'" class="flex-1 flex items-center justify-center p-6" data-testid="build-waiting-onboarding">
            <p class="max-w-sm text-center text-sm text-gray-600 dark:text-gray-300">Your guide will start here once you've finished getting set up.</p>
          </div>

          <div v-else-if="phase === 'failed'" class="flex-1 flex items-center justify-center p-6">
            <LoadFailed
              title="Couldn't read your agents"
              :message="rosterError"
              @retry="$emit('retry-roster')"
            />
          </div>

          <div v-else-if="phase === 'connect'" class="flex-1 flex items-center justify-center p-6" data-testid="build-connect">
            <div class="max-w-sm text-center">
              <p class="text-lg font-semibold text-gray-900 dark:text-gray-100">One step before the guide starts</p>
              <p class="mt-2 text-sm text-gray-600 dark:text-gray-300">
                The guide and the agents it builds think with Claude, and it isn't connected to this instance yet. It takes about two minutes, using your Claude plan or an API key.
              </p>
              <div class="mt-4 flex flex-col items-center gap-2">
                <a
                  href="/?onboarding=1"
                  target="_blank"
                  rel="noopener"
                  class="inline-flex items-center rounded-md bg-action-primary-600 hover:bg-action-primary-700 px-3.5 py-2 text-sm font-medium text-white"
                  data-testid="build-connect-open"
                >Connect Claude</a>
                <BaseButton variant="ghost" size="sm" data-testid="build-connect-recheck" @click="recheckClaude">I've connected it</BaseButton>
              </div>
            </div>
          </div>

          <div v-else class="flex-1 flex items-center justify-center p-6" data-testid="build-setup">
            <div class="max-w-sm text-center">
              <p class="text-lg font-semibold text-gray-900 dark:text-gray-100">Set up the build guide</p>
              <p class="mt-2 text-sm text-gray-600 dark:text-gray-300">
                You build here with your own guide: an agent of yours on this instance that you talk to, and that builds the agents with you. Setting it up takes about a minute.
              </p>
              <p class="mt-2 text-xs text-gray-500 dark:text-gray-400">
                It will be allowed to create agents on this instance, and it asks you to approve each one first.
              </p>
              <BaseButton class="mt-4" :loading="deploying" data-testid="build-setup-deploy" @click="deploy">
                {{ deploying ? 'Setting up…' : 'Set up the guide' }}
              </BaseButton>
              <InlineError v-if="deployError" class="mt-4 text-left" :message="deployError" @dismiss="deployError = ''" />
            </div>
          </div>
        </template>

        <!-- The first time, the guide reads the answers before it can talk. The chat is
             frozen under this until its first reply is in, so nothing can be typed,
             sent or stopped meanwhile. -->
        <div
          v-if="phase === 'chat' && firstReplyPending"
          role="status"
          class="absolute inset-0 z-30 flex items-center justify-center p-6 bg-white dark:bg-gray-900"
          data-testid="build-getting-ready"
        >
          <div class="max-w-sm text-center">
            <p class="flex items-center justify-center gap-2.5 text-lg font-semibold text-gray-900 dark:text-gray-100">
              <span class="h-2.5 w-2.5 rounded-full bg-action-primary-500 motion-safe:animate-pulse" aria-hidden="true"></span>
              {{ guide?.display_label }} is getting ready
            </p>
            <p class="mt-2 text-sm text-gray-600 dark:text-gray-300">
              It's reading your answers and your company research. This takes a minute or two, and the chat opens by itself when it's ready.
            </p>
          </div>
        </div>
      </section>

      <div
        class="min-w-0 sm:block sm:flex-1"
        :class="mobileView === 'world' ? 'block flex-1' : 'hidden'"
        data-tour="company"
      >
        <PortalBuildWorld
          ref="worldPanel"
          :world="world"
          :intake="effectiveIntake"
          :roster="roster"
          :orchestration-available="orchestrationAvailable"
          :read-error="readError"
          :research="research"
          :instance-host="instanceHost"
          :levels="levels"
          :level-celebrate="levelCelebrate"
          :guide-label="guide?.display_label || BUILD_GUIDE_NAME"
          @dismiss-level="levelCelebrate = null"
          :handoff="handoff"
          @keys-later="leaveKeysForLater"
          @handoff-shown="onHandoffShown"
          @handoff-dismiss="closeHandoff"
          @ask="ask"
          @retry="refresh"
          @dismiss-error="readError = ''"
          @finish-profile="$emit('finish-profile')"
          @retry-research="retryResearch"
          @get-help="$emit('get-help')"
        >
          <template #header-end><slot name="header-end" /></template>
        </PortalBuildWorld>
      </div>
    </div>
    <PortalBuildTour :open="tourOpen" :guide-name="guide?.display_label || BUILD_GUIDE_NAME" @close="closeTour" />
    <!-- Level moments: the levels at the start of a first build, a new agent in the
         sidebar, a level ticking. The same spotlight, one or two stops. -->
    <!-- The way to the Dashboard, lit in the sidebar before a person's first walk opens. -->
    <PortalBuildTour :open="Boolean(door)" :steps="DOOR_STEPS" :min-steps="1" :skippable="false" done-label="Open the Dashboard" testid="build-walk-door" @close="closeDoor" />
    <PortalBuildTour :open="Boolean(moment)" :steps="moment?.steps || []" :min-steps="1" :skippable="false" testid="build-moment" @close="closeMoment" />
  </div>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import BaseButton from '@/components/base/BaseButton.vue'
import InlineError from '@/components/InlineError.vue'
import LoadFailed from '@/components/LoadFailed.vue'
import { useClientPortalStore } from '@/stores/clientPortal'
import { useFirstRunStore } from '@/stores/firstRun'
import { useAuthStore } from '@/stores/auth'
import { useSessionsStore } from '@/stores/sessions'
import PortalConversation from '../PortalConversation.vue'
import PortalSkeleton from '../PortalSkeleton.vue'
import { canvasChanged } from '../portalVoiceMode'
import PortalBuildTour from './PortalBuildTour.vue'
import { WALK_DOOR_SEEN, walkDoorDue, walkDoorSteps } from './portalBuildTour'
import PortalBuildFleetCard from './PortalBuildFleetCard.vue'
import PortalBuildWelcome from './PortalBuildWelcome.vue'
import PortalBuildWorld from './PortalBuildWorld.vue'
import { FLEET_CANVAS_ID, cardFor, outcomeNote, proposalFrom } from './fleetChanges'
import { changeHeld, fleetRefreshDue, outcomeMessage, readFleet, runChange, writeFleetFile } from './fleetActions'
import { useBuildIntakeStore } from '@/stores/buildIntake'
import { intakeWithBase, laterMessage, levelAgents, levelMessage, levelsFrom, levelsOf, levelsSummary, markLevelsSeen, newlyReached, withLevels } from './levels'
import { handoffDue, latestHandoff, markHandoffSeen } from './handoff'
import { INTRO_STEPS, introDue, laterNote, levelNote, markLevelsIntroSeen, reachedSteps, retryDropped } from './levelMoments'
import { readLevelSignals } from './levelSignals'
import {
  BUILD_CANVAS_ID,
  BUILD_GUIDE_NAME,
  markTourSeen,
  BUILD_RESEARCH_CANVAS_ID,
  BUILD_POLL_IDLE_MS,
  BUILD_POLL_LIVE_MS,
  buildPhase,
  guideCannotCreate,
  canvasToState,
  clearResearch,
  firstMessagePending,
  instanceAgentsFor,
  isBuildCrew,
  intakeMessage,
  markIntakeSent,
  markProposalHandled,
  rebindToGuide,
  keepGuideCard,
  companyToPublish,
  markResearchStarted,
  myGuideCard,
  researchView,
  shouldStartResearch,
  worldView,
} from './portalBuild'
import { startCompanyResearch } from './useCompanyResearch'
import { deployMyGuide, myGuideName } from './useMyGuide'


const MOBILE_VIEWS = Object.freeze([
  { id: 'chat', label: 'Chat' },
  { id: 'world', label: 'Your page' },
])

const props = defineProps({
  roster: { type: Array, default: () => [] },
  rosterLoaded: { type: Boolean, default: false },
  rosterError: { type: String, default: '' },
  threads: { type: Array, default: () => [] },
  threadsLoaded: { type: Boolean, default: false },
  rename: { type: Function, default: null },
  orchestrationAvailable: { type: Boolean, default: false },
  // The Workspace owns the questions and their window, because the window opens on
  // any page. This page starts the chat from the answers once the window has closed.
  intake: { type: Object, required: true },
  onboardingOpen: { type: Boolean, default: false },
})
const emit = defineEmits(['open-menu', 'open-thread', 'new-chat', 'escalate-to-room', 'sessions-changed', 'retry-roster', 'update:intake', 'need-onboarding', 'finish-profile', 'get-help', 'state'])

const store = useClientPortalStore()
const sessions = useSessionsStore()
const firstRunStore = useFirstRunStore()
const authStore = useAuthStore()
const intakeStore = useBuildIntakeStore()
const instanceHost = typeof window !== 'undefined' ? window.location.host : ''

// Their own guide, never someone else's shared with them, kept as it is until something
// on its card changes (`keepGuideCard`).
const guide = shallowRef(null)
watch(() => myGuideCard(props.roster), (card) => {
  guide.value = keepGuideCard(guide.value, card ? { ...card, display_label: card.display_label || BUILD_GUIDE_NAME } : null)
}, { immediate: true })

const mobileView = ref('chat')
const chatGeneration = ref(0)
const firstReplyPending = ref(false)
const FIRST_TURN_START_MS = 20000
let readyTimer = null
const canvas = ref(null)
const canvasRead = ref(false)
const readError = ref('')
const working = ref(false)
const firstMessage = ref('')
const prefill = ref('')
const sessionAtMount = ref(null)
const deploying = ref(false)
const deployError = ref('')
const grantHeldBack = ref(false)

// Settings changes in the chat: the person's own agents, read with their session and
// written into their guide's workspace; the guide's one proposal; and the card for it.
const http = store.platformHttp()
const ownAgents = computed(() => props.roster.filter((a) => a?.owned === true && !isBuildCrew(a.name)))
const fleet = ref(null)
const fleetCanvas = ref(null)
// The proposal the fleet was last read for (see the watch by `refreshFleet`).
const fleetReadFor = ref('')
const activeCard = ref(null)
const cardStatus = ref('idle')
const cardReason = ref('')
const pendingMessage = ref('')
const fleetCard = computed(() => {
  const proposal = proposalFrom(fleetCanvas.value)
  if (!proposal || !fleet.value || (props.intake.fleetHandled || []).includes(proposal.id)) return null
  // Judged against a list read after it arrived (`fleetReadFor`), never an older one.
  if (fleetReadFor.value !== proposal.id) return null
  return cardFor(proposal, fleet.value)
})
watch(fleetCard, (card) => {
  if (!card || cardStatus.value === 'working') return
  // An unanswered refusal is worked out again once the fleet knows a just-built agent.
  const same = card.id === activeCard.value?.id
  if (same && !(activeCard.value.kind === 'refused' && card.kind !== 'refused')) return
  activeCard.value = card
  cardStatus.value = 'idle'
  cardReason.value = ''
})

const state = computed(() => canvasToState(canvas.value))
const researchCanvas = ref(null)
const researchExecution = ref(null)
// The company profile kept on the instance: a colleague's research, for someone who
// started from it, and where this person's own research and context are published.
const companyProfile = ref(null)
const research = computed(() => researchView({ intake: props.intake, canvas: researchCanvas.value, execution: researchExecution.value, profile: companyProfile.value }))
const world = computed(() => worldView(state.value, props.intake))
// The Workspace's sidebar shows the same red as this page's stepper.
watch(state, (now) => emit('state', now))
// Someone who has met the guide in another browser has a state and no local
// answers. They are past the questions, so the world treats them as such.
const effectiveIntake = computed(() => (state.value ? { ...props.intake, done: true } : props.intake))

const phase = computed(() => buildPhase({
  rosterLoaded: props.rosterLoaded,
  rosterError: props.rosterError,
  hasGuide: Boolean(guide.value),
  claude: {
    loaded: sessions.featureFlagsLoaded,
    failed: sessions.featureFlagsFailed,
    configured: sessions.claudeAuthConfigured,
  },
  canvasRead: canvasRead.value,
  threadsLoaded: props.threadsLoaded,
  // Held while the window is open, so nothing is sent behind it.
  intakeDone: effectiveIntake.value.done && (Boolean(state.value) || !props.onboardingOpen),
}))

function mainChatId(agentName = guide.value?.name) {
  const main = props.threads.find((t) => t && t.agent_name === agentName && t.is_main)
  return main?.id || main?.session_id || null
}

// The answers can wait for a while, across reloads, while Claude is connected and the
// guide set up. They go when the chat is open and the window has closed.
function startChatIfReady() {
  if (props.onboardingOpen || phase.value !== 'chat' || firstMessage.value || tourOpen.value) return
  // The first message tells Trinity's samples apart from the person's own agents.
  if (!firstRunStore.loaded) return
  // A colleague's research goes into this person's guide before it is told it's there.
  if (props.intake.research?.shared && !props.intake.research.copied) {
    copySharedCompany()
    return
  }
  startResearchIfDue()
  if (!firstMessagePending(props.intake, state.value)) return
  firstReplyPending.value = true
  // The chat is frozen until the reply. A first message that never started (the turn
  // never went live) must not leave it frozen, so it thaws and shows why.
  clearTimeout(readyTimer)
  readyTimer = setTimeout(() => { if (!working.value) firstReplyPending.value = false }, FIRST_TURN_START_MS)
  firstMessage.value = intakeMessage(props.intake, instanceAgentsFor(props.roster, []), firstRunStore.seededAgents, authStore.userEmail)
  emit('update:intake', markIntakeSent(props.intake, guide.value?.name))
  // The first message goes when a conversation mounts, so an open one starts again.
  sessionAtMount.value = mainChatId()
  chatGeneration.value += 1
}

// The first time on this page: a walk round the Workspace, then the chat starts.
const tourOpen = computed(() => phase.value === 'chat' && props.threadsLoaded && !props.onboardingOpen && !props.intake.tourSeen)

function closeTour() {
  emit('update:intake', markTourSeen(props.intake))
  // The prop updates with the next patch.
  setTimeout(startChatIfReady)
}

// Someone who left setup before Claude or the guide was ready: it starts with the chat.
async function startResearchIfDue() {
  if (!shouldStartResearch(props.intake) || researchCanvas.value) return
  emit('update:intake', markResearchStarted(props.intake, '', Date.now(), guide.value?.name))
  await nextTick()
  try {
    const username = authStore.principalId
    const id = await startCompanyResearch(store, props.intake, { guideName: myGuideName({ roster: props.roster, username }), username })
    emit('update:intake', markResearchStarted(props.intake, id, Date.now(), guide.value?.name))
    schedule()
  } catch {
    // Findings offers it again.
    researchExecution.value = { status: 'failed' }
    emit('update:intake', markResearchStarted(props.intake, 'not-started', Date.now(), guide.value?.name))
  }
}

function retryResearch() {
  researchExecution.value = null
  emit('update:intake', clearResearch(props.intake))
  // The prop updates with the next patch, so this reads the answers it just changed.
  setTimeout(startResearchIfDue)
}

// The first job one of their agents did for another, until they opened the walk to it or
// put the note away. Read from the jobs the page already reads for the levels.
const handoff = computed(() => {
  const found = latestHandoff(levelSignals.value)
  if (!handoffDue({ handoff: found, intake: props.intake })) return null
  const labelOf = (name) => props.roster.find((a) => a?.name === name)?.display_label || name
  return { ...found, fromLabel: labelOf(found.from), toLabel: labelOf(found.to) }
})
function closeHandoff() {
  if (!props.intake?.handoffSeen) emit('update:intake', markHandoffSeen(props.intake))
}
function onHandoffShown(event, link) {
  // A first walk lights the way to the Dashboard first (`onShowMe`), as every Show me does.
  onShowMe(event, link)
  closeHandoff()
}

// The person leaves the keys for later on the card: their own choice, kept with their
// answers, so the card moves on at once; the guide is told the level and the real next one.
function leaveKeysForLater() {
  if (props.intake?.keysLater) return
  const message = laterMessage(levels.value)
  emit('update:intake', withLevels(props.intake, { keysLater: true }))
  sendToGuide(message)
}

// What the guide's file holds: their agents' settings, and where they are in the levels,
// so it never has to count to know which level is next.
function fleetFile() {
  const summary = levelsSummary(levels.value)
  return summary ? { ...fleet.value, levels: summary } : fleet.value
}
function ask(text) {
  mobileView.value = 'chat'
  prefill.value = ''
  // A fresh tick so asking the same thing twice still fills the composer.
  queueMicrotask(() => { prefill.value = text })
}

// The conversation resumes the guide's Main chat, so a reload picks up where the
// person left off. A chat that starts without one follows the session its first send
// adopts, as the Workspace's own chat does. Left without it, the conversation takes a
// new copy of the guide's card for a new chat, and empties.
watch(phase, (now, before) => {
  if (now === 'intake') emit('need-onboarding')
  if (now !== 'chat' || before === 'chat') return
  sessionAtMount.value = mainChatId()
  if (rebindIfNew()) return
  startChatIfReady()
}, { flush: 'sync' })

// A guide's address comes from the person, so a guide set up again after a deleted one
// has the same name. With no page and no chat of theirs, it has none of their answers:
// they go again (`rebindToGuide` says when its research runs again). Checked once per
// guide when the chat first opens, before this page sends anything itself. True when
// the answers were rebound.
const rebindChecked = new Set()
function rebindIfNew() {
  const name = guide.value?.name
  if (!name || rebindChecked.has(name)) return false
  rebindChecked.add(name)
  if (state.value || readError.value) return false
  const used = (t) => t && !t.is_room && t.agent_name === name && (t.last_message_at || Number(t.message_count || 0) > 0)
  if (props.threads.some(used)) return false
  const next = rebindToGuide(props.intake, name, { fresh: true })
  if (next === props.intake) return false
  emit('update:intake', next)
  // The prop updates with the next patch.
  setTimeout(startChatIfReady)
  return true
}

// The answers are about to go, or have gone and the guide hasn't replied yet.
const answersOnTheirWay = computed(() => Boolean(firstMessage.value) || firstReplyPending.value || firstMessagePending(props.intake, state.value))

// They chose the AI audit and it isn't done: the welcome starts it, never a build.
const auditFirst = computed(() => props.intake.audit === 'start' && levelOwn.value.length === 0 && state.value?.audit?.status !== 'done')

// A start button on the welcome: the message goes as the chat's first, in one press.
function startFromWelcome(text) {
  if (!text || firstMessage.value) return
  firstMessage.value = text
  sessionAtMount.value = mainChatId()
  chatGeneration.value += 1
}

// The first message waits for Trinity's samples to be known (see startChatIfReady).
watch(() => firstRunStore.loaded, (loaded) => {
  if (loaded) startChatIfReady()
})

watch(() => props.onboardingOpen, (open) => {
  if (!open) startChatIfReady()
})

// Asked for from this page (the red profile): the guide is told in its own chat, once
// it has finished any reply it is in the middle of.
watch([() => props.intake.tellGuide, working], ([tell, busy]) => {
  if (!tell || busy) return
  // The prop updates with the next patch.
  setTimeout(startChatIfReady)
})

// Doing the audit in the chat completes a profile they skipped, so the red clears.
watch(state, (now) => {
  if (props.intake.skipped && (now?.profile_complete || now?.audit?.status === 'done')) {
    emit('update:intake', { ...props.intake, skipped: false })
  }
})

let timer = null
let reading = false
let readAgain = false

async function refresh() {
  if (!guide.value) {
    canvasRead.value = true
    return
  }
  // A read asked for while one is in flight (the guide's turn ending during a poll)
  // is not dropped: the page reads once more as soon as this one is back.
  if (reading) {
    readAgain = true
    return
  }
  reading = true
  try {
    const next = await store.fetchAgentCanvas(guide.value.name, BUILD_CANVAS_ID)
    if (canvasChanged(canvas.value, next)) canvas.value = next
    readError.value = ''
  } catch (e) {
    // 404 is the ordinary "the guide hasn't drawn anything yet".
    if (e?.response?.status !== 404) {
      readError.value = "Couldn't refresh your company page, so this may be out of date. It will keep trying."
    }
  } finally {
    reading = false
    canvasRead.value = true
  }
  if (readAgain) {
    readAgain = false
    return refresh()
  }
  await readFleetCanvas()
  await readResearch()
  askForMissingAgents()
  readLevels()
  // A colleague's research that couldn't be copied yet, because their guide wasn't running.
  if (props.intake.research?.shared && !props.intake.research.copied && phase.value === 'chat') startChatIfReady()
}

async function readFleetCanvas() {
  try {
    const next = await store.fetchAgentCanvas(guide.value.name, FLEET_CANVAS_ID)
    if (canvasChanged(fleetCanvas.value, next)) fleetCanvas.value = next
  } catch {
    // 404 is the ordinary "no change proposed yet".
  }
}

// The guide reads this file before it proposes anything. A failed write leaves the old
// one, and the guide says it can't see the fleet yet.
let fleetReadAt = 0
// One read at a time: one asked for meanwhile runs once more after it, so an older answer
// never lands after a newer one.
let fleetReading = null
let fleetAgain = false
function refreshFleet() {
  if (!guide.value) return Promise.resolve()
  if (fleetReading) {
    fleetAgain = true
    return fleetReading
  }
  fleetReading = (async () => {
    do {
      fleetAgain = false
      fleetReadAt = Date.now()
      fleet.value = await readFleet(http, ownAgents.value)
      try {
        await writeFleetFile(http, guide.value.name, fleetFile())
      } catch {
        // Tried again after the next change, or the next agent.
      }
    } while (fleetAgain && guide.value)
  })().finally(() => { fleetReading = null })
  return fleetReading
}

// A new proposal can name what the guide made in the same turn, such as a schedule it
// just created, so the list is read again before its card is worked out: on the older
// list it would be refused as a change the page can't make.
watch(() => proposalFrom(fleetCanvas.value)?.id, async (id) => {
  if (!id || fleetReadFor.value === id || (props.intake.fleetHandled || []).includes(id)) return
  await refreshFleet()
  fleetReadFor.value = id
})

async function loadCompanyProfile() {
  try {
    companyProfile.value = await store.fetchCompanyProfile()
  } catch {
    companyProfile.value = null
  }
}

let copying = false
async function copySharedCompany() {
  if (copying || !guide.value) return
  copying = true
  try {
    if (!companyProfile.value) await loadCompanyProfile()
    const profile = companyProfile.value
    if (!profile) return
    const files = `/api/agents/${encodeURIComponent(guide.value.name)}/files?path=`
    await http.put(`${files}company-research.md`, { content: profile.research_md || '' })
    if (profile.context_md) await http.put(`${files}context.md`, { content: profile.context_md })
    emit('update:intake', { ...props.intake, research: { ...props.intake.research, copied: true, copiedTo: guide.value.name } })
    // The prop updates with the next patch, and then the first message can go.
    setTimeout(startChatIfReady)
  } catch {
    // Their guide isn't running yet: tried again on the next refresh.
  } finally {
    copying = false
  }
}

// What this person's guide found and wrote about the company is kept once on the
// instance, for the next colleague. The kept profile is read first, so publishing
// never blanks a colleague's text and an unchanged one isn't sent. A failure doesn't
// matter here: it is a help.
const profileRead = ref(false)
watch(() => [profileRead.value, research.value.status === 'done' ? research.value.report : '', state.value?.context_md || ''], ([read, report, context]) => {
  if (!read) return
  const doc = companyToPublish({ answers: props.intake.answers, stateName: state.value?.company?.name, report, context, profile: companyProfile.value })
  if (!doc) return
  companyProfile.value = { ...companyProfile.value, ...doc }
  store.saveCompanyProfile(doc).catch(() => {})
})

// What the page tells the guide goes as the person's next message, once it has
// finished any reply it is in the middle of.
function sendToGuide(text) {
  if (working.value) {
    // A second line waiting for the guide joins the first rather than replacing it.
    pendingMessage.value = pendingMessage.value ? `${pendingMessage.value}\n${text}` : text
    return
  }
  // A line still waiting for the conversation to send it is joined the same way.
  if (firstMessage.value) {
    firstMessage.value = `${firstMessage.value}\n${text}`
    return
  }
  pendingMessage.value = ''
  firstMessage.value = text
  sessionAtMount.value = mainChatId()
  chatGeneration.value += 1
}
watch(working, (busy) => {
  if (!busy && pendingMessage.value) sendToGuide(pendingMessage.value)
})

async function confirmCard() {
  const card = activeCard.value
  if (!card || cardStatus.value === 'working') return
  cardStatus.value = 'working'
  const result = await runChange(http, card)
  const outcome = !result.ok ? result
    : (await changeHeld(http, card)) ? 'done'
      : { ok: false, refused: false, reason: "Trinity took it, but it doesn't show yet." }
  // Refused means nothing was changed; anything else the page can't vouch for either way.
  cardStatus.value = outcome === 'done' ? 'done' : outcome.refused ? 'failed' : 'unsure'
  cardReason.value = outcome === 'done' ? '' : outcome.reason
  emit('update:intake', markProposalHandled(props.intake, card.id))
  await refreshFleet()
  sendToGuide(outcomeMessage(card, outcome))
}

function declineCard() {
  const card = activeCard.value
  if (!card) return
  emit('update:intake', markProposalHandled(props.intake, card.id))
  activeCard.value = null
  sendToGuide(outcomeMessage(card, 'declined'))
}

function closeCard() {
  if (activeCard.value) emit('update:intake', markProposalHandled(props.intake, activeCard.value.id))
  activeCard.value = null
}

async function readResearch() {
  if (!guide.value) return
  try {
    const next = await store.fetchAgentCanvas(guide.value.name, BUILD_RESEARCH_CANVAS_ID)
    if (canvasChanged(researchCanvas.value, next)) researchCanvas.value = next
    return
  } catch {
    // 404 is the ordinary "it hasn't written anything yet".
  }
  const id = props.intake.research?.executionId
  if (!id || id === 'not-started') return
  try {
    researchExecution.value = await store.fetchGuideExecution(guide.value.name, id)
  } catch {
    // Unknown is not failed. It reads again next time.
  }
}

function schedule() {
  clearTimeout(timer)
  timer = setTimeout(async () => {
    await refresh()
    schedule()
  }, working.value || research.value.status === 'running' ? BUILD_POLL_LIVE_MS : BUILD_POLL_IDLE_MS)
}

function onWorkState(signal) {
  const live = Number(signal?.live) > 0
  if (live) clearTimeout(readyTimer)
  // The first turn has started and finished, so the reply is there.
  if (working.value && !live) firstReplyPending.value = false
  if (live !== working.value) {
    working.value = live
    schedule()
  }
}

// A guide deployed by someone who isn't an admin can't create agents until an admin
// turns that on, and the page says so.
async function checkGuideGrant() {
  if (!guide.value) return
  try {
    grantHeldBack.value = guideCannotCreate(await store.fetchCapabilityGrants(guide.value.name))
  } catch {
    // Not this person's to read. The guide reports a refused build either way.
    grantHeldBack.value = false
  }
}

async function onTurnDone(...args) {
  emit('sessions-changed', ...args)
  // The guide may have created an agent in that turn, whether or not it wrote the page:
  // the sidebar, the fleet and the levels all read the roster, so it is read again.
  emit('retry-roster')
  // The guide has had its say about an answered card, so the card goes.
  if (['done', 'failed', 'unsure'].includes(cardStatus.value)) {
    activeCard.value = null
    cardStatus.value = 'idle'
  }
  await refresh()
  checkGuideGrant()
  readLevels(true)
}

// Levels (core-agent AC-15 to AC-17): read with the person's own session when the chat
// opens, after each of the guide's turns, when their agents change, when they come back
// to the page, and at most every minute otherwise; never while the page is hidden, and
// never before the page knows which agents are Trinity's samples. Each level reached
// and not celebrated before is celebrated once and told to the guide, once it has their
// answers, isn't replying, and the person isn't in the middle of writing something.
const LEVELS_READ_MS = 60000
const KEYS_RECHECK_MS = 120000
const levelSignals = ref(null)
const levelCelebrate = ref(null)
// The levels part of the saved answers, in the eight-level numbers (`levelsOf`).
const savedLevels = computed(() => levelsOf(props.intake))
// Each agent's playbooks when the page first saw it (Level 2 is one added since), kept
// here as well as on the saved answers so a save racing another can't lose it.
const levelBase = ref({ ...savedLevels.value.levelsBase })
watch(() => savedLevels.value.levelsBase, (base) => { levelBase.value = { ...levelBase.value, ...base } })
const levels = computed(() => (levelSignals.value ? levelsFrom(levelSignals.value, savedLevels.value.levelsKept, levelBase.value, { keysPassed: Boolean(state.value?.keys_not_needed), keysLater: Boolean(state.value?.keys_later || props.intake?.keysLater) }) : []))
const levelsLine = computed(() => JSON.stringify(levelsSummary(levels.value)))
// The levels moved (a tick, a level left for later): the file follows, without a new read.
watch(levelsLine, (now, before) => {
  if (now === before || now === 'null' || !fleet.value || !guide.value) return
  writeFleetFile(http, guide.value.name, fleetFile()).catch(() => { /* written again with the next fleet read */ })
})

// When the page sends the person's answers itself on this visit, the levels already reached
// then are shown on the card but not told to the guide, over its first question.
const arrivingNow = !props.intake.sent
let reachedOnArrival = null
const levelOwn = computed(() => levelAgents(props.roster, firstRunStore.seededAgents))
const samplesKnown = computed(() => firstRunStore.loaded && !firstRunStore.failed)
const chatSection = ref(null)
let levelsReading = false
let levelsAgain = false
let levelsReadAt = 0
let keysCheckedAt = 0
const levelsAnnounced = new Set()

async function readLevels(force = false) {
  if (!guide.value) return
  if (!samplesKnown.value) {
    if (firstRunStore.failed && !firstRunStore.loading) firstRunStore.fetchState(true)
    return
  }
  if (typeof document !== 'undefined' && document.hidden) return
  if (levelsReading) {
    levelsAgain = levelsAgain || force
    return
  }
  if (!force && Date.now() - levelsReadAt < LEVELS_READ_MS) return
  levelsReading = true
  const recheckKeys = force || Date.now() - keysCheckedAt > KEYS_RECHECK_MS
  try {
    levelSignals.value = await readLevelSignals(http, { own: levelOwn.value, email: authStore.userEmail, previous: levelSignals.value, recheckKeys })
    levelsReadAt = Date.now()
    if (recheckKeys) keysCheckedAt = levelsReadAt
    const saved = intakeWithBase(props.intake, intakeStore.storedIntake(), levelBase.value, levelSignals.value)
    levelBase.value = saved.base
    if (saved.intake) emit('update:intake', saved.intake)
    if (arrivingNow && reachedOnArrival === null) reachedOnArrival = new Set(levels.value.filter((l) => l.reached).map((l) => l.id))
  } catch {
    // A help, never an error on the page. It reads again next time.
  } finally {
    levelsReading = false
  }
  if (levelsAgain) {
    levelsAgain = false
    readLevels(true)
  }
  announceLevels()
}

// Something the person is writing is theirs: a level line never replaces it.
function composerHasText() {
  const box = chatSection.value?.querySelector('textarea')
  return Boolean(box && box.value.trim())
}

function announceLevels() {
  const told = props.intake.sent || Boolean(state.value)
  if (phase.value !== 'chat' || !told || firstReplyPending.value || firstMessage.value || composerHasText()) return
  // What another tab already celebrated counts too.
  const seen = [...new Set([...savedLevels.value.levelsSeen, ...levelsOf(intakeStore.storedIntake()).levelsSeen, ...levelsAnnounced])]
  showUntoldOnce()
  const fresh = newlyReached(levels.value, seen)
  if (!fresh.length) return
  fresh.forEach((id) => levelsAnnounced.add(id))
  const reachedNow = levels.value.filter((l) => fresh.includes(l.id))
  levelCelebrate.value = reachedNow[reachedNow.length - 1]
  const tell = reachedNow.filter((l) => !reachedOnArrival?.has(l.id))
  // The moment first, then the guide is told, so it moves on once they've seen the tick.
  // Until then the levels wait on the saved answers (`levelsUntold`), so a reload or a
  // closed tab during the moment shows it again rather than never telling the guide.
  // Saved before the moment is queued: with the card on screen it tells the guide at once.
  const tellIds = tell.map((l) => l.id)
  const untold = [...new Set([...untoldOf(props.intake), ...tellIds])]
  emit('update:intake', { ...markLevelsSeen(withLevels(props.intake, { levelsSeen: seen, levelsBase: levelBase.value }), fresh, levels.value), levelsUntold: untold })
  if (tellIds.length) queueReached(tellIds)
}
// Level moments (core-agent AC-17). A level reached waits until the guide has finished
// replying; the intro, when the first build starts, shows while the guide is still writing,
// because its words say the page is lighting the levels up now. Both wait for the tour and
// the setup window to close and the first reply to be in, one at a time. A stop on the
// Levels card brings the right side to You first, and back after.
const worldPanel = ref(null)
const moment = ref(null)
const momentQueue = []
let momentRestore = null
let introQueued = false
// A short pause between two moments, so the right side has redrawn and finished moving
// before the next one looks at whether the Levels card is on screen; a longer one before a
// moment that found nothing to light comes back.
const MOMENT_GAP_MS = 400
const MOMENT_RETRY_MS = 1500
const momentPause = ref(false)
const guideLabel = computed(() => guide.value?.display_label || BUILD_GUIDE_NAME)
// Lines the page sends for the person (a level reached, a card's outcome) read as notes.
const noteLine = (text) => levelNote(text, guideLabel.value) || laterNote(text, guideLabel.value) || outcomeNote(text, guideLabel.value)
const momentBase = computed(() => phase.value === 'chat' && !firstReplyPending.value
  && !props.onboardingOpen && !tourOpen.value && !moment.value && !momentPause.value)
const momentFree = computed(() => momentBase.value && !working.value)
const freeFor = (m) => (m?.kind === 'intro' ? momentBase.value : momentFree.value)

// The agent a Level 1 moment lights in the sidebar: their only one, when it is.
function firstAgent() {
  if (levelOwn.value.length !== 1) return null
  const name = levelOwn.value[0]
  const row = props.roster.find((a) => a?.name === name)
  return { name, label: row?.display_label || name }
}

function levelsCard() {
  const el = document.querySelector('[data-tour="levels"]')
  const r = el?.getBoundingClientRect?.()
  return r && r.width > 0 && r.height > 0 ? r : null
}
async function levelsCardShown(ms = 1500) {
  const until = Date.now() + ms
  while (!levelsCard() && Date.now() < until) await new Promise((done) => setTimeout(done, 50))
}

// `then` runs once the moment has been seen (or had nothing to show).
function queueMoment(kind, steps, then = null) {
  if (!steps?.length) { then?.(); return }
  momentQueue.push({ kind, steps, then })
  showMomentIfFree()
}

function untoldOf(intake) {
  return Array.isArray(intake?.levelsUntold) ? intake.levelsUntold.filter(Number.isInteger) : []
}
function queueReached(ids) {
  queueMoment('reached', reachedSteps({ reached: ids, levels: levels.value, agent: firstAgent() }), () => tellOnce(ids))
}
// The guide is told each level once: not again by a second tab, or after a reload, that
// finds it already told on the saved answers.
// The answers as last saved (by this tab or another), which may be newer than the prop.
const latestIntake = () => intakeStore.intake || props.intake
function tellOnce(ids) {
  const stored = intakeStore.storedIntake()
  const waiting = untoldOf(Array.isArray(stored?.levelsUntold) ? stored : latestIntake())
  const pending = ids.filter((id) => waiting.includes(id))
  if (pending.length) sendToGuide(levelMessage(levels.value.filter((l) => pending.includes(l.id)), levels.value))
  const latest = latestIntake()
  emit('update:intake', { ...latest, levelsUntold: untoldOf(latest).filter((id) => !ids.includes(id)) })
}
// On this visit's first look: a level still waiting from a moment that never closed (a
// reload, a closed tab) gets its moment again, then the guide is told.
let untoldChecked = false
function showUntoldOnce() {
  if (untoldChecked || !levels.value.length) return
  untoldChecked = true
  const waiting = untoldOf(props.intake).filter((id) => !levelsAnnounced.has(id))
  const still = waiting.filter((id) => levels.value.some((l) => l.id === id && l.reached))
  still.forEach((id) => levelsAnnounced.add(id))
  if (still.length) queueReached(still)
  if (still.length !== waiting.length) {
    emit('update:intake', { ...props.intake, levelsUntold: untoldOf(props.intake).filter((id) => still.includes(id) || !waiting.includes(id)) })
  }
}

let showing = false
async function showMomentIfFree() {
  if (showing || !momentQueue.length || !freeFor(momentQueue[0])) return
  showing = true
  let shown = false
  try {
    const next = momentQueue.shift()
    let steps = next.steps
    // Level 1 reached before the intro could show: it has nothing left to say.
    if (next.kind === 'intro' && !introDue({ levels: levels.value, stage: world.value.stage, intake: props.intake })) steps = []
    if (steps.some((s) => s.company)) {
      // The Levels card is lit every time, wherever the right side was: under You, with
      // the page put back afterwards.
      const restoreWorld = worldPanel.value?.showLevels?.() || null
      const view = mobileView.value
      mobileView.value = 'world'
      momentRestore = () => { restoreWorld?.(); mobileView.value = view }
      await nextTick()
      await levelsCardShown()
      // Something started meanwhile (the setup window, a reply for a level reached): it waits.
      if (!freeFor(next)) {
        momentRestore?.()
        momentRestore = null
        momentQueue.unshift(next)
        return
      }
    }
    if (steps.length) {
      moment.value = { ...next, steps }
      shown = true
    } else {
      if (next.kind === 'intro') emit('update:intake', markLevelsIntroSeen(props.intake))
      momentRestore?.()
      momentRestore = null
      next.then?.()
    }
  } finally {
    showing = false
  }
  if (!shown) showMomentIfFree()
}

function closeMoment(reason = 'done') {
  const done = moment.value
  moment.value = null
  momentRestore?.()
  momentRestore = null
  // Closed with nothing on screen to light: it comes back, and nobody is told it was seen.
  const again = retryDropped(done, reason)
  if (again) {
    momentQueue.unshift(again)
    momentPause.value = true
    setTimeout(() => { momentPause.value = false }, MOMENT_RETRY_MS)
    return
  }
  if (done?.kind === 'intro') emit('update:intake', markLevelsIntroSeen(props.intake))
  momentPause.value = true
  setTimeout(() => { momentPause.value = false }, MOMENT_GAP_MS)
  done?.then?.()
}
watch([momentBase, momentFree], () => showMomentIfFree())

// The levels, once, when the first build starts.
watch([levels, () => world.value.stage, () => props.intake.levelsIntroSeen], () => {
  if (introQueued || !introDue({ levels: levels.value, stage: world.value.stage, intake: props.intake })) return
  introQueued = true
  queueMoment('intro', INTRO_STEPS)
})

function onChatFocusOut() {
  setTimeout(announceLevels)
}
function onPageVisible() {
  if (document.hidden) return
  readLevels(true)
  refreshFleetOnReturn()
}
watch([levels, () => props.intake.sent, () => Boolean(state.value), firstReplyPending, phase, firstMessage], announceLevels)
watch([() => phase.value === 'chat', () => levelOwn.value.join(','), samplesKnown], ([chat]) => {
  if (chat) readLevels(true)
}, { immediate: true })

function recheckClaude() {
  return sessions.loadFeatureFlags(true)
}

async function deploy() {
  deploying.value = true
  deployError.value = ''
  try {
    deployError.value = (await deployMyGuide(store, authStore.principalId)).failure
    emit('retry-roster')
  } finally {
    deploying.value = false
  }
}

// A Show me walk opens in another tab, and the person changes the setting there. Coming
// back, the guide must see what is set now, so the page rewrites its fleet file
// (`fleetRefreshDue` says when).
let walkOpenedAt = 0
let fleetReturning = null
function onWalkOpened() { walkOpenedAt = Date.now() }

// Show me teaches the route, and the route starts here: a person's first Show me lights
// the Dashboard link in the sidebar, and its button opens the walk in the new tab. After
// that (or with no sidebar on screen) the card's own link opens it.
const DOOR_STEPS = walkDoorSteps()
const door = ref(null)
let doorDropped = false
function doorSeen() {
  try { return localStorage.getItem(WALK_DOOR_SEEN) === '1' } catch { return true }
}
function onShowMe(event, link) {
  const box = document.querySelector('[data-tour="dashboard"]')?.getBoundingClientRect?.()
  const linkVisible = Boolean(box && box.width > 0 && box.height > 0)
  if (link && event && walkDoorDue({ seen: doorSeen() || doorDropped, linkVisible, busy: tourOpen.value || Boolean(moment.value) })) {
    event.preventDefault()
    door.value = { link }
    return
  }
  onWalkOpened()
  closeCard()
}
function closeDoor(reason) {
  const opened = door.value
  door.value = null
  if (!opened) return
  // Nothing to light after all: the next press opens the walk as before.
  if (reason === 'dropped') { doorDropped = true; return }
  if (reason !== 'done') return
  try { localStorage.setItem(WALK_DOOR_SEEN, '1') } catch { /* private mode: shown again next time */ }
  window.open(opened.link, '_blank', 'noopener')
  onWalkOpened()
  closeCard()
}
function refreshFleetOnReturn() {
  // Focus and visibilitychange arrive together: one read for both.
  if (phase.value !== 'chat' || fleetReturning) return
  if (!fleetRefreshDue({ now: Date.now(), readAt: fleetReadAt, walkOpenedAt })) return
  fleetReturning = refreshFleet().finally(() => { fleetReturning = null })
}

function onWindowFocus() {
  // Coming back from connecting Claude in the other tab.
  if (phase.value === 'connect') recheckClaude()
  // Or from setting something up elsewhere, which may have reached a level.
  readLevels(true)
  refreshFleetOnReturn()
}

onMounted(() => {
  sessions.loadFeatureFlags()
  if (!firstRunStore.loaded && !firstRunStore.loading) firstRunStore.fetchState()
  window.addEventListener('focus', onWindowFocus)
  document.addEventListener('visibilitychange', onPageVisible)
  if (phase.value === 'intake') emit('need-onboarding')
  refresh()
  checkGuideGrant()
  schedule()
})
watch(guide, (now, before) => {
  if (now && !before) {
    refresh()
    checkGuideGrant()
  }
})
watch([() => phase.value === 'chat', () => ownAgents.value.map((a) => a.name).join(',')], ([chat]) => {
  if (chat) refreshFleet()
}, { immediate: true })
loadCompanyProfile().finally(() => { profileRead.value = true })

// A guide that just became theirs (their own after a shared one, or set up again)
// hasn't been told their answers, and its own research hasn't run.
watch(() => guide.value?.name, (name) => {
  const next = rebindToGuide(props.intake, name)
  if (next !== props.intake) emit('update:intake', next)
}, { immediate: true })

// An agent the guide reports as built that the roster doesn't have yet: read the
// roster again, and again every 15 seconds while it is still missing.
let rosterAskedFor = ''
let rosterAskedAt = 0
function askForMissingAgents() {
  const missing = (state.value?.agents || []).map((a) => a.live_name).filter((n) => n && !props.roster.some((r) => r?.name === n)).join(',')
  if (!missing || (missing === rosterAskedFor && Date.now() - rosterAskedAt < 15000)) return
  rosterAskedFor = missing
  rosterAskedAt = Date.now()
  emit('retry-roster')
}
watch(() => (state.value?.agents || []).map((a) => a.live_name).join(','), askForMissingAgents, { immediate: true })
onBeforeUnmount(() => {
  clearTimeout(timer)
  clearTimeout(readyTimer)
  window.removeEventListener('focus', onWindowFocus)
  document.removeEventListener('visibilitychange', onPageVisible)
})
</script>
