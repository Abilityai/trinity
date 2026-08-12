<template>
  <div class="h-screen flex flex-col bg-white dark:bg-gray-900 text-gray-900 dark:text-gray-100 overflow-hidden">
    <!-- ============================ SIGN-IN ============================ -->
    <div v-if="!store.isClientSignedIn" class="flex-1 flex items-center justify-center px-4">
      <div class="w-full max-w-sm">
        <div class="flex items-center gap-2 mb-6">
          <svg class="w-7 h-7 text-action-primary-600" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z" /></svg>
          <span class="font-semibold text-lg">Workspace</span>
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
          v-model:search="search"
          :searching="searching"
          :search-results="searchResults"
          @new-chat="newChat"
          @new-chat-with-agent="newChatWithAgent"
          @open-thread="openThread"
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
            v-model:search="search"
            :searching="searching"
            :search-results="searchResults"
            @new-chat="() => { mobileNav = false; newChat() }"
            @new-chat-with-agent="(n) => { mobileNav = false; newChatWithAgent(n) }"
            @open-thread="(t) => { mobileNav = false; openThread(t) }"
            @sign-out="onSignOut"
          />
        </div>
      </div>

      <!-- Main stage -->
      <main class="flex-1 min-w-0 flex flex-col bg-white dark:bg-gray-900">
        <!-- ent#361: a room takes the stage when the URL names one. The
             single-agent conversation is untouched below — different
             substrate, different component, no shared state. -->
        <PortalRoom
          v-if="activeRoomIdFromRoute && store.multiAgentChatAvailable"
          :key="activeRoomIdFromRoute"
          :room-id="activeRoomIdFromRoute"
          :roster="store.agents"
          @open-menu="mobileNav = true"
          @rooms-changed="refreshThreads"
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
        <div v-else-if="activeRoomIdFromRoute" class="flex-1 flex flex-col items-center justify-center text-center px-6">
          <svg class="w-10 h-10 text-gray-300 dark:text-gray-700 mb-3" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z" /></svg>
          <template v-if="!store.rosterLoaded || store.loading">
            <p class="text-sm text-gray-500 dark:text-gray-400">Opening this conversation…</p>
          </template>
          <template v-else-if="store.unavailable">
            <p class="text-sm text-gray-700 dark:text-gray-300 font-medium">{{ WORKSPACE_UNAVAILABLE_TITLE }}</p>
            <p class="mt-1 text-xs text-gray-500 dark:text-gray-400 max-w-xs">
              It isn't enabled here. Ask an administrator if you expected access.
            </p>
          </template>
          <template v-else-if="store.error">
            <p class="text-sm text-gray-700 dark:text-gray-300 font-medium">{{ ROSTER_LOAD_FAILED_TITLE }}</p>
            <p class="mt-1 text-xs text-gray-500 dark:text-gray-400 max-w-xs">{{ store.error }}</p>
            <button class="mt-3 text-sm text-action-primary-600 hover:underline" @click="store.fetchRoster()">Try again</button>
          </template>
          <template v-else>
            <p class="text-sm text-gray-700 dark:text-gray-300 font-medium">This conversation isn't available on this instance</p>
            <p class="mt-1 text-xs text-gray-500 dark:text-gray-400 max-w-xs">
              Chats with more than one agent aren't enabled here. Start a chat with a single agent instead.
            </p>
            <button class="mt-3 text-sm text-action-primary-600 hover:underline" @click="leaveRoomRoute">Start a new chat</button>
          </template>
        </div>

        <PortalConversation
          v-else-if="activeAgent"
          :key="convKey"
          :agent="activeAgent"
          :roster="store.agents"
          :session-id="pendingSession"
          :prefill="prefill"
          @switch-agent="switchAgent"
          @session-adopted="onSessionAdopted"
          @sessions-changed="refreshThreads"
          @open-files="filesOpen = true"
          @open-menu="mobileNav = true"
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
        <div v-else-if="unreachableAgent" class="flex-1 flex flex-col items-center justify-center text-center px-6">
          <svg class="w-10 h-10 text-gray-300 dark:text-gray-700 mb-3" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M18.364 18.364A9 9 0 005.636 5.636m12.728 12.728A9 9 0 015.636 5.636m12.728 12.728L5.636 5.636" /></svg>
          <p class="text-sm text-gray-700 dark:text-gray-300 font-medium">
            You don't have access to <span class="font-mono">{{ unreachableAgent }}</span>
          </p>
          <p class="mt-1 text-xs text-gray-500 dark:text-gray-400 max-w-xs">
            That link points at an agent that isn't shared with you. Pick one from the sidebar, or ask whoever sent the link.
          </p>
        </div>

        <div v-else-if="!activeRoomIdFromRoute" class="flex-1 flex flex-col items-center justify-center text-center px-6">
          <svg class="w-10 h-10 text-gray-300 dark:text-gray-700 mb-3" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z" /></svg>
          <template v-if="store.unavailable">
            <p class="text-sm text-gray-700 dark:text-gray-300 font-medium">{{ WORKSPACE_UNAVAILABLE_TITLE }}</p>
            <p class="mt-1 text-xs text-gray-500 dark:text-gray-400 max-w-xs">
              It isn't enabled here. Ask an administrator if you expected access.
            </p>
          </template>
          <template v-else-if="store.error">
            <p class="text-sm text-gray-700 dark:text-gray-300 font-medium">{{ ROSTER_LOAD_FAILED_TITLE }}</p>
            <p class="mt-1 text-xs text-gray-500 dark:text-gray-400 max-w-xs">{{ store.error }}</p>
            <button class="mt-3 text-sm text-action-primary-600 hover:underline" @click="store.fetchRoster()">Try again</button>
          </template>
          <!-- ent#357: an empty roster needs a next step, not just a statement.
               The two audiences need different ones: a signed-in user can go
               make an agent, an external client can only ask the person who
               invited them. -->
          <template v-else-if="store.isPlatformSession">
            <p class="text-sm text-gray-700 dark:text-gray-300 font-medium">No agents here yet</p>
            <p class="mt-1 text-xs text-gray-500 dark:text-gray-400 max-w-xs">
              Agents you own, and agents shared with you, appear here.
            </p>
            <a href="/" class="mt-3 text-sm text-action-primary-600 hover:underline">Go to your agents</a>
          </template>
          <template v-else>
            <p class="text-sm text-gray-700 dark:text-gray-300 font-medium">No agents shared with you yet</p>
            <p class="mt-1 text-xs text-gray-500 dark:text-gray-400 max-w-xs">
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
import { useClientPortalStore, MULTI_AGENT_UNAVAILABLE } from '@/stores/clientPortal'
import PortalSidebar from '@/components/portal/PortalSidebar.vue'
import PortalConversation from '@/components/portal/PortalConversation.vue'
import PortalBriefing from '@/components/portal/PortalBriefing.vue'
import PortalFilesPanel from '@/components/portal/PortalFilesPanel.vue'
import PortalCodeInput from '@/components/portal/PortalCodeInput.vue'
import PortalAgentPicker from '@/components/portal/PortalAgentPicker.vue'
import PortalRoom from '@/components/portal/PortalRoom.vue'
import { resolveAgentLanding } from '@/components/portal/portalUtils'

const store = useClientPortalStore()
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
    await store.verifyCode(email.value.trim().toLowerCase(), code.value.trim())
    await bootstrap()
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
const unreachableAgent = ref(null)
const pendingSession = ref(null)      // session to load when the conversation (re)mounts
const prefill = ref('')
const filesOpen = ref(false)
const mobileNav = ref(false)
const convGen = ref(0)                // bumps on explicit thread switches → remount

const activeSessionId = computed(() => route.params.sessionId || null)
// ent#361: `/workspace/r/:roomId` is the multi-agent chat.
const activeRoomIdFromRoute = computed(() => route.params.roomId || null)
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

function startBlankChat() {
  pendingSession.value = null; prefill.value = ''; convGen.value++
  if (route.params.sessionId || route.params.roomId) router.push('/workspace')
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

// #2128 — shared by the room-route refusal branch and the no-room empty state
// below it, which are the same four states rendered in the same file. Local
// consts, not a module: both consumers live here, and a component extraction
// for two <p> pairs is more abstraction than the duplication costs.
const WORKSPACE_UNAVAILABLE_TITLE = "Workspace isn't available on this instance"
const ROSTER_LOAD_FAILED_TITLE = "Couldn't load your agents"

function openRoom(roomId) {
  if (!roomId) return
  activeRoomId.value = roomId
  pendingSession.value = null
  convGen.value++
  router.push(`/workspace/r/${roomId}`)
}
function newChatWithAgent(name) {
  activeAgentName.value = name
  pendingSession.value = null; prefill.value = ''; convGen.value++
  // #2128: `roomId` too — see leaveRoomRoute above. Without it, picking one
  // agent from the picker while parked on a room URL leaves the room route (and
  // so the refusal) on screen, and the chat the user asked for never appears.
  if (route.params.sessionId || route.params.roomId) router.push('/workspace')
}
function switchAgent(name) { newChatWithAgent(name) }   // mid-thread = plain new chat, no carry-over
function openThread(t) {
  // ent#361: a room row in the merged sidebar opens the room, not a thread.
  if (t.is_room) { openRoom(t.id); return }
  const sid = t.id || t.session_id
  activeAgentName.value = t.agent_name || activeAgentName.value
  pendingSession.value = sid; prefill.value = ''; convGen.value++
  search.value = ''
  router.push(`/workspace/c/${sid}`)
}
function onSessionAdopted(id) {
  pendingSession.value = id
  if (route.params.sessionId !== id) router.replace(`/workspace/c/${id}`)
  refreshThreads()
}
function usePlaybook(text) { prefill.value = ''; nextTick(() => { prefill.value = text }) }

async function refreshThreads() {
  threads.value = await store.fetchAllSessions()
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
  if (known) { activeAgentName.value = known.agent_name; pendingSession.value = sid; convGen.value++ }
})

// ent#358: `/workspace?agent=<name>` opens that agent's conversation directly —
// the landing spot for anything that used to point at the Agent Detail Session
// surface. The decision itself (which agent, which thread) is a pure function in
// portalUtils so it can be tested without mounting the shell.
function resolveAgentQuery() {
  const landing = resolveAgentLanding({
    agent: route.query.agent,
    forceNew: !!route.query.new,
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
  if (landing.sessionId) router.replace(`/workspace/c/${landing.sessionId}`)
  return true
}

async function bootstrap() {
  await store.fetchRoster()
  await refreshThreads()
  const sid = route.params.sessionId
  if (sid) {
    const known = threads.value.find((t) => (t.id || t.session_id) === sid)
    if (known) { activeAgentName.value = known.agent_name; pendingSession.value = sid }
    else pendingSession.value = sid   // let the conversation resolve/load it
    convGen.value++
    return
  }
  resolveAgentQuery()
}

onMounted(async () => { if (store.isClientSignedIn) await bootstrap() })
onBeforeUnmount(() => { clearInterval(resendTimer); clearTimeout(searchTimer) })

function onSignOut() {
  store.signOut()
  threads.value = []; activeAgentName.value = null; pendingSession.value = null
  step.value = 'email'; email.value = ''; code.value = ''
  // #2128: `roomId` too — otherwise a sign-out from a room URL carries that
  // room id into the next session's address bar.
  if (route.params.sessionId || route.params.roomId) router.push('/workspace')
}
</script>
