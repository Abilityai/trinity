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
    <!-- ent#492: a flex row whose fixed columns take their width from the
         resize variables, and the conversation is `flex-1 min-w-0` — the
         flexible middle BY CONSTRUCTION, so it never carries a width of its own
         and cannot be dragged directly. Widening the messages is done by
         narrowing a neighbour, which is what the AC asks for.

         The issue's technical notes suggested `grid-template-columns`, and the
         first cut did that. It is wrong here: the rail handle is conditional
         (AC 1 — present only while there is a column to drag), so the number of
         grid children CHANGES, and with the handle absent the rail fell into
         the handle's track and the track meant for it stayed empty. A grid
         places children by count; flex does not care. Caught live — the rail's
         expand button was in the DOM and never became clickable.

         Below `sm` the columns collapse to the single stage the drawer and the
         bottom sheet already assume, so the variables are simply unused. -->
    <div v-else class="flex-1 flex min-h-0" :style="columns.gridStyle.value">
      <!-- Sidebar: persistent on desktop, drawer on mobile -->
      <div class="hidden sm:flex shrink-0 min-w-0 overflow-hidden sm:w-[var(--ws-sidebar,18rem)]">
        <PortalSidebar
          :roster="store.agents"
          :threads="sidebarThreads"
          :client-email="store.clientEmail"
          :current-session-id="activeSessionId"
          :current-room-id="activeRoomIdFromRoute"
          :is-platform-session="store.isPlatformSession"
          :loading-roster="store.loading && !store.rosterLoaded"
          v-model:search="search"
          :searching="searching"
          :search-results="searchResults"
          :rename="renameChat"
          @new-chat="newChat"
          @new-chat-with-agent="newChatWithAgent"
          @open-agent="openAgentPage"
          @open-thread="openThread"
          @toggle-star="toggleStar"
          @sign-out="onSignOut"
        />
      </div>
      <ColumnResizeHandle
        :value="columns.sidebar.value"
        :min="columns.limits.sidebar.min"
        :max="columns.limits.sidebar.max"
        label="Resize the sidebar"
        side="left"
        testid="ws-handle-sidebar"
        @resize="columns.resizeSidebar"
        @reset="columns.resetSidebar"
      />

      <div v-if="mobileNav" class="sm:hidden fixed inset-0 z-40">
        <div class="absolute inset-0 bg-black/40" @click="mobileNav = false"></div>
        <div class="absolute inset-y-0 left-0">
          <PortalSidebar
            :roster="store.agents"
            :threads="sidebarThreads"
            :client-email="store.clientEmail"
            :current-session-id="activeSessionId"
            :current-room-id="activeRoomIdFromRoute"
            :is-platform-session="store.isPlatformSession"
            :loading-roster="store.loading && !store.rosterLoaded"
            v-model:search="search"
            :searching="searching"
            :search-results="searchResults"
            :rename="renameChat"
            @new-chat="() => { mobileNav = false; newChat() }"
            @new-chat-with-agent="(n) => { mobileNav = false; newChatWithAgent(n) }"
            @open-agent="(n) => { mobileNav = false; openAgentPage(n) }"
            @open-thread="(t) => { mobileNav = false; openThread(t) }"
            @toggle-star="toggleStar"
            @sign-out="onSignOut"
          />
        </div>
      </div>

      <!-- Main stage. ent#534: while a voice call is on, the conversation
           column takes the orb's share of the stage and the canvas column
           (below) takes the rest — orb left / canvas right, the retired page's
           40/60. Below `sm` the orb has the whole stage. -->
      <!-- During a call (ent#534) the orb and the canvas split the space
           BESIDE the sidebar 40 / 60 as flex shares (2 : 3 of a zero basis),
           not as percentages of the whole row: `w-[40%]` + `w-[60%]` next to
           an 18rem sidebar summed to 100% + 18rem, and the shell's
           `overflow-hidden` clipped the canvas column off the right edge with
           no scrollbar — the #2581 report, measured at 296px on a 1280px
           viewport by the gallery (#2583). -->
      <main
        class="min-w-0 flex flex-col bg-white dark:bg-gray-900"
        :class="voiceCall.active ? 'flex-1 sm:flex-[2_1_0%]' : 'flex-1'"
      >
        <!-- ent#361: a room takes the stage when the URL names one. The
             single-agent conversation is untouched below — different
             substrate, different component, no shared state. -->
        <!-- ent#523: `/workspace/a/:agentName` is no longer a REPORT about the
             agent — it resolves to the chat you were last in and renders the
             conversation, with the agent's numbers in the band above it and its
             context one click away in Agent details. The URL is kept (every
             link to it still works); `landOnAgent` replaces it with the
             thread's own URL as soon as the list is in hand. The skeleton below
             covers that beat, so nothing renders here.

             ent#360's reasoning is not reverted — an agent still has a home with
             its history, what it can do and a place to ask you something. It is
             simply no longer a STOP on the way to the conversation. -->

        <!-- #2540: the stage's first load — while the roster AND the deep
             link's target resolve — is a SKELETON of the conversation frame
             (header, thread, composer), so the loaded surface lands on the
             same footprint. Keyed on the stage VERDICT (`stageZone` over
             `viewState`), never on `store.loading`, so a background refetch
             with a roster on screen never re-enters it; and on `stage.state`,
             not `stage.loading`, because a bare `<x>.loading` gate is what the
             #1927 ratchet counts. The branch chain is its `v-else`: the
             placeholder HEADS the chain now that ent#523 retired the agent-page
             branch that used to precede it, so no terminal arm can render under
             it (the ent#253 lesson). The scanline beam that was here
             (#2163) is the CHART motion and is gone from every non-chart zone. -->
        <PortalSkeleton v-if="stage.state === 'loading'" variant="stage" />
        <template v-else>
        <PortalRoom
          v-if="activeRoomIdFromRoute && store.multiAgentChatAvailable"
          :key="activeRoomIdFromRoute"
          :room-id="activeRoomIdFromRoute"
          :roster="store.agents"
          :starred="isStarred('room', activeRoomIdFromRoute)"
          :prefill="prefill"
          :rename="renameRoom"
          @open-menu="mobileNav = true"
          @rooms-changed="refreshThreads"
          @toggle-star="toggleStar"
          @participants-changed="onRoomParticipants"
          @work-state="onWorkState"
          @open-work="openRailOn('work')"
        >
          <template #rail-strip>
            <PortalRailStrip v-if="railVisible" :tabs="railTabs" :signals="railSignals" @open="railSheetOpen = true" />
          </template>
        </PortalRoom>

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
          <!-- #2163: the "Opening this conversation…" line that used to lead
               this block is gone. It was unreachable copy AND a static one: the
               stage zone above is in `loading` for exactly the window it
               covered (`!rosterLoaded`), so a verdict always exists by the time
               this renders. -->
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
          :threads="threads"
          :rename="renameChat"
          @switch-agent="switchAgent"
          @new-chat="newChatWithAgent(activeAgent.name)"
          @session-adopted="onSessionAdopted"
          @sessions-changed="onConversationTurnDone"
          @open-files="openRailOn('files')"
          @open-menu="mobileNav = true"
          @escalate-to-room="onEscalateToRoom"
          @toggle-star="toggleStar"
          @open-thread="openThread"
          @work-state="onWorkState"
          @open-work="openRailOn('work')"
          @main-reset="onMainReset"
          @open-details="detailsOpen = true"
          @voice-call="onVoiceCall"
          @voice-panel="(v) => { voicePanelVersion = v }"
        >
          <!-- ent#523: the agent's numbers, always visible under the header.
               Mounted by the shell because the shell owns which agent is on
               screen and which side panel is open. -->
          <template #band>
            <PortalAgentBand :agent-name="activeAgent.name" />
          </template>
          <!-- #2579 AC 3 — the operator's mark on a fallback title. The same
               `titleGenerationNotice` copy the settings panel renders, raised
               here because this is where the fallback is being LOOKED at.
               Admin-only (see `refreshTitleHealth`), so a client never sees it
               and never even asks for it. Dismissible on purpose rather than as
               decoration: this line names the agent and says the install has no
               Anthropic key, on the surface an operator is most likely to be
               screen-sharing. Semantic status tokens only. -->
          <template #notice>
            <div
              v-if="titleNotice"
              class="shrink-0 flex items-center gap-2 px-3 sm:px-4 py-1.5 text-xs border-b border-status-warning-200 dark:border-status-warning-500/30 bg-status-warning-50 dark:bg-status-warning-500/10"
              role="status"
              aria-live="polite"
              data-testid="portal-title-notice"
            >
              <span class="shrink-0 font-medium text-status-warning-800 dark:text-status-warning-300">{{ titleNotice.title }}</span>
              <span class="min-w-0 truncate text-status-warning-700 dark:text-status-warning-300" :title="titleNotice.body">{{ titleNotice.body }}</span>
              <button
                type="button"
                class="ml-auto shrink-0 underline hover:no-underline text-status-warning-700 dark:text-status-warning-300"
                data-testid="portal-title-notice-dismiss"
                @click="titleNoticeDismissed = true"
              >Dismiss</button>
            </div>
          </template>
          <template #empty>
            <PortalBriefing :agent="activeAgent" @use-playbook="usePlaybook" />
          </template>
          <template #rail-strip>
            <PortalRailStrip v-if="railVisible" :tabs="railTabs" :signals="railSignals" @open="railSheetOpen = true" />
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
        </template>
      </main>

      <!-- ent#474: the conversation rail — a SIBLING of <main>, so the
           conversation and the room render into the same rail, and its state
           (a setup ref of this view, persisted under one key) rides no remount:
           a chat switch remounts the conversation, never the rail. Hidden on
           the agent page and on every stage that holds no conversation
           (`railVisibleFor`); collapsed by default. Below `sm` the column is
           replaced by the strip above the composer + the sheet below. -->
      <!-- AC 1: present only while there is a column to its right to resize.
           A collapsed rail is a fixed 48px strip, not a resizable column, so
           the handle goes with the width it would drag. Agent details takes the
           rail's place (ent#523) and is the same column, so it gets the same
           handle rather than a second one. -->
      <ColumnResizeHandle
        v-if="thirdColumnResizable"
        :value="columns.railOpenWidth.value"
        :min="columns.limits.rail.min"
        :max="columns.limits.rail.max"
        label="Resize the side panel"
        side="right"
        testid="ws-handle-rail"
        @resize="columns.resizeRail"
        @reset="columns.resetRail"
      />

      <!-- ent#523: Agent details opens INTO THE RAIL'S PLACE (ruled
           2026-09-05) — a sibling of the rail, not a tab. The rail's own state
           is a setup ref of this view, so it is untouched by this swap and
           closing returns it on the tab it was showing. -->
      <!-- ent#534: the agent's canvas takes the right column for the duration
           of a voice call — in the rail's (and the details panel's) place, the
           way Agent details takes it. The rail's own state is a setup ref and
           comes back untouched when the call ends. -->
      <PortalVoiceCanvas
        v-if="voiceCall.active && voiceCall.voiceSessionId && activeAgent"
        class="hidden min-w-0 sm:flex sm:flex-[3_1_0%]"
        :agent-name="activeAgent.name"
        :voice-session-id="voiceCall.voiceSessionId || ''"
        :panel-version="voicePanelVersion"
      />
      <PortalAgentDetails
        v-else-if="detailsOpen && activeAgent"
        :agent-name="activeAgent.name"
        :agent="activeAgent"
        :threads="threads"
        @close="detailsOpen = false"
        @open-thread="(t) => { detailsOpen = false; openThread(t) }"
        @use-playbook="(text) => { detailsOpen = false; usePlaybook(text) }"
      />

      <PortalRail
        v-else-if="railVisible"
        :tabs="railTabs"
        :active-tab="railState.tab"
        :open="railState.open"
        :signals="railSignals"
        :participants="railParticipants"
        @update:open="setRailOpen"
        @update:active-tab="setRailTab"
        @see-hints="seeHints"
      >
        <!-- ent#475: the three re-homed tabs dock into the shell's slots. Each
             body READS a shell-owned store (`usePortalRailFeeds`) and never
             fetches, so the collapsed rail can signal with nothing mounted. -->
        <template #tab-work="{ participants, tab }">
          <PortalWork :participants="participants" :tab="tab" :chat-id="railChatId" @open-thread="openThread" @see-hints="seeHints" @ask-about-it="askAboutIt" />
        </template>
        <template #tab-loops="{ participants, tab }">
          <PortalLoops :participants="participants" :tab="tab" />
        </template>
        <template #tab-canvas="{ participants, tab }">
          <PortalRailCanvas :participants="participants" :tab="tab" @ask-canvas="askForCanvas" />
        </template>
        <template #tab-files="{ participants }">
          <PortalRailFiles :participants="participants" />
        </template>
      </PortalRail>
    </div>

    <!-- ent#474: the rail's mobile form (the former Files drawer's sheet). Same
         component, same tabs, same signals; `sheet` only changes the chrome. -->
    <PortalRail
      v-if="railVisible && railSheetOpen"
      sheet
      :tabs="railTabs"
      :active-tab="railState.tab"
      :signals="railSignals"
      :participants="railParticipants"
      @update:active-tab="setRailTab"
      @close="railSheetOpen = false"
      @see-hints="seeHints"
    >
      <!-- ent#475: the same three re-homed tabs dock into the shell's slots. Each
           body READS a shell-owned store (`usePortalRailFeeds`) and never
           fetches, so the collapsed rail can signal with nothing mounted. -->
      <template #tab-work="{ participants, tab }">
        <PortalWork :participants="participants" :tab="tab" :chat-id="railChatId" @open-thread="openThread" @see-hints="seeHints" @ask-about-it="askAboutIt" />
      </template>
      <template #tab-loops="{ participants, tab }">
        <PortalLoops :participants="participants" :tab="tab" />
      </template>
      <template #tab-canvas="{ participants, tab }">
        <PortalRailCanvas :participants="participants" :tab="tab" @ask-canvas="askForCanvas" />
      </template>
      <template #tab-files="{ participants }">
        <PortalRailFiles :participants="participants" />
      </template>
    </PortalRail>

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
import PortalLoops from '@/components/portal/PortalLoops.vue'
import PortalRailCanvas from '@/components/portal/PortalRailCanvas.vue'
import PortalWork from '@/components/portal/PortalWork.vue'
import PortalRailFiles from '@/components/portal/PortalRailFiles.vue'
import PortalCodeInput from '@/components/portal/PortalCodeInput.vue'
import PortalAgentPicker from '@/components/portal/PortalAgentPicker.vue'
import PortalRoom from '@/components/portal/PortalRoom.vue'
import PortalAgentBand from '@/components/portal/PortalAgentBand.vue'
import PortalAgentDetails from '@/components/portal/PortalAgentDetails.vue'
import ColumnResizeHandle from '@/components/ColumnResizeHandle.vue'
import { useColumnResize } from '@/composables/useColumnResize'
import PortalSkeleton from '@/components/portal/PortalSkeleton.vue'
import PortalRail from '@/components/portal/PortalRail.vue'
import PortalRailStrip from '@/components/portal/PortalRailStrip.vue'
import PortalVoiceCanvas from '@/components/portal/PortalVoiceCanvas.vue'
import { usePortalRailFeeds } from '@/composables/usePortalRailFeeds'
import {
  RAIL_TABS,
  askCanvasPrefill,
  isWideViewport,
  railOpenPlan,
  emptySignal,
  loadRailState,
  railParticipantsFor,
  railVisibleFor,
  saveRailState,
  visibleTabs,
} from '@/components/portal/portalRail'
import { stageZone } from '@/components/portal/portalBriefingState'
import {
  isNewChatHotkey, resolveAgentLanding, shouldMarkTurnRead, shouldEscapeStage,
  landingThread,
  agentHasMain, titleSettling, shouldFetchTitleHealth, titleGenerationNotice,
  TITLE_SETTLE_DELAYS_MS,
} from '@/components/portal/portalUtils'

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
const mobileNav = ref(false)
const convGen = ref(0)                // bumps on explicit thread switches → remount
// #2163 — `bootstrap()` has finished placing the caller (see the function).
// Deliberately a separate bit from `store.rosterLoaded`: that one says the
// ROSTER reached a verdict, this one says the deep link did.
const bootstrapResolved = ref(false)

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

// #2163 — the stage's loading verdict. Keyed on `rosterLoaded` (a VERDICT)
// and never on `store.loading` (fetch in flight), so a background refetch
// with a roster on screen is invisible; `viewState` owns that rule.
const stage = computed(() => stageZone({
  rosterLoaded: store.rosterLoaded,
  resolved: bootstrapResolved.value,
  error: store.error,
  agents: store.agents,
}))

// ---- Conversation rail (ent#474) --------------------------------------------
// State is a SETUP ref of this view — outside `convKey` and outside every stage
// branch — so a chat switch (which remounts the conversation) and a live update
// (which patches the rail body in place) never touch it. Persisted under ONE
// key (design pass, "State & honesty"), read synchronously here before first
// paint, written on every change.
const railState = ref(loadRailState(safeStorage()))
watch(railState, (s) => saveRailState(safeStorage(), s), { deep: true })
const railSheetOpen = ref(false)
// ent#523 — Agent details, which takes the rail's place while open. A setup ref
// of this view for the same reason `railState` is one: it must survive the
// conversation remounting on a chat switch. Closed on every agent change, since
// a panel about the previous agent is worse than no panel.
const detailsOpen = ref(false)

// ent#534 — the voice call the conversation reports. Owned here because the
// shell decides what the right column shows and whether a chat may be left:
// while a call is on, sidebar clicks, the tabs, New chat and ⌘J are refused
// (a switch remounts the conversation and would drop the call); route-driven
// changes end the call gracefully inside the conversation instead.
const voiceCall = ref({ active: false, agentName: null, voiceSessionId: null })
const voicePanelVersion = ref(0)
function onVoiceCall(sig) {
  voiceCall.value = sig?.active
    ? { active: true, agentName: sig.agentName || null, voiceSessionId: sig.voiceSessionId || null }
    : { active: false, agentName: null, voiceSessionId: null }
  if (!sig?.active) voicePanelVersion.value = 0
}

// ent#492 — the three resizable columns. Widths are per user and read
// synchronously here, before first paint, so a reload does not flash the
// default layout. The rail's own open/collapsed state stays `railState`'s: this
// owns how WIDE the column is, never whether it is there — except for the
// auto-collapse below, which is the AC's tie-breaker when the viewport cannot
// fit all three.
const columns = useColumnResize({
  railOpen: computed(() => railState.value.open && railVisible.value),
  setRailOpen: (open) => { if (!open) setRailOpen(false) },
})

// The third column is resizable only when it is a real column: the rail when
// open, or Agent details, which takes its place at the same width. Not during
// a voice call (ent#534): the canvas takes that column at a fixed share and
// would ignore the width the handle drags.
const thirdColumnResizable = computed(() => (
  !voiceCall.value.active
  && ((detailsOpen.value && !!activeAgent.value) || (railVisible.value && railState.value.open))
))
const roomParticipants = ref([])
const workSignal = ref(emptySignal())

const railParticipants = computed(() => railParticipantsFor({
  agentPage: activeAgentPageName.value,
  roomId: activeRoomIdFromRoute.value,
  roomParticipants: roomParticipants.value,
  activeAgent: activeAgent.value?.name,
}))
// THE door gate (the per-door test): the rail column, the mobile strip and the
// sheet all read this list and never the registry, so a tab whose door this
// session fails has no icon, no label and no mounted body — and therefore no
// request for whatever that body would fetch.
const railTabs = computed(() => visibleTabs(RAIL_TABS, {
  isPlatform: store.isPlatformSession,
  participants: railParticipants.value,
}))
// Keyed on the route and the stage VERDICT — synchronous facts — never on data
// still arriving (a room's participants land with its own fetch), so a live
// update cannot flicker the rail in and out.
const railVisible = computed(() => railVisibleFor({
  agentPage: activeAgentPageName.value,
  stageState: stage.value.state,
  roomId: activeRoomIdFromRoute.value,
  roomsAvailable: store.multiAgentChatAvailable,
  activeAgent: activeAgent.value?.name,
  unreachable: !!unreachableAgent.value,
}))
// ent#475: the ONE owner of what the Loops / Canvas / Files tabs read. It
// feeds `portalLoops` and `portalRailFeeds` off the same door gate and
// participant list the rail renders from — nothing is fetched for a tab this
// session cannot see, or before the stage verdict — and hands back the three
// store-derived signals. The Work signal still rides the emits below (#457).
// ent#525: the open 1:1 thread, for the Work feed's delegated children. A
// room has no portal session, so it passes nothing.
const railChatId = computed(() => (activeRoomIdFromRoute.value ? null : (activeSessionId.value || pendingSession.value || null)))
const rail = usePortalRailFeeds({
  visible: railVisible,
  tabs: railTabs,
  participants: railParticipants,
  activeTab: computed(() => railState.value.tab),
  open: computed(() => railState.value.open),
  sheetOpen: railSheetOpen,
  storage: safeStorage,
  chatId: railChatId,
  workEmit: workSignal,
})
// ent#525: the Work signal is store-derived now — the owner merges the
// conversation's emit into the feed's running rows BY EXECUTION ID.
const railSignals = computed(() => ({ ...rail.signals.value }))

function setRailOpen(open) { railState.value = { ...railState.value, open } }
function setRailTab(tab) { railState.value = { ...railState.value, tab } }
function onWorkState(sig) {
  const wasLive = workSignal.value.live > 0
  workSignal.value = sig || emptySignal()
  // ent#475: a turn just ended (1:1: `sending` fell; room: the server's
  // `working` list went idle) — the moment a canvas or a file may have
  // changed. One trigger for both chats, no timer.
  if (wasLive && !workSignal.value.live) rail.refresh()
  // ent#525: a turn STARTING is when its row (and, soon, its delegated
  // children) appear — read the feed once the row exists; the 12 s poll then
  // runs while it is live. A room's `working` list starts the same way.
  if (!wasLive && workSignal.value.live) rail.work.scheduleRefresh(1500)
}

// ent#475: "open the rail on <tab>" — the header paperclip. The column at
// and above `sm`, the sheet below it; a phone tap never persists `open`.
function openRailOn(tab) {
  const plan = railOpenPlan({ wide: isWideViewport(typeof window !== 'undefined' ? window : null) })
  setRailTab(tab)
  if (plan.open) setRailOpen(true)
  if (plan.sheet) railSheetOpen.value = true
}

// ent#475: the Canvas tab's empty action — a PREFILL, never a send.
function askForCanvas() {
  railSheetOpen.value = false
  usePlaybook(askCanvasPrefill(railParticipants.value))
}
// ent#525: the Work tab's "Ask about it" — the same prefill path, never a send.
function askAboutIt(text) {
  railSheetOpen.value = false
  usePlaybook(text)
}
function onRoomParticipants(list) { roomParticipants.value = Array.isArray(list) ? list : [] }

// A signal belongs to the chat that reported it. The conversation is keyed by
// `convKey` and the room by its id, so a switch unmounts the reporter; reset
// here as well, so nothing can read as still running across the switch — the
// "never a stuck indicator" half of #474's AC.
// ent#475: NO `rail.reset()` here. Watchers run in creation order, and the
// feeds owner (`usePortalRailFeeds`, created above) reacts to the same route
// change FIRST — it has already re-scoped both stores to the new chat and
// started their fetches by the time this runs, so a reset here would wipe the
// new chat's data and nothing would refetch. A participant change IS the
// reset (`setParticipants` clears each store); the owner clears on its own
// when the rail leaves the screen.
watch([convKey, activeRoomIdFromRoute], () => {
  // ent#534: the reporter unmounted (its own teardown ended the call on the
  // server); the shell must not keep showing a canvas column for it.
  onVoiceCall(null)
  workSignal.value = emptySignal()
  roomParticipants.value = []
  railSheetOpen.value = false
})

// "See what you can ask" — the Work tab's empty-state action. The hints are
// the briefing on the empty-chat screen: when they are on screen, go to them;
// otherwise the agent page's "what it can do" is the nearest home.
function seeHints() {
  railSheetOpen.value = false
  const el = typeof document !== 'undefined' ? document.getElementById('portal-briefing') : null
  if (el) {
    const reduce = typeof window !== 'undefined'
      && typeof window.matchMedia === 'function'
      && window.matchMedia('(prefers-reduced-motion: reduce)').matches
    el.scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'start' })
    return
  }
  const first = railParticipants.value[0]
  if (first) openAgentPage(first)
}

// localStorage can throw on access (private mode, blocked site data); the rail
// then runs session-only, which `loadRailState`/`saveRailState` already treat
// as the default.
function safeStorage() {
  try { return typeof localStorage !== 'undefined' ? localStorage : null } catch { return null }
}

// #2163 — hydrate the ACTIVE agent's briefing, driven from HERE rather than
// from `PortalBriefing`'s mount. `PortalBriefing` renders only in the
// conversation's `#empty` slot, so a deep link into an EXISTING thread never
// mounts it — and that agent's `/` typeahead reads the same `playbooks`, so it
// would have waited for the background batch's slowest member. Watching the
// active agent covers deep links, thread opens and agent switches; the
// component stays presentational. The store's own guards make repeat calls
// free (`ready` never re-requests; `unavailable` retries once per session).
watch(
  () => activeAgent.value?.name,
  (name) => { if (name) store.ensureBriefing(name) },
  { immediate: true }
)

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
// ent#523: clicking an agent opens the CONVERSATION you were last in, not a
// report about the agent. The `/workspace/a/:name` URL is kept — every existing
// link, and the sidebar row, still route through it — and `landOnAgent`
// swaps it for the thread's own URL once the list is in hand. Landing here
// rather than pushing the thread URL directly is deliberate: the thread list
// may not have loaded yet on a cold deep link, and this way the URL is honest
// at every instant instead of pointing at a chat we have not resolved.
function openAgentPage(name) {
  if (!name) return
  unreachableAgent.value = null
  pendingSession.value = null
  startingNewChat.value = false
  activeRoomId.value = null
  detailsOpen.value = false
  router.push(`/workspace/a/${encodeURIComponent(name)}`)
}

// ent#523 — turn `/workspace/a/:name` into the chat to land in.
// Named `landOnAgent` to stay clear of the pure `resolveAgentLanding` above,
// which answers the same question for the `?agent=` deep link; both defer to
// `landingThread` so there is ONE rule for which chat you land in.
//
// `landingThread` is the rule (most recently active, Main as the floor); it is
// pure and lives in portalUtils so it is testable without a mount. With no
// chats at all the agent's Main has not been minted yet, so the shell asks the
// server for the list — which is what mints it — and lands on what comes back.
// A failure leaves the caller on the agent URL with the stage's own error
// states, rather than dropping them somewhere unrelated.
async function landOnAgent(name) {
  if (!name) return
  activeAgentName.value = name
  const target = landingThread(threads.value, name)
  if (target) { openThread(target); return }
  try {
    // #2579: this branch used to destructure `{ sessions }` off the store's
    // return value — which is an ARRAY (`data.sessions || []`). `sessions` was
    // therefore always `undefined`, `landingThread` always missed, and the
    // repair branch this comment describes never once ran: a first-time
    // visitor always fell through to a fresh chat, and the Main the call had
    // just minted server-side never reached the screen. It now goes through
    // `ensureMainListed`, which does the same per-agent read AND folds the
    // result into `threads` — one seam for "the list must show this agent's
    // Main", shared with the watcher below.
    await ensureMainListed(name)
    // The watcher fires on the route param AND on the thread list arriving, so
    // two landings can be in flight at once on a cold deep link: the first
    // misses (no threads yet) and goes to the network, the second finds the
    // list and navigates. Without this the first one's late resolution
    // navigates too — moving the person off a chat they have since chosen. The
    // route is the authority; if it no longer names this agent, this landing
    // has been overtaken and has nothing to say. Re-checked HERE, after the
    // await: the ensure spends two round trips where the old code spent one.
    if (activeAgentPageName.value !== name) return
    const landed = landingThread(threads.value, name)
    if (landed) { openThread(landed); return }
  } catch {
    // Fall through: a fresh chat is a better answer than a dead stage.
  }
  if (activeAgentPageName.value !== name) return
  newChatWithAgent(name)
}

// #2579 — make sure this agent's pinned Main is IN the list on screen.
//
// The cross-agent batch deliberately never mints a Main (it would write a row
// per rostered agent on every sidebar refresh), and the per-agent
// `list_sessions` is the read that does. So a (user, agent) pair whose chats
// predate ent#523 has a Main nowhere: not in the list, and not in the database
// until something calls the per-agent route. This is that something.
//
// Two things to be honest about:
//   * it is a GET that INSERTS. `list_sessions` → `ensure_main_session`, so
//     visiting N agents creates N empty rows. That is ent#523's stated intent
//     ("opening an agent is the moment the pinned tab has to be there").
//   * the retry cap is not defensive tidiness. `fetchAllSessions` NEVER
//     rejects — on failure it flags `sessionsFailed` and returns the last good
//     list — so a resolved entry over a still-missing Main would make the miss
//     permanent for the whole session. Deleting the entry on a miss lets the
//     next visit try again; the cap stops that becoming a loop, because
//     `refreshThreads` re-fires the `threads.value.length` watchers that call
//     back into here.
const mainEnsured = new Map()   // agent name → in-flight promise
const mainAttempts = new Map()  // agent name → attempts spent (cap below)
const MAIN_ENSURE_ATTEMPTS = 2

function ensureMainListed(name) {
  if (!name) return Promise.resolve()
  if (agentHasMain(threads.value, name)) return Promise.resolve()
  const inflight = mainEnsured.get(name)
  if (inflight) return inflight
  const spent = mainAttempts.get(name) || 0
  if (spent >= MAIN_ENSURE_ATTEMPTS) return Promise.resolve()
  mainAttempts.set(name, spent + 1)
  const p = store.fetchSessions(name)
    .then(() => refreshThreads())
    .then(() => {
      // Still no Main? Then this attempt bought nothing, and the entry must not
      // stand as a resolved "already handled" for the rest of the session.
      if (!agentHasMain(threads.value, name)) mainEnsured.delete(name)
    })
    .catch(() => { mainEnsured.delete(name) })
  mainEnsured.set(name, p)
  return p
}

// Guarded exactly like the landing watcher below: signed in, roster resolved,
// a name to act on. A brand-new agent gets its Main on the first visit rather
// than on whichever later refresh happened to follow a write.
watch(activeAgentName, (name) => {
  if (!name || !store.isClientSignedIn || !store.rosterLoaded) return
  ensureMainListed(name)
})

// ent#523 — Reset finished. The shell owns what happens next, since the
// conversation does not know its own route: land in the fresh Main and refresh
// the list so the archive appears as an ordinary chat. A no-op reset
// (`archived_session_id: null`, an untouched Main) leaves the person exactly
// where they are — re-navigating to the same thread would flash the stage for
// no reason.
async function onMainReset(result) {
  await refreshThreads()
  if (!result?.archived_session_id) return
  const id = result.main_session_id
  if (!id) return
  pendingSession.value = id
  convGen.value++
  router.push(`/workspace/c/${id}`)
}

function newChatWithAgent(name) {
  if (voiceCall.value.active) return   // ent#534: end the call to switch chats
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
  if (voiceCall.value.active) return   // ent#534: end the call to switch chats
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

// ent#523: what the SIDEBAR lists, which is not what the tab strip lists.
//
// An unused Main is not a "recent chat" — it exists for every pair the moment
// the agent is opened, so listing it would put a "New chat" row under every
// agent the person has never talked to, and the agent's own row already is the
// way into it. The tab strip must show Main from the first visit, so this is a
// projection for one consumer rather than a filter on `threads` itself.
const sidebarThreads = computed(() => threads.value.filter(
  (t) => !(t.is_main && !t.last_message_at),
))

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
  // ent#491: rank any agent this session has not ranked yet. Fills only missing
  // keys, so a refresh triggered by an incoming reply cannot walk back a send's
  // bump and re-sort the sidebar under the cursor.
  store.seedAgentRecency(threads.value)
}

// A turn finishing in the conversation the user is LOOKING AT is read by
// definition. Without this, the reply the user just watched arrive would land
// after the read cursor set at dispatch and badge the chat they are sitting in
// — a notification for something they are actively reading.
function onConversationTurnDone(sessionId) {
  // #2579: this event has four emitters, and one of them (the send's own
  // `sessions-changed`) can fire with a null id. Everything below needs a
  // thread to be about.
  clearTitleSettle()
  if (!sessionId) return refreshThreads()
  // Only if the user is STILL in that thread. The conversation's send is an
  // async closure that outlives the component, so this fires even when they
  // have navigated away mid-turn — which is the main way a reply legitimately
  // arrives unseen. Marking read unconditionally cleared exactly the badge the
  // feature exists to show, and made it near-unreachable in normal use.
  const open = activeSessionId.value || pendingSession.value
  // #2579: the settle cycle asks the SAME question, for the same reason. This
  // fires for a thread the user has navigated away from — that is the main way
  // a reply legitimately arrives unseen — and a cycle armed on a background
  // thread would go on replacing the list for 16s and could raise a notice
  // above a different conversation. `watch(convKey)` only catches the switch
  // that happens after arming; this catches the one that happened before.
  const stillHere = shouldMarkTurnRead(sessionId, open)
  return (stillHere ? markRead('thread', sessionId) : Promise.resolve())
    .then(refreshThreads)
    .then(() => { if (stillHere) armTitleSettle(sessionId) })
}

// --- Titles settle (#2579) --------------------------------------------------
//
// #2579 moved the generator's spawn to run CONCURRENTLY with the turn, so in
// the ordinary case the generated title is already on the row this refresh
// reads. This is the belt for the cases that move does not close: a turn faster
// than the model call, the `retry` attempt (which by construction lands after
// its own turn), and a slow provider.
//
// It re-reads the LIST — no chat-state fetch — on a bounded schedule and stops
// the moment the title differs from what it saw at turn-done. Deliberately not
// a mirror of the server's `PORTAL_TITLE_TIMEOUT_SECONDS`, which is
// operator-tunable: exhausting the schedule is a trigger to ASK the health
// record, never a verdict of its own (the #2133 class).
const titleSettleTimers = []
let titleAtTurnDone = null
const titleHealth = ref(null)
const titleNoticeDismissed = ref(false)

function clearTitleSettle() {
  while (titleSettleTimers.length) clearTimeout(titleSettleTimers.pop())
  titleAtTurnDone = null
}

function threadRow(sessionId) {
  return (threads.value || []).find((t) => !t.is_room && (t.id || t.session_id) === sessionId) || null
}

function armTitleSettle(sessionId) {
  // Idempotent. `onConversationTurnDone` clears synchronously but arms after an
  // await, so two events close together (the voice path emits `sessions-changed`
  // right after `createSession`, and again when the turn lands) would otherwise
  // leave the first cycle's timers running beside the second's, against a
  // baseline the second overwrote.
  clearTitleSettle()
  // /review: the "still in this thread" question has to be asked HERE too, not
  // only at the event. `onConversationTurnDone` decides it synchronously and
  // then arms after two awaits (`markRead` + `refreshThreads`), so a thread
  // switch inside that window passes the caller's check, fires `watch(convKey)`
  // while nothing is armed yet, and lands here anyway — arming a cycle on a
  // conversation the person has already left, which is the exact thing the
  // caller's comment says is prevented.
  if (!shouldMarkTurnRead(sessionId, activeSessionId.value || pendingSession.value)) return
  const row = threadRow(sessionId)
  if (!row || !titleSettling(row)) return
  titleAtTurnDone = row.title || ''
  TITLE_SETTLE_DELAYS_MS.forEach((ms, i) => {
    titleSettleTimers.push(setTimeout(() => settleTick(sessionId, i === TITLE_SETTLE_DELAYS_MS.length - 1), ms))
  })
}

async function settleTick(sessionId, last) {
  // List only. `fetchAllSessions` NEVER rejects — it flags `sessionsFailed` and
  // hands back the last good list — so without asking it, a flaky network reads
  // as "the title never changed" and would report a working generator broken.
  const list = await store.fetchAllSessions().catch(() => null)
  if (list === null || store.sessionsFailed) { clearTitleSettle(); return }
  threads.value = decorate(list)
  // /review: this replaces `threads` exactly as `refreshThreads` does, so it
  // owes the same ent#491 seeding — otherwise an agent this session has not
  // ranked stays unranked for as long as the cycle keeps overwriting the list.
  // Fills only MISSING keys, so it cannot walk back a send's bump.
  store.seedAgentRecency(threads.value)
  const row = threadRow(sessionId)
  // Gone (Reset, delete): stop, and leave the health verdict alone. A deleted
  // row is not evidence that generation works.
  if (!row) { clearTitleSettle(); return }
  if ((row.title || '') !== titleAtTurnDone) {
    // It landed — generation demonstrably works, whoever did it. A person's
    // rename stops the cycle too, and that is right: `_title_plan` returns None
    // for `title_source == 'user'`, so there is nothing left to wait for.
    titleHealth.value = null
    clearTitleSettle()
    return
  }
  if (last) { clearTitleSettle(); refreshTitleHealth() }
}

// Only a platform admin, and only after a title demonstrably failed to settle
// on a SUCCESSFUL read. Never at bootstrap — this is a diagnostic, not a page
// dependency — and any refusal means no notice rather than an error in a
// client's face.
async function refreshTitleHealth() {
  if (!shouldFetchTitleHealth(store.isPlatformSession, authStore.role)) return
  try {
    titleHealth.value = await store.fetchTitleGenerationHealth()
  } catch {
    titleHealth.value = null
  }
}

const titleNotice = computed(() =>
  titleNoticeDismissed.value ? null : titleGenerationNotice(titleHealth.value)
)

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

// ent#473: a person renames a chat. Optimistic like the star — the row and
// the header redraw at once — and reverted in place on refusal, with the
// error RETHROWN so the editor that asked can show the server's own sentence
// (a named 400 says which rule; a 404 says the chat is no longer theirs).
// The list is re-read afterwards so a title the generator landed meanwhile,
// or a rename from another tab, is what the sidebar shows next.
async function renameChat(t, title) {
  const key = chatKey(t)
  const id = t.id || t.session_id
  const before = threads.value
  threads.value = threads.value.map((x) => (chatKey(x) === key
    ? { ...x, title, ...(x.is_room ? { name: title } : {}) }
    : x))
  try {
    if (t.is_room) await store.renameRoom(id, title)
    else await store.renameThread(t.agent_name, id, title)
  } catch (err) {
    threads.value = before
    throw err
  }
  refreshThreads()
}
function renameRoom(roomId, title) {
  return renameChat({ id: roomId, is_room: true }, title)
}

// ent#451: ⌘J / Ctrl+J — New chat with the agent in front of you (the page
// or the conversation); with no agent in front of you, the picker. Armed at
// mount, above bootstrap's await (contract #23), and inert until signed in.
function onGlobalKeydown(e) {
  if (!isNewChatHotkey(e)) return
  if (!store.isClientSignedIn) return
  if (voiceCall.value.active) return   // ent#534: the call owns the stage
  e.preventDefault()
  const name = activeAgentPageName.value
    || (!activeRoomIdFromRoute.value && !unreachableAgent.value ? activeAgent.value?.name : null)
  if (name) newChatWithAgent(name)
  else newChat()
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

// ent#523: `/workspace/a/:agentName` resolves to a conversation.
//
// Watched on BOTH the route param and the thread list, for the same reason the
// `sessionId` watcher above watches both: on a cold deep link the agent name
// arrives long before the threads do, so a param-only watcher would resolve
// against an empty list and always mint a new chat. Re-entrancy is guarded by
// the fact that `landOnAgent` navigates away from this route as soon as it
// succeeds; while it has not, re-running is harmless and idempotent.
watch([activeAgentPageName, () => threads.value.length], ([name]) => {
  if (!name || !store.isClientSignedIn) return
  // Wait for the roster verdict. Landing before it means `activeAgent` cannot
  // resolve the name yet, and the "you don't have access" branch would fire for
  // an agent the caller can perfectly well reach.
  if (!store.rosterLoaded) return
  landOnAgent(name)
})

// A panel about the PREVIOUS agent is worse than no panel, so details closes on
// every agent change rather than following the conversation across.
watch(() => activeAgent.value?.name, (next, prev) => {
  if (next !== prev) detailsOpen.value = false
})

// ent#492: a sign-in inside this tab changes whose layout this is, and the
// identity is read from storage at setup — so it has to be re-read once the
// session lands, or this session keeps writing into the anonymous bucket.
watch(() => store.isClientSignedIn, () => columns.refreshIdentity())

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
  // #2163: the stage stays in its loading phase until this whole function has
  // run, not merely until the roster lands. `activeAgentName`/`pendingSession`
  // are assigned only AFTER `refreshThreads()`, so a `/workspace/c/:sid` deep
  // link (or `?agent=X`) would otherwise reveal the stage for `agents[0]`,
  // flash that agent's briefing and fire a wasted hydration, then remount for
  // the real target. AC4 says "while the roster AND a thread's history
  // hydrate". The `finally` is load-bearing: the deep-link branch returns
  // early, and a throw must not strand the stage in loading forever.
  bootstrapResolved.value = false
  try {
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
  } finally {
    bootstrapResolved.value = true
  }
}

onMounted(async () => {
  window.addEventListener('keydown', onGlobalKeydown)
  if (store.isClientSignedIn) await bootstrap()
})
onBeforeUnmount(() => {
  window.removeEventListener('keydown', onGlobalKeydown)
  stopAsksPoll()          // ent#364 — the poll must not outlive the view
  clearInterval(resendTimer)
  clearTimeout(searchTimer)
  clearTitleSettle()      // #2579 — nor may the settle cycle
})

// #2579 — the third clear site, and the one that matters most in practice.
// `convKey` is the seam that already MEANS "the conversation changed"; neither
// of the other two fires on a thread switch, so without this a cycle armed in
// chat A keeps replacing `threads.value` under the user for 16 seconds and can
// raise a notice above chat B.
watch(convKey, () => { clearTitleSettle() })

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
    // #2579: this handler resets state IN PLACE — the OTP form is a branch of
    // this same component, so the view is never remounted. Without clearing
    // these, client B signing in on the same tab inherits client A's resolved
    // promises and never gets a Main: exactly the defect they exist to fix,
    // reintroduced for the second principal.
    mainEnsured.clear(); mainAttempts.clear()
    clearTitleSettle(); titleHealth.value = null; titleNoticeDismissed.value = false
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
