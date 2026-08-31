<template>
  <div class="h-screen flex flex-col bg-white dark:bg-gray-900 text-gray-900 dark:text-gray-100 overflow-hidden">
    <!-- ============================ SIGNING OUT ========================== -->
    <!-- #2258: holds the frame while the platform credential is being revoked.
         Without it, a platform user sees the OTP form — "enter the email an
         operator shared agents with" — for the beat between `logout()` and
         the /login push: the exact confusion ent#357 removed. Same footprint
         as the sign-in card (contract #4). -->
    <div v-if="signingOut" class="flex-1 flex items-center justify-center px-4" aria-live="polite">
      <p class="text-sm text-gray-500 dark:text-gray-400">Signing out…</p>
    </div>

    <!-- ============================ SIGN-IN ============================ -->
    <div v-else-if="!store.isClientSignedIn" class="flex-1 flex items-center justify-center px-4">
      <div class="w-full max-w-sm">
        <div class="flex items-center gap-2 mb-6">
          <svg class="w-7 h-7 text-action-primary-600" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z" /></svg>
          <span class="font-semibold text-lg">Workspace</span>
        </div>

        <!-- #2261 — the store has set `sessionExpired` since ent#375 and nothing
             ever rendered it, so an idle-out was indistinguishable from "you
             were never signed in": the form just reappeared. Say which it was. -->
        <div
          v-if="store.sessionExpired"
          data-testid="workspace-session-expired"
          class="mb-5 rounded-md border border-amber-200 dark:border-amber-800 bg-amber-50 dark:bg-amber-900/25 px-3 py-2"
        >
          <p class="text-sm text-amber-800 dark:text-amber-200">
            Your session timed out. Sign in again to pick up where you left off.
          </p>
        </div>

        <!-- #2261 — the operator's way back in. The suppression that stops a
             client's expiry from silently becoming the operator's session has to
             fail closed, which also catches the operator when the browser is
             genuinely theirs. One explicit click, never an automatic re-derive. -->
        <div
          v-if="canContinueAsOperator"
          data-testid="workspace-continue-as-operator"
          class="mb-5 rounded-md border border-gray-200 dark:border-gray-700 px-3 py-3"
        >
          <p class="text-sm text-gray-600 dark:text-gray-300">
            You're signed in to Trinity as <span class="font-medium">{{ operatorEmail }}</span> in this browser.
          </p>
          <button
            type="button"
            class="mt-2 text-sm font-medium text-action-primary-600 hover:text-action-primary-700 underline"
            @click="continueAsOperator"
          >Continue as {{ operatorEmail }}</button>
        </div>

        <template v-if="step === 'email'">
          <h1 class="text-xl font-semibold mb-1">Sign in to your agents</h1>
          <p class="text-sm text-gray-500 dark:text-gray-400 mb-6">
            Enter the email an operator shared agents with — we'll send a 6-digit code.
          </p>
          <form @submit.prevent="onRequest" class="space-y-3">
            <input
              v-model="email"
              type="email"
              required
              placeholder="you@example.com"
              class="w-full rounded-xl border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900 px-3.5 py-2.5 text-sm focus:ring-2 focus:ring-action-primary-500/40 focus:border-action-primary-500 focus:outline-none"
            />
            <button
              type="submit"
              :disabled="busy || !email"
              class="w-full rounded-xl bg-action-primary-600 hover:bg-action-primary-700 text-white text-sm font-medium px-4 py-2.5 disabled:opacity-50"
            >{{ busy ? 'Sending…' : 'Send code' }}</button>
          </form>
        </template>

        <template v-else>
          <h1 class="text-xl font-semibold mb-1">Enter your code</h1>
          <p class="text-sm text-gray-500 dark:text-gray-400 mb-6">
            If <span class="font-medium text-gray-700 dark:text-gray-300">{{ email }}</span> has access, a 6-digit code is on its way.
          </p>
          <PortalCodeInput ref="codeInput" v-model="code" @complete="onVerify" />
          <div class="mt-3 flex items-center justify-between text-xs">
            <button class="text-gray-400 hover:text-gray-600" @click="backToEmail">← different email</button>
            <button
              :disabled="resendIn > 0 || busy"
              class="text-action-primary-600 dark:text-action-primary-400 hover:underline disabled:opacity-50 disabled:no-underline"
              @click="onResend"
            >{{ resendIn > 0 ? `Resend in ${resendIn}s` : 'Resend code' }}</button>
          </div>
          <p class="mt-2 text-xs text-gray-400">Codes expire after a few minutes.</p>
          <button
            type="button"
            :disabled="busy || code.length < 6"
            class="mt-4 w-full rounded-xl bg-action-primary-600 hover:bg-action-primary-700 text-white text-sm font-medium px-4 py-2.5 disabled:opacity-50"
            @click="onVerify"
          >{{ busy ? 'Verifying…' : 'Verify & continue' }}</button>
        </template>

        <p v-if="error" class="mt-3 text-sm text-status-danger-600 dark:text-status-danger-400">{{ error }}</p>
      </div>
    </div>

    <!-- ============================ APP SHELL ============================ -->
    <div v-else class="flex-1 flex min-h-0">
      <!-- Sidebar: persistent on desktop, drawer on mobile -->
      <div class="hidden sm:flex shrink-0">
        <PortalSidebar
          :roster="store.agents"
          :threads="threads"
          :client-email="store.clientEmail"
          :current-session-id="activeSessionId"
          :current-room-id="activeRoomIdFromRoute"
          :is-platform-session="store.isPlatformSession"
          :loading-roster="store.loading && !store.rosterLoaded"
          v-model:search="search"
          :searching="searching"
          :search-results="searchResults"
          @new-chat="newChat"
          @new-chat-with-agent="newChatWithAgent"
          @open-agent="openAgentPage"
          @open-thread="openThread"
          @toggle-star="toggleStar"
          @sign-out="onSignOut"
        />
      </div>
      <div v-if="mobileNav" class="sm:hidden fixed inset-0 z-40">
        <div class="absolute inset-0 bg-black/40" @click="mobileNav = false"></div>
        <div class="absolute inset-y-0 left-0">
          <PortalSidebar
            :roster="store.agents"
            :threads="threads"
            :client-email="store.clientEmail"
            :current-session-id="activeSessionId"
            :current-room-id="activeRoomIdFromRoute"
            :is-platform-session="store.isPlatformSession"
            :loading-roster="store.loading && !store.rosterLoaded"
            v-model:search="search"
            :searching="searching"
            :search-results="searchResults"
            @new-chat="() => { mobileNav = false; newChat() }"
            @new-chat-with-agent="(n) => { mobileNav = false; newChatWithAgent(n) }"
            @open-agent="(n) => { mobileNav = false; openAgentPage(n) }"
            @open-thread="(t) => { mobileNav = false; openThread(t) }"
            @toggle-star="toggleStar"
            @sign-out="onSignOut"
          />
        </div>
      </div>

      <!-- Main stage -->
      <main class="flex-1 min-w-0 flex flex-col bg-white dark:bg-gray-900">
        <!-- ent#361: a room takes the stage when the URL names one. The
             single-agent conversation is untouched below — different
             substrate, different component, no shared state. -->
        <!-- ent#360: an agent is a destination with its own URL. Takes the
             stage ahead of room/chat, since a route can only name one. -->
        <PortalAgentPage
          v-if="activeAgentPageName"
          :key="activeAgentPageName"
          :agent-name="activeAgentPageName"
          :threads="threads"
          @start-chat="onStartChatFromPage"
          @open-thread="openThread"
          @open-menu="mobileNav = true"
        />

        <PortalRoom
          v-else-if="activeRoomIdFromRoute && store.multiAgentChatAvailable"
          :key="activeRoomIdFromRoute"
          :room-id="activeRoomIdFromRoute"
          :roster="store.agents"
          :starred="isStarred('room', activeRoomIdFromRoute)"
          @open-menu="mobileNav = true"
          @rooms-changed="refreshThreads"
          @toggle-star="toggleStar"
        />

        <!-- #2128: the URL names a room this instance cannot open. This branch
             must catch EVERY remaining room-URL case, and its position between
             the two components above and below is load-bearing: falling through
             to PortalConversation opens a DIFFERENT agent's chat under a room
             link (activeAgent defaults to the first roster entry), and falling
             past that lands on the `!activeRoomIdFromRoute` block whose guard is
             false here — rendering a completely blank <main>.

             Gating the RENDER, not just a watcher, is also what stops
             PortalRoom::onMounted issuing GET /api/rooms/:id at all.

             Four sub-states, not one: fail-closed is right for the affordance
             and wrong for the copy that explains it. Only a roster that loaded
             CLEANLY and reported the capability absent may say "not available
             on this instance" — saying it during a transient 5xx on an entitled
             instance would be a false statement about the operator's build, on
             the one surface whose whole bar is honest status. -->
        <div v-else-if="activeRoomIdFromRoute" :class="STAGE_WRAP">
          <svg :class="STAGE_ICON" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z" /></svg>
          <template v-if="!store.rosterLoaded || store.loading">
            <p :class="STAGE_BODY_LEAD">Opening this conversation…</p>
          </template>
          <template v-else-if="store.unavailable">
            <p :class="STAGE_TITLE">{{ WORKSPACE_UNAVAILABLE_TITLE }}</p>
            <p :class="STAGE_BODY">
              It isn't enabled here. Ask an administrator if you expected access.
            </p>
          </template>
          <template v-else-if="store.error">
            <p :class="STAGE_TITLE">{{ ROSTER_LOAD_FAILED_TITLE }}</p>
            <p :class="STAGE_BODY">{{ store.error }}</p>
            <button :class="STAGE_ACTION" @click="store.fetchRoster()">Try again</button>
          </template>
          <template v-else>
            <p :class="STAGE_TITLE">This conversation isn't available on this instance</p>
            <p :class="STAGE_BODY">
              Chats with more than one agent aren't enabled here. Start a chat with a single agent instead.
            </p>
            <button :class="STAGE_ACTION" @click="leaveRoomRoute">Start a new chat</button>
          </template>
        </div>

        <PortalConversation
          v-else-if="activeAgent"
          :key="convKey"
          :agent="activeAgent"
          :roster="store.agents"
          :session-id="pendingSession"
          :new-chat="startingNewChat"
          :prefill="prefill"
          :starred="isStarred('thread', activeSessionId || pendingSession)"
          @switch-agent="switchAgent"
          @session-adopted="onSessionAdopted"
          @sessions-changed="onConversationTurnDone"
          @open-files="filesOpen = true"
          @open-menu="mobileNav = true"
          @escalate-to-room="onEscalateToRoom"
          @toggle-star="toggleStar"
          @open-thread="openThread"
        >
          <template #empty>
            <PortalBriefing :agent="activeAgent" @use-playbook="usePlaybook" />
          </template>
        </PortalConversation>

        <!-- ent#357 AC: an unavailable workspace must SAY so. These three
             states used to collapse into one "No agents shared with you yet",
             which reads as "your operator hasn't shared anything" whether the
             module is absent, the roster call failed, or the list is genuinely
             empty — a dead end in two of the three cases. -->
        <div v-else-if="unreachableAgent" :class="STAGE_WRAP">
          <svg :class="STAGE_ICON" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M18.364 18.364A9 9 0 005.636 5.636m12.728 12.728A9 9 0 015.636 5.636m12.728 12.728L5.636 5.636" /></svg>
          <p :class="STAGE_TITLE">
            You don't have access to <span class="font-mono">{{ unreachableAgent }}</span>
          </p>
          <p :class="STAGE_BODY">
            That link points at an agent that isn't shared with you. Pick one from the sidebar, or ask whoever sent the link.
          </p>
        </div>

        <div v-else-if="!activeRoomIdFromRoute && !activeAgentPageName" :class="STAGE_WRAP">
          <svg :class="STAGE_ICON" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z" /></svg>
          <template v-if="store.unavailable">
            <p :class="STAGE_TITLE">{{ WORKSPACE_UNAVAILABLE_TITLE }}</p>
            <p :class="STAGE_BODY">
              It isn't enabled here. Ask an administrator if you expected access.
            </p>
          </template>
          <template v-else-if="store.error">
            <p :class="STAGE_TITLE">{{ ROSTER_LOAD_FAILED_TITLE }}</p>
            <p :class="STAGE_BODY">{{ store.error }}</p>
            <button :class="STAGE_ACTION" @click="store.fetchRoster()">Try again</button>
          </template>
          <!-- ent#357: an empty roster needs a next step, not just a statement.
               The two audiences need different ones: a signed-in user can go
               make an agent, an external client can only ask the person who
               invited them. -->
          <template v-else-if="store.isPlatformSession">
            <p :class="STAGE_TITLE">No agents here yet</p>
            <p :class="STAGE_BODY">
              Agents you own, and agents shared with you, appear here.
            </p>
            <a href="/" :class="STAGE_ACTION">Go to your agents</a>
          </template>
          <template v-else>
            <p :class="STAGE_TITLE">No agents shared with you yet</p>
            <p :class="STAGE_BODY">
              Ask whoever invited you to share an agent with
              <span class="font-medium">{{ store.clientEmail || 'your email' }}</span>.
            </p>
          </template>
          <button class="sm:hidden mt-4 text-sm text-action-primary-600" @click="mobileNav = true">Open menu</button>
        </div>
      </main>
    </div>

    <!-- ent#361: picking who is in a chat is an explicit act now -->
    <PortalAgentPicker
      v-if="pickerOpen"
      :agents="store.agents"
      :multi="store.multiAgentChatAvailable"
      :busy="pickerBusy"
      :error="pickerError"
      @confirm="onPickerConfirm"
      @cancel="() => { pickerOpen = false; pickerError = null }"
    />

    <!-- Files panel -->
    <PortalFilesPanel v-if="filesOpen && activeAgent" :agent="activeAgent" @close="filesOpen = false" />
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted, onBeforeUnmount, nextTick } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useClientPortalStore, MULTI_AGENT_UNAVAILABLE, PLATFORM_LOGIN_ROUTE } from '@/stores/clientPortal'
import { useAuthStore } from '@/stores/auth'
import PortalSidebar from '@/components/portal/PortalSidebar.vue'
import PortalConversation from '@/components/portal/PortalConversation.vue'
import PortalBriefing from '@/components/portal/PortalBriefing.vue'
import PortalFilesPanel from '@/components/portal/PortalFilesPanel.vue'
import PortalCodeInput from '@/components/portal/PortalCodeInput.vue'
import PortalAgentPicker from '@/components/portal/PortalAgentPicker.vue'
import PortalRoom from '@/components/portal/PortalRoom.vue'
import PortalAgentPage from '@/components/portal/PortalAgentPage.vue'
import { resolveAgentLanding, shouldMarkTurnRead, shouldEscapeStage } from '@/components/portal/portalUtils'

const store = useClientPortalStore()
const authStore = useAuthStore()

// #2261 — shown only when this tab suppressed the platform fallback (a client
// session expired here) AND a platform session actually exists to continue as.
// Both terms matter: without the first, every operator would be asked to
// re-confirm on a normal visit; without the second, the button would offer an
// identity that isn't there.
const canContinueAsOperator = computed(
  () => store.platformFallbackSuppressed && authStore.isAuthenticated
)
const operatorEmail = computed(() => authStore.userEmail || 'your Trinity account')

function continueAsOperator() {
  store.continueAsPlatform()
  // The roster is fetched by the same bootstrap the implicit-entry path uses;
  // clearing the suppression flips `isClientSignedIn`, and the watcher below
  // takes it from there.
  bootstrap()
}
const route = useRoute()
const router = useRouter()

// ---- Sign-in ------------------------------------------------------------------
const step = ref('email')
const email = ref('')
const code = ref('')
const busy = ref(false)
const error = ref(null)
const codeInput = ref(null)
const resendIn = ref(0)
let resendTimer = null

function startResendCooldown() {
  resendIn.value = 30
  clearInterval(resendTimer)
  resendTimer = setInterval(() => { if (--resendIn.value <= 0) clearInterval(resendTimer) }, 1000)
}
async function onRequest() {
  busy.value = true; error.value = null
  try {
    await store.requestCode(email.value.trim().toLowerCase())
    step.value = 'code'; code.value = ''
    startResendCooldown()
    await nextTick(); codeInput.value?.focusFirst()
  } catch (err) { error.value = err.response?.data?.detail || 'Could not send a code. Try again.' }
  finally { busy.value = false }
}
async function onResend() {
  if (resendIn.value > 0) return
  await onRequest()
}
function backToEmail() { step.value = 'email'; code.value = ''; error.value = null }
async function onVerify() {
  if (code.value.length < 6 || busy.value) return
  busy.value = true; error.value = null
  try {
    // #2261 — read the resume target BEFORE the roster load, since the sign-in
    // that consumed the expiry is what makes it spendable. `endSession` recorded
    // where the client was when their session lapsed, and until now nothing ever
    // read it back: the expired notice promises "pick up where you left off", so
    // it has to actually land there rather than on the roster root.
    const resumeTo = store.resumePath
    await store.verifyCode(email.value.trim().toLowerCase(), code.value.trim())
    await bootstrap()
    if (resumeTo && resumeTo !== route.fullPath) {
      store.resumePath = null
      router.push(resumeTo)
    }
  } catch (err) {
    error.value = err.response?.status === 401 ? 'Invalid or expired code.' : (err.response?.data?.detail || 'Verification failed.')
    code.value = ''
    await nextTick(); codeInput.value?.focusFirst()
  } finally { busy.value = false }
}

// ---- Shell state --------------------------------------------------------------
const threads = ref([])
const activeAgentName = ref(null)
// ent#361: the room a multi-agent chat is being held in, if any.
const activeRoomId = ref(null)
// The agent a deep link named that this caller cannot reach (ent#358 review).
// Set when a deep link names an agent this caller cannot reach. Cleared by
// every navigation away from it — `activeAgent` returns null while it is set,
// so a latch that never clears leaves the whole Workspace stuck on the
// access-denied panel for the rest of the SPA session.
const unreachableAgent = ref(null)
const pendingSession = ref(null)      // session to load when the conversation (re)mounts
const prefill = ref('')
const filesOpen = ref(false)
const mobileNav = ref(false)
const convGen = ref(0)                // bumps on explicit thread switches → remount

const activeSessionId = computed(() => route.params.sessionId || null)
// ent#361: `/workspace/r/:roomId` is the multi-agent chat.
const activeRoomIdFromRoute = computed(() => route.params.roomId || null)
// ent#360: `/workspace/a/:agentName`.
const activeAgentPageName = computed(() => route.params.agentName || null)
const activeAgent = computed(() => {
  // Never substitute a different agent for one the caller asked for by name.
  if (unreachableAgent.value) return null
  if (!activeAgentName.value) return store.agents[0] || null
  return store.agents.find((a) => a.name === activeAgentName.value) || { name: activeAgentName.value }
})
// Remount the conversation on agent/thread switches, but NOT when a session-less
// first turn adopts an id (that just updates the route in place).
const convKey = computed(() => `${activeAgentName.value || (store.agents[0]?.name) || ''}#${convGen.value}`)

// ---- Navigation handlers ------------------------------------------------------
// ent#361: "+ New chat" is now an explicit act — pick who is in it. The old
// behaviour (reset to a blank single-agent thread) is what the picker's
// one-agent path still does, so nothing is lost, it is just no longer implicit.
const pickerOpen = ref(false)
const pickerBusy = ref(false)

// ent#451 — the second bit beside `pendingSession`. Cleared the moment a real
// thread exists (`openThread`, and the send that gets a session id back), so it
// can never make a SECOND turn open another thread.
const startingNewChat = ref(false)

function newChat() {
  pickerOpen.value = true
}

// #2128 — every exit from the main stage tested ONLY `sessionId`, so on a
// /workspace/r/:id URL none of them changed the route. That was invisible while
// the room always rendered; the moment a room URL can resolve to a refusal, it
// makes that refusal a state the user cannot leave by any control except the
// one on the refusal itself — a dead end created by the very fix meant to
// remove one. `roomId` belongs in the same test for the same reason.
function leaveRoomRoute() {
  activeRoomId.value = null
  pendingSession.value = null; prefill.value = ''; convGen.value++
  router.push('/workspace')
}

// The one way to hand the stage back. #2158 reached the same conclusion
// concurrently and inlined `route.path !== '/workspace'` at all three sites;
// this keeps that rule and moves it behind a name, for two reasons the inline
// form cannot cover:
//
//   * it is a PURE FUNCTION in portalUtils, so it is testable — this project has
//     no component-mount harness, and an inline closure over `route` can only be
//     pinned by scanning the source for a spelling;
//   * the QUERY is part of the stage too. `?agent=` is the ent#358 landing spot,
//     re-read by `bootstrap()` after every sign-in, so `/workspace?agent=X`
//     satisfies the path check while still carrying X into the next session.
//
// `startBlankChat` used to live here and is deleted rather than converted: ent#361
// (8e5157f1) renamed it out of the `@new-chat` binding and handed that event to
// the picker, so it has had ZERO callers since — #2158 converted an orphan.
function escapeStage() {
  if (shouldEscapeStage(route.path, route.query)) router.push('/workspace')
}

async function onPickerConfirm(agentNames) {
  if (!agentNames.length) return
  // ONE agent stays a portal thread: that path resumes, streams and reattaches
  // (ent#358/#286). TWO OR MORE needs a room — the only substrate that models
  // several agents and @mention-waking.
  if (agentNames.length === 1) {
    pickerOpen.value = false
    newChatWithAgent(agentNames[0])
    return
  }
  pickerBusy.value = true
  try {
    const room = await store.createRoom(agentNames, `Chat with ${agentNames.join(', ')}`)
    pickerOpen.value = false
    await refreshThreads()
    openRoom(room.id || room.room_id)
  } catch (err) {
    // Keep the picker open with the reason: closing it would leave the user
    // guessing whether anything happened.
    // #2128 — the store refuses a room call on an instance with no rooms
    // substrate, and self-heals the flag on a definitive 404/403 mid-session,
    // so the picker collapses to single-select on this same tick. A typed code,
    // never message-sniffing: the generic path below must stay intact, because
    // a `true` flag does not guarantee success (the client may lack access to
    // one selected agent) and that reason still has to surface.
    pickerError.value = err?.code === 'rooms_unavailable'
      ? (err.message || MULTI_AGENT_UNAVAILABLE)
      : (err?.response?.data?.detail?.message
        || err?.response?.data?.detail
        || 'Could not start that chat.')
  } finally {
    pickerBusy.value = false
  }
}

const pickerError = ref(null)

// ent#361: a 1:1 became a group discussion. Create the room with both agents,
// carry the message that caused it, and move the user there.
//
// The message is posted AFTER navigation rather than before: posting first and
// then navigating leaves the user staring at the old thread while the agents
// they just summoned reply somewhere they cannot see. If the post fails the
// room still exists and they are in it, which retyping recovers — whereas a
// created-but-unreachable room does not.
const escalating = ref(false)

async function onEscalateToRoom({ agents, message } = {}) {
  if (escalating.value || !agents?.length) return
  escalating.value = true
  try {
    const room = await store.createRoom(agents, `Chat with ${agents.join(', ')}`)
    const roomId = room.id || room.room_id
    await refreshThreads()
    openRoom(roomId)
    if (message) {
      try {
        await store.postRoomMessage(roomId, message)
      } catch { /* the room is open in front of them; retyping recovers */ }
    }
  } catch (err) {
    // Escalation failed, so the user is still in the 1:1 with an emptied
    // composer. Give the text back rather than losing what they typed.
    prefill.value = ''
    await nextTick()
    prefill.value = message || ''
    pickerError.value = err?.response?.data?.detail?.message
      || err?.response?.data?.detail
      || 'Could not start a group chat with those agents.'
  } finally {
    escalating.value = false
  }
}

// #2128 — shared by the room-route refusal branch and the no-room empty state
// below it, which are the same four states rendered in the same file. Local
// consts, not a module: both consumers live here, and a component extraction
// for two <p> pairs is more abstraction than the duplication costs.
const WORKSPACE_UNAVAILABLE_TITLE = "Workspace isn't available on this instance"
const ROSTER_LOAD_FAILED_TITLE = "Couldn't load your agents"

// #2128 — the ink for the three empty/refusal stages, written ONCE.
//
// This file renders nine of them (a room URL this instance can't open ×4, an
// unreachable agent, an empty roster ×4) and every one used to carry its own
// copy of the same class strings. That is design-system principle 4 —
// loading/loaded/empty/failed share one footprint — held together by
// copy-paste, and adding a state grew the file's raw-palette count by four
// every time (the ratchet in CLAUDE.md §9 says per-file counts may only
// shrink, and this branch had pushed 40 → 56).
//
// Hoisting is the whole remedy available here, and it is worth being precise
// about why: `gray` has NO semantic token. It is the design system's residual
// family — the contract's own colour section ends "Everything else is gray"
// and prescribes these exact shades for the ink ladder — so there is nothing
// to convert `text-gray-500 dark:text-gray-400` INTO. Repainting body copy in
// `status-*` or `action-*` would be a defect, not compliance. What can be
// fixed is the number of PLACES a raw shade is written, which is what makes a
// future migration tractable, and that drops from twenty-odd to five.
const STAGE_WRAP = 'flex-1 flex flex-col items-center justify-center text-center px-6'
const STAGE_ICON = 'w-10 h-10 text-gray-300 dark:text-gray-700 mb-3'
const STAGE_TITLE = 'text-sm text-gray-700 dark:text-gray-300 font-medium'
const STAGE_BODY = 'mt-1 text-xs text-gray-500 dark:text-gray-400 max-w-xs'
// The neutral "still resolving" line stands alone (no title above it), so it
// carries the body ink without the top margin the paired form needs.
const STAGE_BODY_LEAD = 'text-sm text-gray-500 dark:text-gray-400'
const STAGE_ACTION = 'mt-3 text-sm text-action-primary-600 hover:underline'

function openRoom(roomId) {
  if (!roomId) return
  unreachableAgent.value = null
  markRead('room', roomId)
  activeRoomId.value = roomId
  pendingSession.value = null
  // ent#451 review: every site that nulls `pendingSession` also settles the
  // intent. Latent today because both consumers AND on "no session yet", but a
  // flag whose meaning depends on a second variable is one refactor from being
  // wrong, and the declaration at :441 claims it is cleared the moment a real
  // thread exists.
  startingNewChat.value = false
  convGen.value++
  router.push(`/workspace/r/${roomId}`)
}
// ent#360 AC #1: a roster row opens the agent's PAGE. Starting a chat is an
// explicit act there — which also resolves the tension ent#359 left behind,
// where a row carrying an unread badge opened the unread chat instead. The
// count still shows on the row; the page's Overview lists the chats it belongs
// to, so the conversation is one click further, not lost.
function openAgentPage(name) {
  if (!name) return
  unreachableAgent.value = null
  pendingSession.value = null
  startingNewChat.value = false
  activeRoomId.value = null
  router.push(`/workspace/a/${encodeURIComponent(name)}`)
}

// "Start a chat" from the page, optionally seeded by a capability card.
function onStartChatFromPage(name, starter) {
  newChatWithAgent(name)
  if (starter) usePlaybook(starter)
}

function newChatWithAgent(name) {
  unreachableAgent.value = null
  activeAgentName.value = name
  // ent#451: this function has always MEANT a fresh chat — it clears
  // `pendingSession` — but a null session id is also what an unresolved thread
  // looks like, so the conversation could not tell the two apart and loaded the
  // agent's most recent thread instead. Saying it explicitly is the fix.
  startingNewChat.value = true
  pendingSession.value = null; prefill.value = ''; convGen.value++
  // Leave ANY specific route, not an enumerated list of params.
  //
  // This condition was wrong twice for the same reason. #2128 added `roomId`
  // after picking an agent while parked on a room URL left the room on screen;
  // ent#360 then added `/workspace/a/:agentName` and did not extend the list, so
  // "Start a chat" on the agent page set the state and navigated nowhere — the
  // page kept rendering (it is the first branch of the stage chain) and the chat
  // never appeared. #2158 fixed that by asking about route shape; `escapeStage`
  // keeps that rule, names it, and extends it to the query.
  escapeStage()
}
function switchAgent(name) { newChatWithAgent(name) }   // mid-thread = plain new chat, no carry-over
function openThread(t) {
  unreachableAgent.value = null
  // Opening an existing thread is the opposite intent; clear it so a later
  // send does not still ask for a fresh one.
  startingNewChat.value = false
  // ent#361: a room row in the merged sidebar opens the room, not a thread.
  if (t.is_room) { openRoom(t.id); return }
  const sid = t.id || t.session_id
  markRead('thread', sid)
  activeAgentName.value = t.agent_name || activeAgentName.value
  pendingSession.value = sid; prefill.value = ''; convGen.value++
  search.value = ''
  router.push(`/workspace/c/${sid}`)
}
function onSessionAdopted(id) {
  pendingSession.value = id
  // ent#451: a real thread exists now, so the fresh-start intent is spent.
  // The send guard already ANDs on "no session yet", so a second turn was never
  // going to open a third thread — this keeps the two bits from disagreeing
  // rather than relying on that, and matters when the user navigates away and
  // back to a thread this flag would otherwise still describe as unborn.
  startingNewChat.value = false
  if (route.params.sessionId !== id) router.replace(`/workspace/c/${id}`)
  // A thread you are actively talking in is by definition read. This is also
  // what gives a brand-new thread its read cursor, so the very next reply the
  // user does NOT see is the first thing that badges.
  markRead('thread', id).then(refreshThreads)
}
function usePlaybook(text) { prefill.value = ''; nextTick(() => { prefill.value = text }) }

// ent#359 — per-viewer star + unread state, merged onto the thread list.
//
// Kept a separate call from `fetchAllSessions` on purpose, so that a chat-state
// failure costs the stars and badges, never the list. (#2198: the original
// wording — "that call fans out over the roster and degrades per agent, while
// this is one call for the whole viewer" — described the shape sessions should
// have had; sessions is now one viewer-scoped call too.)
const chatState = ref({})
const chatKey = (t) => `${t.is_room ? 'room' : 'thread'}:${t.id || t.session_id}`

const isStarred = (kind, id) => !!(id && chatState.value[`${kind}:${id}`]?.starred)

function decorate(list) {
  return list.map((t) => {
    const s = chatState.value[chatKey(t)]
    return { ...t, starred: !!s?.starred, unread: Number(s?.unread) || 0 }
  })
}

async function refreshThreads() {
  // #2198: both halves are caught. `fetchAllSessions` already returns its last
  // good list rather than rejecting, but this is a belt on the load-bearing
  // property: `bootstrap()` AWAITS this before `resolveAgentQuery()` and the
  // deep-link `sessionId` branch, so an unhandled rejection here would not
  // merely empty the sidebar — it would break Workspace deep-link landing
  // outright, on the most client-visible surface in the product. Before the
  // batch this was structurally impossible (each per-agent call had its own
  // catch); with one request it is one 500 away, so it is made explicit.
  const [list, state] = await Promise.all([
    store.fetchAllSessions().catch(() => store.lastSessions),
    store.fetchChatState().catch(() => chatState.value),
  ])
  chatState.value = state || {}
  threads.value = decorate(list || [])
}

// A turn finishing in the conversation the user is LOOKING AT is read by
// definition. Without this, the reply the user just watched arrive would land
// after the read cursor set at dispatch and badge the chat they are sitting in
// — a notification for something they are actively reading.
function onConversationTurnDone(sessionId) {
  // Only if the user is STILL in that thread. The conversation's send is an
  // async closure that outlives the component, so this fires even when they
  // have navigated away mid-turn — which is the main way a reply legitimately
  // arrives unseen. Marking read unconditionally cleared exactly the badge the
  // feature exists to show, and made it near-unreachable in normal use.
  const open = activeSessionId.value || pendingSession.value
  return (shouldMarkTurnRead(sessionId, open)
    ? markRead('thread', sessionId)
    : Promise.resolve()
  ).then(refreshThreads)
}

// Optimistic: a star is a personal bookmark, and waiting on a round trip to
// redraw it makes the control feel broken. Reverted in place on failure so the
// list never claims a star the server rejected.
async function toggleStar(t) {
  const key = chatKey(t)
  const next = !t.starred
  const before = chatState.value[key]
  chatState.value = {
    ...chatState.value,
    [key]: { ...(before || { kind: t.is_room ? 'room' : 'thread', id: t.id }), starred: next },
  }
  threads.value = decorate(threads.value)
  try {
    await store.setChatStar(t.is_room ? 'room' : 'thread', t.id || t.session_id, next)
  } catch {
    const reverted = { ...chatState.value }
    if (before) reverted[key] = before
    else delete reverted[key]
    chatState.value = reverted
    threads.value = decorate(threads.value)
  }
}

// Opening a chat is what "reading" it means here. Clear the badge locally first
// so the count does not linger for a round trip, then persist.
// Returns the write promise. Callers that refresh afterwards MUST await it:
// `GET /chat-state` racing the cursor UPSERT overwrites the optimistic zero
// with a stale count, and the badge comes back on the conversation the user is
// reading — possibly for minutes, until the next refresh.
function markRead(kind, id) {
  if (!id) return Promise.resolve()
  const key = `${kind}:${id}`
  if (chatState.value[key]?.unread) {
    chatState.value = { ...chatState.value, [key]: { ...chatState.value[key], unread: 0 } }
    threads.value = decorate(threads.value)
  }
  return store.markChatRead(kind, id)
}

// ---- Cross-chat search (sidebar) ----------------------------------------------
const search = ref('')
const searchResults = ref([])
const searching = ref(false)
let searchTimer = null
watch(search, (q) => {
  clearTimeout(searchTimer)
  if ((q || '').trim().length < 2) { searchResults.value = []; searching.value = false; return }
  searching.value = true
  searchTimer = setTimeout(async () => {
    try { searchResults.value = await store.searchChats(q.trim()) } catch { searchResults.value = [] }
    finally { searching.value = false }
  }, 250)
})

// ---- Deep-link / refresh resolution -------------------------------------------
// On a /workspace/c/:id load (or refresh), resolve which agent that thread belongs
// to from the merged thread list so the shell opens the right conversation.
watch([() => route.params.sessionId, () => threads.value.length], () => {
  const sid = route.params.sessionId
  if (!sid || !store.isClientSignedIn) return
  if (pendingSession.value === sid && activeAgentName.value) return
  const known = threads.value.find((t) => (t.id || t.session_id) === sid)
  // ent#451 review: this is "the commonest way in — back/forward, a bookmark
  // and a reload" (below), and it adopts a REAL thread, so any pending
  // fresh-start intent is spent here too.
  if (known) {
    activeAgentName.value = known.agent_name
    pendingSession.value = sid
    startingNewChat.value = false
    convGen.value++
  }
  // Opening by ROUTE is an open. Back/forward, a bookmark and a reload all land
  // here rather than in `openThread`, and it is the commonest way in — without
  // this the sidebar badges the conversation on screen, through every reload.
  markRead('thread', sid)
})

// ent#358: `/workspace?agent=<name>` opens that agent's conversation directly —
// the landing spot for anything that used to point at the Agent Detail Session
// surface. The decision itself (which agent, which thread) is a pure function in
// portalUtils so it can be tested without mounting the shell.
function resolveAgentQuery() {
  // ONE local, read twice: `resolveAgentLanding` decides which thread to land
  // on, and `startingNewChat` decides what the first SEND asks for. Reading
  // `route.query.new` separately in each place is how they drifted — the
  // landing honoured `?new=1` and the send did not, so the deep link rendered
  // an empty conversation and then resumed the old thread on the first turn.
  const forceNew = !!route.query.new
  const landing = resolveAgentLanding({
    agent: route.query.agent,
    forceNew,
    agents: store.agents,
    threads: threads.value,
  })
  if (!landing) {
    // The link named an agent this caller cannot reach (un-shared since the URL
    // was created, renamed, deleted). Falling through used to let `activeAgent`
    // default to the FIRST agent on the roster, so the user landed in someone
    // else's conversation believing it was the one they clicked — and could
    // send it context meant for another agent. Say so instead.
    if (route.query.agent) {
      unreachableAgent.value = String(route.query.agent)
      activeAgentName.value = null
      pendingSession.value = null
      startingNewChat.value = false
      convGen.value++
      return true
    }
    return false
  }
  unreachableAgent.value = null

  activeAgentName.value = landing.agentName
  prefill.value = ''
  convGen.value++
  pendingSession.value = landing.sessionId
  // `landing.sessionId` is null under `forceNew`, but null alone is exactly the
  // ambiguity ent#451 exists to remove — it also means "unresolved". AND-ed
  // with the landing so a `?new=1` that still resolved a thread (it cannot
  // today, but the two are independent functions) never claims a fresh start.
  startingNewChat.value = forceNew && !landing.sessionId
  if (landing.sessionId) router.replace(`/workspace/c/${landing.sessionId}`)
  return true
}

// ent#364 — one poll feeds all three ask renderings.
//
// The Workspace has no WebSocket (`operator_queue_new` is broadcast on the platform
// `/ws`, which a portal client is not on), and it already polls elsewhere, so a
// second live channel for this would be new transport for one badge. 20s: an ask is
// a human-latency decision, not a stream.
const ASKS_POLL_MS = 20000
let asksTimer = null

function startAsksPoll() {
  stopAsksPoll()
  if (!store.isClientSignedIn) return
  store.fetchAsks()
  asksTimer = setInterval(() => {
    // Visibility-aware: a backgrounded tab polls nothing. The next foreground
    // tick catches up, and an ask that arrived meanwhile is not lost — it is a
    // row, not an event.
    if (document.visibilityState === 'visible') store.fetchAsks()
  }, ASKS_POLL_MS)
}

function stopAsksPoll() {
  if (asksTimer) { clearInterval(asksTimer); asksTimer = null }
}

async function bootstrap() {
  await store.fetchRoster()
  await refreshThreads()
  startAsksPoll()
  const sid = route.params.sessionId
  if (sid) {
    const known = threads.value.find((t) => (t.id || t.session_id) === sid)
    if (known) { activeAgentName.value = known.agent_name; pendingSession.value = sid }
    else pendingSession.value = sid   // let the conversation resolve/load it
    convGen.value++
    markRead('thread', sid)           // a deep-linked open is still an open
    return
  }
  resolveAgentQuery()
}

onMounted(async () => { if (store.isClientSignedIn) await bootstrap() })
onBeforeUnmount(() => {
  stopAsksPoll()          // ent#364 — the poll must not outlive the view
  clearInterval(resendTimer)
  clearTimeout(searchTimer)
})

// #2258: true from the click until the credential is gone and the route has
// moved. Gates the template's first branch so neither principal sees a state
// that lies about them mid-flight, and makes the handler idempotent against a
// double click while `logout()` is on the wire.
const signingOut = ref(false)

// #2258: the decision — which credential to end, in what order, and where to
// land — lives in `store.signOutEverywhere()`, where a unit test can pin it.
// This handler only resets view-local state and performs the navigation the
// store names: a platform user signed out of Trinity, so they go to the
// platform login; a client stays on the Workspace, which now shows the OTP
// form because BOTH ways of being signed in are gone.
async function onSignOut() {
  if (signingOut.value) return
  signingOut.value = true
  try {
    const target = await store.signOutEverywhere()
    threads.value = []; activeAgentName.value = null; pendingSession.value = null
    step.value = 'email'; email.value = ''; code.value = ''
    if (target === PLATFORM_LOGIN_ROUTE) {
      await router.push(PLATFORM_LOGIN_ROUTE)
      return
    }
    // Leave ANY specific route (rationale in newChatWithAgent) — otherwise a
    // sign-out carries that room id (#2128) or agent name into the next session's
    // address bar. The query matters most here: `?agent=X` survives the path
    // check and is re-read by the next `bootstrap()`, so the next person to sign
    // in on this browser lands on "You don't have access to X".
    escapeStage()
  } finally {
    signingOut.value = false
  }
}
</script>
