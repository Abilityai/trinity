<template>
  <!-- ent#524: the whole conversation is the drop target — "dropping one or
       more files ANYWHERE on the conversation uploads them", on by default,
       no setting. The listeners sit on the root rather than on the composer
       because the thread is where the eye is; the affordance names what will
       happen so a drop is never a guess. Dragging text or a link is not a file
       drag (`isFileDrag`) and lights nothing. -->
  <!-- `h-full` is correct while this is `<main>`'s only child, which it is: the
       band renders through the `#band` slot below, INSIDE this column. If the
       band is ever lifted out to a sibling (the #2580 follow-up that would let
       its instance survive a thread switch), this must become `flex-1 min-h-0`
       in the same change — `h-full` would still resolve to 100% of `<main>`, so
       the column would total band + 100% and push the composer past the bottom
       of a shell that is `overflow-hidden`: off-screen, with no scrollbar to get
       it back. -->
  <div
    class="relative flex flex-col h-full min-h-0"
    @dragenter="dropHandlers.onDragEnter"
    @dragover="dropHandlers.onDragOver"
    @dragleave="dropHandlers.onDragLeave"
    @drop="dropHandlers.onDrop"
  >
    <!-- `pointer-events-none` so the overlay cannot swallow the drop it is
         announcing — the listeners are on the container underneath. -->
    <div
      v-if="fileDragging"
      class="absolute inset-2 z-20 pointer-events-none rounded-2xl border-2 border-dashed border-action-primary-400 bg-action-primary-50/80 dark:bg-action-primary-900/30 flex items-center justify-center"
      data-testid="portal-drop-overlay"
      aria-hidden="true"
    >
      <p class="text-sm font-medium text-action-primary-700 dark:text-action-primary-200">
        Drop files to send to {{ agentDisplayName(agent) }}
      </p>
    </div>

    <!-- Header: agent identity + picker, files, voice -->
    <header class="shrink-0 flex items-center gap-2 px-3 sm:px-4 h-14 border-b border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900">
      <button
        class="sm:hidden -ml-1 p-2 text-gray-500 hover:text-gray-800 dark:hover:text-gray-200"
        aria-label="Menu"
        @click="$emit('open-menu')"
      >
        <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6h16M4 12h16M4 18h16" /></svg>
      </button>

      <!-- Agent picker (ChatGPT model-picker position) -->
      <div class="relative min-w-0" ref="pickerRef">
        <button
          class="flex items-center gap-2 max-w-full rounded-lg px-2 py-1.5 hover:bg-gray-100 dark:hover:bg-gray-800 transition disabled:cursor-not-allowed"
          :disabled="voiceCallActive"
          data-testid="portal-agent-picker"
          @click="pickerOpen = !pickerOpen"
        >
          <PortalAvatar :name="agent.name" :avatar-url="agent.avatar_url" :size="26" />
          <span class="font-semibold truncate">{{ agentDisplayName(agent) }}</span>
          <svg class="w-4 h-4 text-gray-400 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" /></svg>
        </button>
        <div
          v-if="pickerOpen"
          class="absolute z-30 mt-1 w-72 max-w-[80vw] rounded-xl border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 shadow-xl py-1 max-h-80 overflow-y-auto"
        >
          <button
            v-for="a in roster"
            :key="a.name"
            class="w-full flex items-center gap-2.5 px-3 py-2 text-left hover:bg-gray-100 dark:hover:bg-gray-800"
            @click="pickAgent(a)"
          >
            <PortalAvatar :name="a.name" :avatar-url="a.avatar_url" :size="28" />
            <span class="min-w-0">
              <span class="block text-sm font-medium truncate">
                {{ a.name === agent.name ? agentDisplayName(a) : `New chat with ${agentDisplayName(a)}` }}
              </span>
              <span v-if="a.description" class="block text-xs text-gray-400 truncate">{{ a.description }}</span>
            </span>
          </button>
        </div>
      </div>

      <!-- ent#473: the thread's title, renameable in place. Below `sm` the
           picker and the controls already fill the bar; the tab strip under
           it still names the active chat. -->
      <!-- ent#523: Main is named by its ROLE and is not renameable — it is the
           same thread for the life of the pair and the place the agent reaches
           you, so a title derived from whatever was said in it first (or a
           person's rename) would make the pinned tab and this header disagree
           about which chat you are in. Every other chat renames as before. -->
      <span
        v-if="isMainChat"
        class="hidden sm:inline min-w-0 flex-1 truncate text-sm font-medium"
      >{{ MAIN_TAB_LABEL }}</span>
      <PortalEditableTitle
        v-else-if="currentThread"
        class="hidden sm:flex"
        :value="currentTitle"
        placeholder="New chat"
        :rename="rename ? saveTitle : null"
        label="Rename this chat"
        text-class="text-sm font-medium"
      />
      <span v-else-if="!currentSessionId" class="hidden sm:inline min-w-0 flex-1 truncate text-sm text-gray-500 dark:text-gray-400">New chat</span>

      <div class="ml-auto flex items-center gap-1 shrink-0">
        <!-- ent#451: New chat lives in the header, with its hotkey (⌘J /
             Ctrl+J, ruled 2026-09-06). Starts a fresh thread with THIS agent —
             the sidebar's button is the cross-agent one (the picker). -->
        <button
          type="button"
          class="inline-flex items-center gap-1 rounded-lg px-2 py-1.5 text-sm font-medium text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800 hover:text-gray-900 dark:hover:text-gray-100 transition"
          :title="`New chat (${newChatHotkey})`"
          :aria-label="`New chat (${newChatHotkey})`"
          :aria-keyshortcuts="newChatHotkey === '⌘J' ? 'Meta+J' : 'Control+J'"
          :disabled="voiceCallActive"
          data-testid="new-chat-header"
          @click="emit('new-chat')"
        >
          <svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" /></svg>
          <span class="hidden md:inline">New chat</span>
          <kbd class="hidden lg:inline text-[11px] font-mono text-gray-400 dark:text-gray-500">{{ newChatHotkey }}</kbd>
        </button>
        <!-- ent#359 AC #4: star from the header too. Hidden until the thread
             exists — a chat with no id yet cannot be pinned, and rendering a
             control that silently does nothing is the dead end this family of
             issues keeps removing. -->
        <PortalStarButton
          v-if="currentSessionId"
          :class="voiceCallActive ? 'opacity-40 pointer-events-none' : ''"
          :starred="starred"
          @toggle="$emit('toggle-star', { id: currentSessionId, is_room: false, starred })"
        />
        <!-- ent#547: the voice call now starts from the COMPOSER ROW, beside
             attach — where ChatGPT puts it, and beside the other thing you do
             to say something. See the composer for the placement and for why it
             sits outside the row's inert wrapper. -->
        <!-- Hidden while a call is on: the orb owns playback then. -->
        <button
          v-if="ttsEnabled && !voiceCallActive"
          class="p-2 rounded-lg transition"
          :class="voiceMode ? 'bg-action-primary-100 dark:bg-action-primary-900/40 text-action-primary-600 dark:text-action-primary-300' : 'text-gray-400 hover:text-gray-700 dark:hover:text-gray-200'"
          :title="voiceMode ? 'Voice replies on — click to mute' : 'Speak replies aloud'"
          @click="voiceMode ? (voiceMode = false, stopSpeaking()) : (voiceMode = true)"
        >
          <svg v-if="voiceMode" class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15.536 8.464a5 5 0 010 7.072M18.364 5.636a9 9 0 010 12.728M5 9v6h4l5 4V5L9 9H5z" /></svg>
          <svg v-else class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 14l4-4m0 4l-4-4M5 9v6h4l5 4V5L9 9H5z" /></svg>
        </button>
        <!-- ent#523: Reset, on Main only. No confirmation (operator,
             2026-09-06: "we are not losing info") — the conversation is
             archived and stays in the chat list, so the undo is simply
             opening it again. Disabled while a turn is in flight because the
             server refuses then anyway; showing it live would offer an action
             that can only fail. -->
        <!-- ent#547: the Agent-details button is GONE from the header. The
             agent's context is the rail's Info tab now (operator, 2026-09-07 —
             a header-launched sibling panel "reads as one more top-level
             thing"), so the door is the rail strip, beside Work, Loops, Canvas
             and Files. ent#523's reasoning above it — "the band is numbers;
             this is the door to everything else" — still holds; only the door's
             location moved. -->
        <button
          v-if="isMainChat"
          class="px-2.5 py-1.5 rounded-lg text-xs font-medium text-gray-500 hover:text-gray-800 dark:text-gray-400 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-800 transition disabled:opacity-40 disabled:cursor-not-allowed"
          :disabled="sending || resetting || voiceCallActive"
          :title="voiceCallActive ? 'End the call, then reset' : (sending ? 'Wait for the current reply, then reset' : 'Archive this conversation and start the agent cold')"
          data-testid="portal-reset-main"
          @click="onResetMain"
        >{{ resetting ? 'Resetting…' : 'Reset' }}</button>
        <!-- ent#547: ONE paperclip, and it is the composer's. This header
             control opened the rail's Files tab with the same glyph the
             composer uses to ATTACH — two paperclips a few hundred pixels
             apart doing different things (open a panel / pick a file). The
             rail strip still opens Files, and ent#524's drop-anywhere is
             untouched. -->
      </div>
    </header>

    <!-- ent#523: the agent's numbers, always visible under the header. A slot
         rather than a mount, for the reason the `#rail-strip` slot below is
         one: the shell owns which agent is on screen and the panel it opens,
         and the conversation should not grow a second opinion about either. -->
    <slot name="band" />

    <!-- ent#451: this user's chats with the agent, as tabs (OverflowTabs) —
         Main pinned first (ent#523), then most recent, the rest under
         "N more". Selecting one is an ordinary thread open through the shell.
         #2579: `draft` asks for the provisional "New chat" tab — either this
         is still an unsaved fresh start, or it was born here a moment ago and
         the list has not caught up yet. -->
    <PortalChatTabs
      :threads="threads"
      :agent-name="agent.name"
      :active-id="currentSessionId"
      :disabled="voiceCallActive"
      :draft="newChat || bornHere"
      @select="(t) => emit('open-thread', t)"
    />

    <!-- #2579: the shell's line under the strip (today: the admin-only notice
         that generated titles are not being generated). A SLOT rather than a
         prop for the reason the `#band` slot two blocks up is one — the shell
         owns every fact it carries, and this conversation should not grow a
         second opinion about them. It also keeps this PR's footprint in the
         header band to one line, which matters: two sibling PRs restructure
         exactly this region next. -->
    <slot name="notice" />

    <!-- ent#534: the call's one status line — what the orb is doing, or why
         the call ended when it ended other than by End. `aria-live` because
         the state changes with no keystroke. Renders nothing between calls. -->
    <div
      v-if="voiceCallActive || voiceEndNotice"
      class="shrink-0 flex items-center gap-2 px-3 sm:px-6 py-1.5 text-xs border-b border-gray-200 dark:border-gray-800 bg-gray-50 dark:bg-gray-950"
      role="status"
      aria-live="polite"
      data-testid="portal-voice-line"
    >
      <span
        class="w-1.5 h-1.5 rounded-full shrink-0"
        :class="voiceCallActive ? 'bg-action-primary-500 motion-safe:animate-pulse' : 'bg-gray-400'"
      ></span>
      <span class="min-w-0 truncate text-gray-700 dark:text-gray-200">
        {{ voiceCallActive ? voiceHeaderText : voiceEndNotice }}
      </span>
      <span v-if="voiceCallActive" class="hidden sm:inline text-gray-400 dark:text-gray-500">· End the call to switch chats · Esc ends</span>
      <button
        v-if="voiceCallActive"
        type="button"
        class="ml-auto shrink-0 underline hover:no-underline text-gray-500 dark:text-gray-400"
        data-testid="portal-voice-end"
        @click="endVoiceCall()"
      >End call</button>
      <button
        v-else
        type="button"
        class="ml-auto shrink-0 underline hover:no-underline text-gray-500 dark:text-gray-400"
        @click="voiceEndNotice = ''"
      >Dismiss</button>
    </div>

    <!-- Messages. The wrapper is the orb's positioned box (ent#534): while a
         call is on `VoiceOverlay` covers the thread region and the header,
         tabs and composer stay in view, inert. -->
    <div class="relative flex-1 min-h-0 flex flex-col">
    <VoiceOverlay :voice="voice" @end="endVoiceCall" />
    <div ref="scrollEl" class="flex-1 min-h-0 overflow-y-auto px-3 sm:px-6 py-5" @scroll.passive="onTranscriptScroll">
      <!-- #2540: a skeleton while the thread's history loads — the scanline is
           the chart motion, not a page's. Keyed on the VERDICT `historyLoaded`,
           never on `loadingHistory`: the session-adoption path re-runs
           loadThread with the transcript on screen, and an in-flight key would
           swap the transcript the user just watched arrive for a placeholder.
           The wrapper owns the footprint, so the swap never shifts (principle 4). -->
      <div class="max-w-[var(--ws-message-max,64rem)] mx-auto min-h-[10rem]">
      <PortalSkeleton v-if="!historyLoaded" variant="thread" />
      <div v-else class="space-y-6">

        <!-- Briefing (new-chat state) rendered by the parent via slot -->
        <slot v-if="!loadingHistory && messages.length === 0 && !sending" name="empty" />

        <!-- ent#523: a SYSTEM line — the platform speaking about the thread,
             not the agent speaking in it. Today its only author is Reset,
             naming where the previous conversation went. Rendered centred and
             muted, with no avatar and no rating control, precisely so it is
             not read as something the agent said. Its own branch rather than a
             variant of the assistant bubble: a bubble with the trimmings
             hidden would still be an agent turn to anyone reading the code. -->
        <!-- ent#534: a voice call's spoken rows fold into ONE collapsed block,
             keyed on the call id (`groupVoiceBlocks`), placed where the call
             started. Visibly spoken — no rating control, a mic glyph, the
             label the call wrote ("Voice call · N min", and how it ended). -->
        <!-- #2694: the window is counted in typed turns under a row ceiling.
             When the ceiling cut the OLD end, say so — a thread that silently
             starts mid-call is the very symptom the window fix removes. -->
        <p
          v-if="historyTruncated"
          :class="PLATFORM_LINE_CLASS"
          data-testid="portal-history-truncated"
        >Earlier messages in this chat aren't shown</p>
        <template v-for="(item, k) in threadItems" :key="item.kind === 'voice-call' ? `call-${item.callId}` : `m-${item.index}`">
        <details
          v-if="item.kind === 'voice-call'"
          class="rounded-xl border border-gray-200 dark:border-gray-800 bg-gray-50 dark:bg-gray-950"
          data-testid="portal-voice-call-block"
        >
          <summary class="cursor-pointer select-none flex items-center gap-2 px-3 py-2 text-xs text-gray-600 dark:text-gray-300">
            <svg class="w-3.5 h-3.5 shrink-0 text-gray-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 11a7 7 0 01-14 0m7 7v3m0-3a4 4 0 004-4V7a4 4 0 10-8 0v6a4 4 0 004 4z" /></svg>
            <span class="font-medium">{{ item.label }}</span>
            <span :class="META_INK_CLASS">· spoken</span>
          </summary>
          <div class="px-3 pb-3 space-y-3">
            <div
              v-for="(t, j) in item.turns"
              :key="t.id || j"
              :class="t.role === 'user' ? 'flex justify-end' : 'flex items-start gap-2.5'"
              data-testid="portal-voice-turn"
            >
              <PortalAvatar v-if="t.role !== 'user'" :name="agent.name" :avatar-url="agent.avatar_url" :size="24" class="mt-0.5" />
              <div
                v-if="t.role === 'user'"
                class="max-w-[85%] rounded-2xl rounded-br-md px-3.5 py-2 text-sm leading-relaxed whitespace-pre-wrap bg-action-primary-600 text-white"
              >{{ t.content }}</div>
              <div v-else class="max-w-[85%]">
                <PortalAgentBubble :content="t.content" />
              </div>
            </div>
          </div>
        </details>
        <div v-else>
        <p
          v-if="item.message.role === 'system'"
          :class="PLATFORM_LINE_CLASS"
          data-testid="portal-system-line"
        >{{ item.message.content }}</p>
        <div v-else :class="item.message.role === 'user' ? 'flex justify-end' : 'flex items-start gap-2.5'">
          <PortalAvatar v-if="item.message.role !== 'user'" :name="agent.name" :avatar-url="agent.avatar_url" :size="28" class="mt-0.5" />
          <div v-if="item.message.role === 'user'" class="max-w-[85%] flex flex-col items-end gap-1">
            <div
              class="rounded-2xl rounded-br-md px-3.5 py-3 text-sm leading-relaxed whitespace-pre-wrap"
              :class="item.message.failed ? 'bg-status-danger-50 dark:bg-status-danger-900/30 text-status-danger-800 dark:text-status-danger-200 ring-1 ring-status-danger-300 dark:ring-status-danger-800' : 'bg-action-primary-600 text-white'"
            >{{ item.message.content }}</div>
            <!-- ent#551: a task the agent ran during a voice call lands as an
                 ordinary turn (it was not spoken, so it is not in the block);
                 the caption is the attribution. -->
            <p
              v-if="voiceTaskCaption(item.message)"
              class="text-[11px]"
              :class="META_INK_CLASS"
              data-testid="portal-voice-task-caption"
            >{{ voiceTaskCaption(item.message) }}</p>
            <p
              v-if="item.message.failed && item.message.error"
              class="text-xs text-status-danger-700 dark:text-status-danger-300 text-right max-w-[32ch]"
            >{{ item.message.error }}</p>
            <button
              v-if="item.message.failed && item.message.retryable !== false"
              class="text-xs text-status-danger-600 dark:text-status-danger-400 hover:underline inline-flex items-center gap-1"
              @click="retry(item.index)"
            >
              <svg class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" /></svg>
              Not delivered · Retry
            </button>
          </div>
          <div v-else class="max-w-[85%]">
            <!-- #2515: the bubble, its markdown body, its stylesheet and both
                 copy controls are ONE component now. The rating lands in the
                 bubble's action row beside the message Copy — same row, one
                 line of controls under the answer they are about. -->
            <PortalAgentBubble :content="item.message.content">
              <!-- ent#366: one click, on the answer being judged. Only on a
                   PERSISTED agent message — a thumb needs a row to point at, and
                   a client-fabricated id would simply 404 against the ratings
                   route.
                   #2580: the gate STAYS, but it is no longer what delays a fresh
                   reply. Every path that produces an assistant row now carries
                   the persisted id with the text (`assistantRow`), so a reply is
                   rateable as it lands. The gate remains as the consumer's
                   refusal to act on an empty id — carry the flag and the
                   identifier together, and let the reader still check (the
                   2026-09-07 ledger rule). It is what correctly keeps the thumbs
                   off the one row that genuinely has no id: a reply delivered
                   when the server could not persist it. System lines and a voice
                   call's spoken turns are unrateable by a different route — they
                   never reach this branch at all. -->
              <PortalRating
                v-if="item.message.id"
                :agent-name="agent.name"
                target-kind="message"
                :target-id="item.message.id"
                :initial-rating="item.message.myRating"
              />
            </PortalAgentBubble>
          </div>
        </div>
        </div>
        </template>

        <!-- ent#525: the live execution card under the message that started
             the job (ent#457's card, as ruled). Status, elapsed, the stream's
             last line as the current step (ent#286), the steps of a pipeline
             with its holder, delegated children found by THIS chat, Stop
             (ent#155) and Open in Work. The feed's row wins once it has the
             turn — matched by execution id, never "latest running". -->
        <div v-if="sending" class="flex items-start gap-2.5">
          <PortalAvatar :name="agent.name" :avatar-url="agent.avatar_url" :size="28" class="mt-0.5" />
          <PortalWorkCard
            :item="liveCardItem"
            :live-step="liveStepLine"
            :elapsed-seconds="elapsed"
            :children="liveChildren"
            :can-stop="canCancelTurn"
            :stopping="cancelling"
            show-open-in-work
            @stop="cancelTurn"
            @open-work="emit('open-work')"
          />
        </div>
        <!-- A turn that failed, timed out, was stopped or was lost sight of
             keeps its card (AC 3): rendered FROM the durable verdict the
             thread carries (#2320's outcome record), so it survives a reload
             exactly as the red message does. Success collapses into the
             reply — the reply IS the outcome. "Ask about it" is a prefill. -->
        <div v-else-if="terminalCardItem" class="flex items-start gap-2.5" data-testid="portal-work-terminal">
          <PortalAvatar :name="agent.name" :avatar-url="agent.avatar_url" :size="28" class="mt-0.5" />
          <PortalWorkCard
            :item="terminalCardItem"
            show-open-in-work
            @ask-about-it="askAboutIt"
            @open-work="emit('open-work')"
          />
        </div>

        <!-- ent#365: what this conversation actually produced, at the end of
             the thread where the newest turns are. Inside the scroll region,
             not pinned above the composer like the ent#364 asks — an ask is
             waiting on you, a deliverable is something to read. -->
        <PortalDeliverables
          :agent-name="agent.name"
          :session-id="currentSessionId"
          :refresh-key="deliverableTick"
        />
      </div>
      </div>
    </div>
    <PortalJumpToLatest :show="showJumpToLatest" :count="unreadBelow" @jump="scrollToLatest" />
    </div>

    <!-- ent#364: asks this agent raised, immediately above the composer — the
         third rendering of the SAME row the sidebar counts and the agent page
         shows, so answering here clears it in both. Directly above the input
         because it is a turn that is waiting on the person about to type. -->
    <div v-if="agentAsks.length" class="shrink-0 px-3 sm:px-6 pt-2">
      <div class="max-w-[var(--ws-message-max,64rem)] mx-auto">
        <PortalAsks
          :agent-name="agent.name"
          :current-session-id="currentSessionId"
          @open-thread="(t) => emit('open-thread', t)"
        />
      </div>
    </div>

    <!-- ent#474: the rail's mobile collapsed form — a strip above the
         composer, supplied by the shell, which owns the rail's tabs and
         signals. Renders nothing above `sm`, where the column beside the
         stage is the collapsed rail. -->
    <slot name="rail-strip" />

    <!-- Composer -->
    <div class="shrink-0 border-t border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900 px-3 sm:px-6 py-3">
      <div class="max-w-[var(--ws-message-max,64rem)] mx-auto">
        <p v-if="offline" class="mb-2 text-xs text-status-warning-600 dark:text-status-warning-400 flex items-center gap-1.5">
          <span class="w-1.5 h-1.5 rounded-full bg-status-warning-500"></span>
          You appear to be offline — messages will send once you're reconnected.
        </p>
        <!-- #2212: every voice failure says what happened here. Voice is an
             assist, never a blocker, so this is an inline notice next to a
             composer that still works — not a modal. -->
        <p
          v-if="voiceError"
          class="mb-2 text-xs text-status-danger-600 dark:text-status-danger-400 flex items-start gap-1.5"
          role="status"
          aria-live="polite"
        >
          <span class="mt-1 w-1.5 h-1.5 rounded-full bg-status-danger-500 shrink-0"></span>
          <span class="flex-1">{{ voiceError }}</span>
          <button
            type="button"
            class="shrink-0 underline hover:no-underline text-gray-500 dark:text-gray-400"
            @click="voiceError = ''"
          >Dismiss</button>
        </p>
        <!-- ent#155: a cancel that was REFUSED. Deliberately not `markFailed` —
             the turn is still running and still spending, so calling it failed
             would be the dishonest half of "honest status". -->
        <p
          v-if="cancelError"
          class="mb-2 text-xs text-status-danger-600 dark:text-status-danger-400 flex items-start gap-1.5"
          role="status"
          aria-live="polite"
        >
          <span class="mt-1 w-1.5 h-1.5 rounded-full bg-status-danger-500 shrink-0"></span>
          <span class="flex-1">{{ cancelError }}</span>
          <button
            type="button"
            class="shrink-0 underline hover:no-underline text-gray-500 dark:text-gray-400"
            @click="cancelError = ''"
          >Dismiss</button>
        </p>
        <!-- ent#523 AC 10: an agent that cannot take a message says so BEFORE
             the person types a paragraph into it. A label, never a disabled
             input — disabling relocates the dead state rather than removing it,
             and a client whose agents are all stopped (a routine resource-saving
             posture) would get an entirely inert Workspace. The message still
             sends; the server's own refusal stays the authority. -->
        <p
          v-if="availabilityNotice"
          class="mb-2 text-xs text-status-warning-700 dark:text-status-warning-300"
          data-testid="portal-availability-notice"
        >{{ availabilityNotice.message }}</p>
        <!-- ent#524: one chip per file, each with its OWN progress and its own
             outcome — a batch has no shared verdict, which is what makes "one
             failure does not fail the batch" true rather than aspirational. A
             rejected file names itself and the limit it broke. -->
        <p v-if="batchNotice" class="mb-2 text-xs text-status-warning-700 dark:text-status-warning-300">{{ batchNotice }}</p>
        <div v-if="attachments.length" class="mb-2 flex flex-wrap gap-1.5">
          <span
            v-for="(f, i) in attachments"
            :key="i"
            class="inline-flex items-center gap-1 text-xs rounded-full pl-2.5 pr-1.5 py-1"
            :class="f.error
              ? 'bg-status-danger-50 dark:bg-status-danger-900/30 text-status-danger-700 dark:text-status-danger-300'
              : 'bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-300'"
            :title="f.error || f.name"
            data-testid="portal-attachment-chip"
          >
            <svg class="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15.172 7l-6.586 6.586a2 2 0 102.828 2.828l6.414-6.586a4 4 0 00-5.656-5.656l-6.415 6.585a6 6 0 108.486 8.486L20.5 13" /></svg>
            <span class="max-w-[10rem] truncate">{{ f.name }}</span>
            <svg v-if="attachmentState(f) === 'uploading'" class="w-3 h-3 animate-spin text-gray-400" viewBox="0 0 24 24" fill="none"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/></svg>
            <span v-else-if="attachmentState(f) === 'failed'" class="max-w-[16rem] truncate opacity-90">· {{ f.error }}</span>
          </span>
        </div>
        <!-- ent#534: the composer is visible but inert while a call is on —
             the orb has the conversation; typing resumes the moment it ends. -->
        <!-- ent#547: the inert class moved off the <form> and onto the WRAPPER
             around everything except the call toggle, and that split is the
             whole point rather than a tidy-up.
             The call control is a TOGGLE — the same button starts the call and
             ends it. Dropped inside a region carrying `pointer-events-none`, it
             would render in its active/pressed styling for the entire call and
             refuse the click that ends it: a control that is visibly live and
             does nothing, which is precisely the dead affordance this issue's
             own AC 5 forbids, manufactured by AC 4's move. Escape and the
             status line's "End call" would still work, but a button that looks
             pressable and is not is worse than no button.
             So: the toggle stays live, and the fields it sits beside go inert
             around it. -->
        <form
          :aria-disabled="voiceCallActive ? 'true' : undefined"
          @submit.prevent="send"
        >
          <input ref="fileInput" type="file" multiple class="hidden" @change="onPickFile" />
          <!-- #2662: ONE composer shell — the field on top, the controls in a
               row inside it, the model picker right-aligned beside Send.

               The border, fill and focus ring move OFF the textarea and onto
               this shell, which is what makes the controls read as being inside
               the field rather than parked around it. The ring is scoped to the
               FIELD — `has-[textarea:focus]`, never `focus-within` — because
               `focus-within` lit the whole shell when an icon button was merely
               tabbed onto, and drew a second ring concentric with the model
               picker's own. `portalComposerAlignment.spec.js` asserts the
               absence of `focus-within:` here, so this is not a preference. The
               textarea keeps `block w-full` (#2259 — an inline-block textarea
               reserves a descender line box its wrapper then inherits) and goes
               transparent and borderless; it must never regain `rounded-2xl`
               or a background, or there are two nested boxes.

               This shape is also what finally fixes the narrow composer. In the
               single-row layout every 44px button came out of the field's
               width: at 375px the textarea measured 143px and wrapped a
               placeholder over four lines, and adding the model picker to that
               row was what left 34px in ent#403 (hence its own-row placement,
               and hence this issue). Stacked, the field takes the full shell at
               every width and the controls have a row of their own to spend.

               Two consequences of the chrome living HERE rather than on the
               textarea, both of which the first cut of this shape got wrong:

               (a) The visible box is now bigger than the field, so a click on
               the 8px padding band or on the control row's ground landed on
               <body> — where before the shell existed the box WAS the textarea
               and a click anywhere in it put the caret in. `focusComposerFromShell`
               puts that back.

               (b) The chrome is CONDITIONAL on the call, not static. ent#547's
               two inert regions dim the contents, but this element is the parent
               of both and cannot join them: the call toggle lives inside it and
               must stay at full contrast, and `opacity` on a parent is not
               something a child can undo. So the border and fill are REMOVED for
               the call's duration rather than dimmed — the composer recedes to
               the page ground, the one live control stays bright. Removed and
               not muted because a muted pair would be four more raw-gray classes
               in a file whose baseline this issue's AC says must not grow, while
               `border-transparent`/`bg-transparent` cost none.

               BOTH arms are bound and the static class carries no chrome colour
               at all. That is not tidiness — it is the fix for a bug this had on
               its first cut. Leaving `border-transparent bg-transparent` static
               and binding only the resting pair renders a light composer with NO
               border: Tailwind emits `.border-transparent` AFTER `.border-gray-300`
               (so transparent wins) but `.bg-transparent` BEFORE `.bg-white` (so
               white wins), and the two utilities therefore disagree about which
               of an equal-specificity pair survives. Dark hid it, because every
               `dark:` variant is emitted after both. Mutually exclusive arms have
               no ordering to get wrong. -->
          <div
            class="rounded-2xl border px-2 py-2 transition has-[textarea:focus]:border-action-primary-600 dark:has-[textarea:focus]:border-action-primary-500 has-[textarea:focus]:ring-[3px] has-[textarea:focus]:ring-action-primary-500/40 dark:has-[textarea:focus]:ring-action-primary-400/40"
            :class="voiceCallActive ? 'border-transparent bg-transparent' : 'border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-800'"
            @click="focusComposerFromShell"
          >
            <!-- ent#392's anchor, unchanged in job and in ref name (the
                 outside-click close reads `composerWrap`). It sheds `flex-1
                 min-w-0` because it is no longer a flex item competing with
                 buttons — it is the shell's first row and simply full width. -->
            <div ref="composerWrap" class="relative" :class="voiceCallActive ? 'opacity-60 pointer-events-none' : ''">
              <PortalTypeahead
                v-if="typeaheadOpen"
                :kind="typeaheadKind"
                :rows="typeaheadRows"
                :active-index="activeIndex"
                :overflow="typeaheadBound.overflow"
                :hidden-count="typeaheadHidden"
                :empty-message="typeaheadEmpty || ''"
                @pick="acceptActive"
                @hover="activeIndex = $event"
              />
              <textarea
                ref="textarea"
                v-model="input"
                rows="1"
                :placeholder="composerPlaceholder"
                :disabled="voiceCallActive"
                class="block w-full resize-none border-0 bg-transparent text-sm text-gray-900 dark:text-gray-100 px-2 py-2 leading-6 focus:outline-none focus:ring-0 max-h-40"
                @input="onComposerInput"
                @keydown="onComposerKeydown"
                @click="onComposerCaret"
                @select="onComposerCaret"
              ></textarea>
            </div>
            <!-- ent#547: the call toggle stays LIVE while everything else goes
                 inert, and the split is the point rather than a tidy-up. The
                 same button starts and ends the call; inside a region carrying
                 `pointer-events-none` it would render pressed for the whole
                 call and refuse the click that ends it — a control that looks
                 live and does nothing. Two inert regions now, because the
                 stacked layout puts the field and the other controls on
                 different rows and `opacity` needs a real box on each. -->
            <div class="mt-1 flex items-center gap-1">
              <button
                v-if="voiceEntry.render"
                type="button"
                class="shrink-0 h-11 w-11 flex items-center justify-center rounded-xl transition disabled:opacity-40 disabled:cursor-not-allowed"
                :class="voiceCallActive ? 'bg-action-primary-100 dark:bg-action-primary-900/40 text-action-primary-600 dark:text-action-primary-300' : 'text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-750'"
                :disabled="!voiceEntry.enabled || voiceStarting"
                :title="voiceCallActive ? 'End the voice call (Esc)' : (voiceEntry.enabled ? 'Start a voice call' : voiceEntry.reason)"
                :aria-label="voiceCallActive ? 'End the voice call' : (voiceEntry.enabled ? 'Start a voice call' : voiceEntry.reason)"
                :aria-pressed="voiceCallActive"
                data-testid="portal-voice-call"
                @click="voiceCallActive ? endVoiceCall() : startVoiceCall()"
              >
                <svg v-if="voiceCallActive" class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 6h12v12H6z" /></svg>
                <svg v-else class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 10v4m4-7v10m4-7v4M4 12h.01M20 12h.01" /></svg>
              </button>
              <div class="flex-1 min-w-0 flex items-center gap-1" :class="voiceCallActive ? 'opacity-60 pointer-events-none' : ''">
                <button
                  type="button"
                  class="shrink-0 h-11 w-11 flex items-center justify-center rounded-xl text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-750 transition"
                  title="Attach a file for the agent"
                  :disabled="voiceCallActive"
                  @click="fileInput?.click()"
                >
                  <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15.172 7l-6.586 6.586a2 2 0 102.828 2.828l6.414-6.586a4 4 0 00-5.656-5.656l-6.415 6.585a6 6 0 108.486 8.486L20.5 13" /></svg>
                </button>
                <button
                  v-if="sttSupported"
                  type="button"
                  class="shrink-0 h-11 w-11 flex items-center justify-center rounded-xl transition disabled:opacity-50"
                  :class="listening ? 'text-status-danger-600 dark:text-status-danger-400 animate-pulse' : 'text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-750'"
                  :title="micTitle"
                  :aria-label="micTitle"
                  :aria-pressed="listening"
                  :disabled="transcribing || voiceCallActive"
                  @click="toggleMic"
                >
                  <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 11a7 7 0 01-14 0m7 7v3m0-3a4 4 0 004-4V7a4 4 0 10-8 0v6a4 4 0 004 4z" /></svg>
                </button>
                    <!-- Right cluster: the model picker, then Send. `ml-auto`
                         rather than a spacer element, and `min-w-0` so the picker
                         is the thing that truncates when the row runs out — never
                         Send, which is `shrink-0`. -->
                <div class="ml-auto flex items-center gap-1 min-w-0">
                  <!-- `@keydown.enter.prevent` is the price of moving the picker
                       INSIDE the <form>. On dev it was a sibling above it, so Enter
                       there did nothing; inside, Chrome and Firefox route Enter on a
                       focused <select> to the form's default button, and a user who
                       arrows to another model and presses Enter to commit the choice
                       sends their unfinished draft instead. Preventing it costs
                       nothing: on every engine whose picker is drawn by the platform
                       the open dropdown never dispatches here, so the only page-level
                       effect of Enter on this control was the submit. -->
                  <BaseSelect
                    v-if="modelControl.render"
                    v-model="selectedModel"
                    variant="ghost"
                    class="min-w-0 max-w-[17rem]"
                    :disabled="voiceCallActive || !modelControl.enabled"
                    :title="modelControl.reason || 'Which model this chat runs on'"
                    aria-label="Model for this chat"
                    data-testid="portal-model-picker"
                    @keydown.enter.prevent
                  >
                    <option :value="INHERIT_VALUE">{{ modelDefaultText }}</option>
                    <option
                      v-for="opt in modelControl.options"
                      :key="opt.id"
                      :value="opt.id"
                      :title="optionTitle(opt)"
                    >{{ optionText(opt) }}</option>
                  </BaseSelect>
                  <button
                    v-if="canCancelTurn"
                    type="button"
                    @click="cancelTurn"
                    :disabled="cancelling"
                    class="shrink-0 h-11 w-11 flex items-center justify-center rounded-xl bg-status-danger-600 hover:bg-status-danger-700 text-white disabled:opacity-40 transition"
                    :title="cancelling ? 'Stopping…' : 'Stop this turn (Esc)'"
                    aria-label="Stop this turn"
                  >
                    <svg v-if="cancelling" class="w-5 h-5 animate-spin" fill="none" viewBox="0 0 24 24">
                      <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                      <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"></path>
                    </svg>
                    <svg v-else class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><rect x="7" y="7" width="10" height="10" rx="1.5" stroke-width="2" /></svg>
                  </button>
                  <button
                    v-else
                    type="submit"
                    class="shrink-0 h-11 w-11 flex items-center justify-center rounded-xl bg-action-primary-600 hover:bg-action-primary-700 text-white disabled:opacity-40 disabled:hover:bg-action-primary-600 transition"
                    :disabled="sending || !input.trim() || voiceCallActive"
                    title="Send"
                  >
                    <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 12h14M12 5l7 7-7 7" /></svg>
                  </button>
                </div>
              </div>
            </div>
          </div>
        </form>
      </div>
    </div>

    <audio ref="audioEl" class="hidden" @ended="onNarrationDone" @error="onNarrationDone"></audio>
  </div>
</template>

<script setup>
import { ref, computed, watch, nextTick, onMounted, onBeforeUnmount } from 'vue'
import { useClientPortalStore } from '@/stores/clientPortal'
import { agentDisplayName } from '@/utils/agentName'
import PortalAgentBubble from './PortalAgentBubble.vue'
import PortalWorkCard from './PortalWorkCard.vue'
import { usePortalWorkStore } from '@/stores/portalWork'
import { askAboutItPrefill, childrenForChat, itemById } from './portalWork'
import PortalAvatar from './PortalAvatar.vue'
import PortalStarButton from './PortalStarButton.vue'
import PortalEditableTitle from './PortalEditableTitle.vue'
import PortalChatTabs from './PortalChatTabs.vue'
import { newChatHotkeyLabel, MAIN_TAB_LABEL, composerAvailabilityNotice, assistantRow, replyFromHistory, replyBaseline } from './portalUtils'
import { usePortalFileDrop, attachmentState } from '@/composables/usePortalFileDrop'
import { useStickToBottom } from '@/composables/useStickToBottom'
import PortalTypeahead from './PortalTypeahead.vue'
import PortalJumpToLatest from './PortalJumpToLatest.vue'
import PortalAsks from './PortalAsks.vue'
import PortalDeliverables from './PortalDeliverables.vue'
import PortalSkeleton from './PortalSkeleton.vue'
import { workSignalFrom } from './portalRail'
import PortalRating from './PortalRating.vue'
import {
  deliveryFailureReason,
  mentionedAgents,
  resolveWaitBudgetMs,
  applyTypeaheadInsert,
  boundCandidates,
  buildMentionToken,
  clampActiveIndex,
  detectTypeaheadTrigger,
  dismissAfterInsert,
  filterAgentCandidates,
  filterPlaybookCandidates,
  hiddenPlaybookCount,
  playbookSearchSource,
  isSuppressed,
  nextActiveIndex,
  nextDismissState,
  resolveComposerGrowth,
  resolveComposerKey,
  starterFor,
  typeaheadEmptyMessage,
  MIN_RECORDING_BYTES,
  RECORDING_TOO_SHORT_MESSAGE,
  SPEECH_START_TIMEOUT_MS,
  TRANSCRIPT_EMPTY_MESSAGE,
  TTS_FAILED_MESSAGE,
  recorderErrorMessage,
  resolveMicMode,
  resolveRecordingMimeType,
  speechAttemptOutcome,
  speechErrorMessage,
  transcriptionErrorMessage,
} from './portalUtils'
import { shouldCancelOnEscape, shouldEndCallOnEscape, restoreDraft, cancelOutcome, isNoopCancel } from '../../utils/turnCancel'
// ent#534: the voice CALL — the platform's real-time orb, in this chat. Its
// rules live in their own pure module (vitest runs `environment: 'node'` with
// no mount harness, so a rule kept in here is a rule no test can reach); this
// component is the dispatcher over them and the composable.
import VoiceOverlay from '../chat/VoiceOverlay.vue'
import { useVoiceSession } from '../../composables/useVoiceSession'
import {
  VOICE_UNAVAILABLE_FALLBACK,
  endedNotice,
  groupVoiceBlocks,
  startFailureReason,
  voiceEntryState,
  voiceHeaderLine,
  voicePreflight,
  voiceTaskCaption,
} from './portalVoiceMode'
// ent#403: the model choice's rules, in their own pure module for the same
// reason voice mode's are — nothing rendered is reachable from vitest here.
import BaseSelect from '../base/BaseSelect.vue'
import { useUserPreferencesStore } from '@/stores/userPreferences'
import { PREF_KEYS } from '@/utils/gridStorageKeys'
import {
  INHERIT_VALUE,
  defaultOptionText,
  modelControlState,
  optionText,
  optionTitle,
  shouldClearChoice,
  storedFor,
  withChoice,
} from './portalModelChoice'

const props = defineProps({
  // `stt_available` (#2212) is the platform's ability to transcribe server-side
  // (an ElevenLabs key resolves) — a different fact from `voice_available`,
  // which additionally needs an effective voice to speak WITH.
  agent: { type: Object, required: true },      // {name, owner, avatar_url, description, playbooks, voice_available, stt_available}
  roster: { type: Array, default: () => [] },
  sessionId: { type: String, default: null },   // current thread, or null for a new chat
  // ent#451: a null `sessionId` alone cannot say WHICH kind of "no thread" this
  // is — an unresolved one (deep link, refresh) or a deliberately fresh one.
  // This is the second bit that makes them distinguishable, mirroring the
  // backend's `new_thread`.
  newChat: { type: Boolean, default: false },
  prefill: { type: String, default: '' },
  // ent#359: whether the CURRENT thread is starred. Owned by the shell (it
  // holds the per-viewer chat state), rendered here.
  starred: { type: Boolean, default: false },
  // ent#451: the shell's thread list — the tab strip above the thread is this
  // agent's slice of it, and the header's title is the active thread's.
  threads: { type: Array, default: () => [] },
  // ent#473: async (thread, title) => void, or null when renaming is unavailable.
  rename: { type: Function, default: null },
})
// ent#547: `open-files` and `open-details` are gone with the two header
// controls that raised them — the rail strip is the door to both now.
// Declared emits are the component's contract, so a name left here after
// its only `$emit` is deleted is a promise nothing keeps.
const emit = defineEmits(['switch-agent', 'session-adopted', 'sessions-changed', 'open-menu', 'toggle-star', 'escalate-to-room', 'open-thread', 'work-state', 'open-work', 'new-chat', 'main-reset', 'voice-call', 'voice-panel'])

// ent#451/#473: the active thread as the shell's list knows it. Null until the
// list carries the thread (a just-adopted session lands on the next refresh),
// and the header shows no title rather than a guessed one meanwhile.
const currentThread = computed(() => (props.threads || []).find(
  (t) => t && !t.is_room && (t.id || t.session_id) === currentSessionId.value,
) || null)
const currentTitle = computed(() => (currentThread.value?.title || '').trim())
const newChatHotkey = newChatHotkeyLabel(typeof navigator !== 'undefined' ? navigator.platform : '')
async function saveTitle(title) {
  if (!props.rename || !currentThread.value) return
  await props.rename(currentThread.value, title)
}

const store = useClientPortalStore()
// ent#364: one list, filtered — never a second fetch for this surface.
// `.name`, not the object (ent#429): `agent` is `{name, owner, ...}` and
// `asksForAgent` compares against `a.agent_name`, so passing the object matched
// nothing and this surface — the third of the three ent#364 promises — had never
// rendered. It failed SILENTLY, as an empty list is a legitimate state.
const agentAsks = computed(() => store.asksForAgent(props.agent.name))
const messages = ref([])
const currentSessionId = ref(props.sessionId)

// #2579 — "this thread was born in THIS mounted conversation, and the list may
// not know it yet". It bridges a real gap rather than duplicating
// `props.newChat`: the shell flips `startingNewChat` off the instant it hears
// `session-adopted`, which is BEFORE the refreshed thread list arrives, so
// without this the provisional tab vanishes for a round trip — or, when this
// was the agent's only chat, the whole strip does.
//
// It resets by construction on any real thread switch (`convGen` bumps and this
// instance is replaced) and deliberately survives adoption, because
// `onSessionAdopted` does NOT bump `convGen`.
const bornHere = ref(false)

// The ONE adoption seam (#2579). Three call sites emit `session-adopted` —
// streaming, the synchronous `/chat` fallback, and the voice path's
// `createSession`. Setting the flag at one of them is how the tab would vanish
// exactly when streaming is unavailable (the asymmetry ent#451's own spec
// exists to catch), or for the whole round trip of starting a call.
function adoptSession(id) {
  currentSessionId.value = id
  bornHere.value = true
  emit('session-adopted', id)
}

// #2579 — spend the flag the moment the list catches up. Display is identical
// either way (the real row now carries the active id, so `agentChatTabs`
// inserts nothing), but a flag that outlives its purpose is one refactor away
// from labelling a real conversation "New chat".
watch(() => props.threads, (list) => {
  if (!bornHere.value || !currentSessionId.value) return
  const id = currentSessionId.value
  if ((list || []).some((t) => !t.is_room && (t.id || t.session_id) === id)) bornHere.value = false
})

function focusComposer() { textarea.value?.focus() }

const loadingHistory = ref(false)
// #2163 — "a verdict exists for this thread's history" (mirrors `onMounted`'s
// condition). Never goes false again on this instance, so the adoption-path
// refetch swaps messages in place with no beam; `convKey` remounts re-derive it.
const historyLoaded = ref(!(props.sessionId && !props.newChat))
// #2694: the history window is counted in typed turns and bounded by a row
// ceiling; true when the ceiling cut rows off the OLD end of this thread.
const historyTruncated = ref(false)
// The platform speaking ABOUT the thread — one look for the ent#523 system
// line and the #2694 window notice: centred, muted, no avatar, so neither is
// read as something the agent said. Dark meta text stops at gray-400 (the
// contract's ink floor).
const PLATFORM_LINE_CLASS = 'my-3 text-center text-xs text-gray-400 dark:text-gray-400'
// Meta ink for a label beside a message (the block's "· spoken", a task's
// "asked during a voice call"): tertiary in light, and gray-400 in dark — the
// dark ink ladder's floor for meta text is gray-400, never gray-500.
const META_INK_CLASS = 'text-gray-400 dark:text-gray-400'
const input = ref('')
const sending = ref(false)
// ent#523 — Reset, offered on Main only.
const resetting = ref(false)
// ent#523 AC 10 — the same pure rule the sidebar chip and the details header
// read, so the four surfaces cannot disagree about one agent.
const availabilityNotice = computed(() => composerAvailabilityNotice(props.agent))
const isMainChat = computed(() => {
  const id = currentSessionId.value
  if (!id) return false
  const row = (props.threads || []).find((t) => !t.is_room && (t.id || t.session_id) === id)
  return !!row?.is_main
})

// Archive this conversation and start the agent cold. No confirmation dialog
// (operator, 2026-09-06) — nothing is lost, and the archived chat is one click
// away in the tab strip the moment this returns.
//
// The shell owns what happens next (switching to the new Main, refreshing the
// list), so this emits rather than navigating: the conversation does not know
// its own route. A server refusal is surfaced through the same inline error the
// send path uses, with the server's own sentence — `turn_in_flight` and
// `reset_raced` need different words and only the server knows which happened.
async function onResetMain() {
  if (resetting.value || sending.value) return
  resetting.value = true
  cancelError.value = ''
  try {
    const result = await store.resetMainChat(props.agent.name)
    emit('main-reset', result)
  } catch (e) {
    const detail = e?.response?.data?.detail
    cancelError.value = (detail && typeof detail === 'object' ? detail.message : detail)
      || 'Could not reset this chat right now.'
  } finally {
    resetting.value = false
  }
}
// ent#155 — stopping an in-flight turn. The id arrives with the 202, so Stop is
// offered only once there is something to stop; a turn that fell back to the
// synchronous send has no id and correctly offers nothing.
const activeExecutionId = ref(null)
const pendingUserText = ref('')
const cancelling = ref(false)
const cancelError = ref('')
// Review finding: `terminate_portal_turn` writes the CANCELLED terminal, and
// the still-running background turn then finishes — `portal_chat` sees a
// non-success status, matches no `error_code` branch, and raises the generic
// `agent_error` 502. `deliver` reports failed, `send()` calls `markFailed`, and
// the user who pressed Stop sees their own message struck out in red under
// "Something went wrong while the agent was working on this."
// A cancel the user ASKED FOR is not a failure. The DURABLE half of that lives
// in the server classifier now (`category: "cancelled"`, ent#155 review NEW-1)
// — the argument for keeping it client-side was that the portal outcome path
// had no such category, and the answer was to add one rather than to accept a
// verdict that outlived the tab. This set remains for the WINDOW that verdict
// cannot cover: `terminate_execution` writes the CANCELLED CAS before
// `cancelPortalTurn()` resolves, so a poll landing in between reads a turn that
// has already been cancelled but has no recorded outcome yet. In-memory is the
// right lifetime for a race that closes in milliseconds; it is the wrong one
// for a verdict a reload re-reads.
const cancelledExecutionIds = ref(new Set())
// Survives `deliver`'s finally, which clears the live id — `send()` needs to
// know which execution the turn it is about to judge actually ran under.
const lastDeliveredExecutionId = ref(null)
const canCancelTurn = computed(() => sending.value && !!activeExecutionId.value)

// ---- ent#525: the live card and the terminal card ---------------------------
const workStore = usePortalWorkStore()
function lastUserText() {
  for (let i = messages.value.length - 1; i >= 0; i--) {
    if (messages.value[i].role === 'user') return messages.value[i].content
  }
  return ''
}
// The feed's row for THIS turn, by id; until the feed has read it, a
// synthetic item that says what is known (steps `undefined` = not read yet,
// which the card renders as nothing — never as "could not be read").
const liveCardItem = computed(() => {
  const fromFeed = itemById(workStore.now, activeExecutionId.value)
  if (fromFeed) return fromFeed
  return {
    id: activeExecutionId.value || 'pending',
    agent_name: props.agent.name,
    status: 'running',
    outcome: 'running',
    kind: 'turn',
    title: pendingUserText.value || lastUserText(),
    chat_id: currentSessionId.value,
    steps: undefined,
    can_stop: false,
  }
})
// Two clocks, one rule: while the stream is live its last line is the step.
const liveStepLine = computed(() => (
  streaming.value && liveActivity.value.length ? liveActivity.value[liveActivity.value.length - 1] : null
))
// Delegated work this turn handed on — found by the CHAT, not the agent.
const liveChildren = computed(() => childrenForChat(workStore.now, currentSessionId.value, activeExecutionId.value))
// The durable verdict the terminal card renders from (#2320's record, or this
// tab's own settle). Cleared by the next send and by a thread switch.
const terminalOutcome = ref(null)
const terminalCardItem = computed(() => {
  const o = terminalOutcome.value
  if (!o) return null
  // A RETRYABLE verdict means nothing reached the agent (#2320's "never
  // started"): no work ran, the red message with its Retry is the honest UI,
  // and a "Failed" card beside it would describe a job that never existed.
  // The card stands for work that ran or may have run.
  if (o.retryable === true) return null
  const msg = String(o.message || '')
  let outcome = 'failed'
  if (o.category === 'cancelled') outcome = 'cancelled'
  else if (o.category === 'lost' || /lost track|did not reply/i.test(msg)) outcome = 'lost'
  else if (/timed?\s*-?\s*out|timeout/i.test(msg)) outcome = 'timeout'
  return {
    id: o.execution_id || lastDeliveredExecutionId.value || 'last',
    agent_name: props.agent.name,
    status: outcome === 'cancelled' ? 'cancelled' : 'failed',
    outcome,
    kind: 'turn',
    title: lastUserText(),
    chat_id: currentSessionId.value,
    error: outcome === 'cancelled' || outcome === 'lost' ? null : (msg || null),
    steps: null,
    can_stop: false,
  }
})
// The card keeps every honest terminal — a cancel included, which the
// red-message path (`markLastUserTurnFailed`) deliberately does NOT mark.
// Kept beside that function rather than inside it: #2320's spec evaluates
// that function in isolation, and this state is the card's, not the row's.
function rememberVerdict(outcome) {
  if (!outcome || !outcome.message) { terminalOutcome.value = null; return }
  // The same tail rule as `markLastUserTurnFailed`: a verdict is shown only
  // under a user message still waiting for its reply. A verdict recorded by a
  // raise site that persisted no user row (the roster / availability refusals)
  // must not pin a card onto an EARLIER, answered turn.
  const last = messages.value[messages.value.length - 1]
  if (!last || last.role !== 'user') { terminalOutcome.value = null; return }
  terminalOutcome.value = outcome
  // ent#403: the self-heal is deliberately NOT run here (review, 2026-09-08).
  // `rememberVerdict` also fires on LOAD and on reattach, off the durable Redis
  // verdict — which lives 15 minutes (`TURN_OUTCOME_TTL_SECONDS`) and is cleared
  // only at the next dispatch or on a success. Clearing here therefore re-fired
  // on every reload inside that window: a user who re-picked a model after the
  // failure had the fresh choice wiped again on the next refresh, and written
  // through to the server for every device. The clear belongs on the settle of
  // a turn this tab actually sent (`settleDelivery`) — and a re-send is the only
  // way the loop this guards against can happen at all, so nothing is lost.
}

// "Ask about it": the ruled lesser control — a prefill, never a send.
function askAboutIt(item) {
  input.value = askAboutItPrefill(item)
  autoGrowAfterUpdate()
  nextTick(() => textarea.value?.focus())
}
// ent#524 — drop + batch, shared with the room and the rail's Files tab.
// `attachments` is the composable's entry list: the chips render straight from
// it, so per-file progress and per-file failure are the same object.
const {
  dragging: fileDragging,
  entries: attachments,
  batchNotice,
  addFiles,
  clear: clearAttachments,
  handlers: dropHandlers,
} = usePortalFileDrop((file) => store.uploadDocument(props.agent.name, file))
const offline = ref(typeof navigator !== 'undefined' && navigator.onLine === false)

const scrollEl = ref(null)
// #2624: an agent's reply settling must not move a transcript the reader is
// holding. The rule lives in the composable, shared with `PortalRoom` — the two
// surfaces had two copies of the same unconditional `scrollTop = scrollHeight`.
const {
  unread: unreadBelow,
  showJumpToLatest,
  onScroll: onTranscriptScroll,
  onArrive: onMessagesArrived,
  pinToBottom,
  scrollToLatest,
  reset: resetFollowing,
} = useStickToBottom(scrollEl)
const textarea = ref(null)
const fileInput = ref(null)
const pickerRef = ref(null)
const pickerOpen = ref(false)

// ent#392 — composer typeahead state. Three refs, no decisions: `trigger` is
// whatever the pure scanner last returned, `activeIndex` is the roving
// selection (-1 = nothing chosen, which is what makes Enter send), `dismissed`
// is the Esc sentinel.
const composerWrap = ref(null)
const typeaheadTrigger = ref(null)
const activeIndex = ref(-1)
const dismissed = ref(null)

// ---- Load history when the thread/agent changes -------------------------------
async function loadThread(sessionId) {
  loadingHistory.value = true
  messages.value = []
  historyTruncated.value = false
  terminalOutcome.value = null
  let inFlight = null
  let inFlightBudget = null
  let budgetReadAt = null
  let outcome = null
  try {
    const { sessionId: resolved, messages: msgs, inFlightExecutionId, inFlightWaitBudgetSeconds,
            lastTurnOutcome, truncated } =
      await store.fetchHistory(props.agent.name, sessionId || null)
    historyTruncated.value = truncated === true
    // #2214: the budget is the marker's REMAINING TTL, honest only from the
    // instant it was measured — stamp that instant beside the fetch, not when
    // the (possibly long) reattached stream later ends.
    budgetReadAt = Date.now()
    currentSessionId.value = sessionId || resolved || null
    // ent#366: `id` and the caller's OWN rating ride along, so a reload shows
    // the thumb they already gave.
    // #2580: through the shared mapper, which is what keeps this site and the
    // two live-turn sites agreeing about the row's shape. A user row keeps its
    // own role; only the assistant shape is shared.
    messages.value = (msgs || []).map((m) => (
      m.role === 'assistant'
        ? assistantRow(m)
        // ent#534: spoken rows and the call they belong to — folded by `threadItems`.
        : { role: m.role, content: m.content, id: m.id, myRating: m.my_rating || null,
            source: m.source || null, voiceCallId: m.voice_call_id || null }
    ))
    inFlight = inFlightExecutionId
    inFlightBudget = inFlightWaitBudgetSeconds
    outcome = lastTurnOutcome
  } catch { /* start empty */ }
  // #2624: opening a thread is an intent — it pins and re-arms, so a thread
  // always opens at the bottom however the previous one was left.
  finally { loadingHistory.value = false; historyLoaded.value = true; await pinToBottom() }

  // ent#286: a turn was still running when this client loaded — reattach to it
  // rather than showing a thread that looks finished. The user's message is
  // already in `messages` (persisted at dispatch), so what is missing is only
  // the "working" state and the reply.
  if (inFlight) { await reattach(inFlight, inFlightBudget, budgetReadAt); return }

  // #2320: nothing running, but the last turn on this thread failed. The user
  // message is on screen (persisted at dispatch, deliberately — ent#286) with
  // no reply after it and, before this, no explanation either: reopening the
  // thread showed a question the agent had silently ignored. The verdict
  // outlives the tab that sent it, so it is applied on load too, not only to
  // the client that happened to be watching.
  if (outcome) markLastUserTurnFailed(outcome)
  if (outcome) rememberVerdict(outcome)
}

// Apply a server verdict to the most recent user message. Used by the two
// surfaces that have no in-memory index for it — a fresh load and a reattach —
// where the row came from history rather than from this tab's own `send()`.
function markLastUserTurnFailed(outcome) {
  if (!outcome?.message) return
  // ent#155 review (NEW-1): a cancellation is not a failure, and the server now
  // says so durably (`category: "cancelled"`). Keying on that rather than on
  // `cancelledExecutionIds` is the whole point of the server-side fix — that set
  // lives in ONE tab's memory, so switching threads or reloading brought the red
  // "Something went wrong" back for something the person did on purpose. This
  // arm survives a reload because the verdict does.
  if (outcome.category === 'cancelled') return
  const last = messages.value[messages.value.length - 1]
  // Only ever the UNANSWERED tail. Two raise sites (`portal_chat`'s roster 404
  // and its availability refusal) fire BEFORE `_persist_user_turn`, so they
  // record a verdict while leaving no user row of their own. Walking back to
  // "the last user message" would then pin the failure onto an EARLIER turn
  // that was answered — reporting a successful exchange as failed, and offering
  // a Retry that re-sends it. Today both are gated earlier in
  // `start_portal_turn`, so no execution row exists to carry a verdict; that is
  // a property of the current call graph, not of this function, and it is the
  // kind of property that rots quietly. If nothing is waiting for a reply, say
  // nothing.
  if (!last || last.role !== 'user') return
  markFailed(messages.value.length - 1, last.content, outcome.message,
             { retryable: outcome.retryable === true })
}

// Rejoin a turn already in progress. The agent replays its buffered log before
// streaming live, so a client that reloaded sees what it missed.
// #2214: `budgetSeconds` is the server's remaining wait budget for that turn
// (the marker's TTL at read time) and `budgetReadAt` the instant it was read —
// together they bound the wait the same way a fresh dispatch's 202 budget does.
async function reattach(executionId, budgetSeconds, budgetReadAt) {
  if (sending.value) return
  sending.value = true
  streaming.value = true
  // ent#525 (review E3): a reattached turn is still a turn the person may
  // stop — without the id, `canCancelTurn` stayed false after every reload.
  activeExecutionId.value = executionId || null
  liveActivity.value = []
  elapsed.value = 0
  clearInterval(elapsedTimer)
  elapsedTimer = setInterval(() => { elapsed.value += 1 }, 1000)
  // The baseline is what is on screen right now: this client reloaded INTO a
  // running turn, so every assistant message it can see predates that turn.
  // Passing nothing made the poll's comparison false on every poll, so the
  // reply never rendered and the user had to reload a second time. #2694: the
  // baseline is the newest TYPED reply's identity (`replyBaseline`), not a count.
  const baseline = replyBaseline(messages.value)
  try {
    await store.streamPortalExecution(props.agent.name, executionId, onStreamEvent)
    const data = await awaitPersistedReply(currentSessionId.value, baseline,
                                           budgetSeconds, budgetReadAt, executionId)
    // #2320: this surface was the silent one. It checked ONLY for a reply, so a
    // turn that failed — or was lost — rendered nothing at all: no message, no
    // Retry, the spinner simply stopped. A client that refreshed mid-turn got
    // less than one that stayed, which is backwards.
    if (data?.failed) {
      markLastUserTurnFailed(data.outcome)
      rememberVerdict(data.outcome)
    } else if (data?.lost) {
      const verdict = {
        category: 'lost',
        message: data.idle
          ? 'The agent did not reply. Check the conversation in a moment.'
          : "Still no reply — we've lost track of this turn. It may still finish; check the conversation shortly.",
        retryable: false,
        execution_id: executionId,
      }
      markLastUserTurnFailed(verdict)
      rememberVerdict(verdict)
    }
    if (data?.response) {
      // #2580: `id` + `myRating` from the persisted row, so a reattached reply is
      // rateable the moment it lands rather than on the next load.
      messages.value.push(assistantRow({ content: data.response, id: data.id, my_rating: data.myRating }))
      // A reattached reply is still a reply the user just watched land, so it
      // has to announce itself like `deliver()` does. Without this the thread
      // keeps its server-side unread count and the sidebar badges the
      // conversation on screen.
      emit('sessions-changed', currentSessionId.value)
    }
  } catch { /* the reply lands in history on the next load */ }
  finally {
    sending.value = false
    streaming.value = false
    liveActivity.value = []
    activeExecutionId.value = null
    clearInterval(elapsedTimer)
    // #2624: a reply settling is an ARRIVAL, not an intent — this turn was
    // already running when the thread loaded, so the reader may well have
    // scrolled up while waiting for it.
    await onMessagesArrived(1)
  }
}

watch(() => [props.agent.name, props.sessionId], async ([, sid], [oldName]) => {
  // ent#534: a route-driven thread change (browser back, a deep link) cannot
  // be refused the way a click can — the call ends first, its transcript kept.
  if (voiceCallActive.value) await voice.stop()
  currentSessionId.value = sid
  resetTypeahead()
  // #2624: the outgoing thread's element is about to be replaced, so re-arm
  // WITHOUT scrolling it. Every branch below either loads a thread (which pins)
  // or empties the transcript, so both land at the bottom.
  resetFollowing()
  // ent#451: `newChat` is the deliberate-fresh-start signal, and it has to be
  // consulted BEFORE the agent-changed branch. Without it this read a changed
  // agent as "load that agent's history" and called `fetchHistory(name, null)`,
  // which the backend answers with the most-recent thread — so New chat with an
  // agent you had spoken to before resumed it, while New chat with the agent
  // you were already on correctly started fresh. The asymmetry was the tell.
  if (props.newChat && !sid) { messages.value = []; currentSessionId.value = null; return }
  if (props.agent.name !== oldName || sid) await loadThread(sid)
  else { messages.value = []; currentSessionId.value = null }   // brand-new chat
})

watch(() => props.prefill, (v) => {
  if (v) { input.value = v; resetTypeahead(); nextTick(() => { autoGrow(); textarea.value?.focus() }) }
})

onMounted(async () => {
  window.addEventListener('online', onNet)
  window.addEventListener('offline', onNet)
  document.addEventListener('click', onDocClick)
  document.addEventListener('keydown', onEscapeKeydown)
  window.addEventListener('resize', onViewportResize)
  if (props.prefill) input.value = props.prefill
  // ent#451: `newChat` also has to hold on FIRST paint — the picker mounts a
  // fresh conversation rather than updating one, so the watcher above never
  // runs for it.
  if (props.sessionId && !props.newChat) await loadThread(props.sessionId)
  else {
    messages.value = []
    // #2579 AC 2: New chat has to put the caret in the composer in the SAME
    // gesture that makes the tab appear. It has to happen here, in the fresh
    // instance: pressing New chat bumps `convGen`, which remounts this
    // component, so any focus set before the press is thrown away. A disabled
    // textarea (a live voice call) makes it a no-op by construction.
    if (props.newChat) nextTick(focusComposer)
  }
  autoGrowAfterUpdate()   // `props.prefill` was assigned above; wait for the patch
})
// Review finding: `overflow-y` is now pinned, so the height must be recomputed when
// the box REWRAPS — narrowing the window (or opening a drawer) makes a fitting draft
// taller, and a stale `hidden` would leave that text invisible and unscrollable
// where it previously scrolled. Cheap: one listener, only while mounted.
function onViewportResize() {
  autoGrow()
}

onBeforeUnmount(() => {
  window.removeEventListener('online', onNet)
  window.removeEventListener('offline', onNet)
  document.removeEventListener('click', onDocClick)
  document.removeEventListener('keydown', onEscapeKeydown)
  window.removeEventListener('resize', onViewportResize)
  cleanupVoice()
})

function onNet() { offline.value = navigator.onLine === false }
function onDocClick(e) {
  if (pickerOpen.value && pickerRef.value && !pickerRef.value.contains(e.target)) pickerOpen.value = false
  // Outside the composer AND its popup closes the typeahead WITHOUT arming the
  // Esc sentinel; a click inside the textarea recomputes via @click, and a click
  // on the popup's own padding or scrollbar must not close it. This also covers
  // the files drawer and the mobile nav, whose buttons live outside the wrapper.
  if (typeaheadTrigger.value && composerWrap.value && !composerWrap.value.contains(e.target)) closeTypeahead()
}

function pickAgent(a) {
  pickerOpen.value = false
  emit('switch-agent', a.name)   // mid-thread → parent starts a NEW chat with that agent (no carry-over)
}

// #2211: the composer's growth ceiling, matching the `max-h-40` class on the
// textarea (40 * 4px). Named so the class and the JS cannot drift apart.
const COMPOSER_MAX_PX = 160

function autoGrow() {
  const el = textarea.value
  if (!el) return
  el.style.height = 'auto'
  const { height, overflowY } = resolveComposerGrowth(el, COMPOSER_MAX_PX)
  el.style.height = height + 'px'
  el.style.overflowY = overflowY
}

// Review finding: `autoGrow()` reads the DOM, but Vue patches `v-model` on the NEXT
// microtask — so every call that follows a programmatic `input.value = ...` (send,
// clear, restore-on-failure, dictation, typeahead pick) measured the OLD content.
// Before this PR that only meant "did not resize"; now that `overflow-y` is managed
// explicitly it also means a taller value can be left clipped AND unscrollable. So
// programmatic mutations use this deferred form, and the direct `@input` path keeps
// the synchronous one (the DOM is already current there).
function autoGrowAfterUpdate() {
  nextTick(autoGrow)
}


// ---- ent#392: composer typeahead (`/` playbooks, `@` agents) -----------------
//
// A DISPATCHER. Every decision — what counts as a trigger, what is offered, what
// a key does, what a pick splices — lives in the pure exports of portalUtils.js,
// because `vitest` runs `environment: 'node'` with no component-mount harness,
// so a decision left here is a decision no test can reach.

const typeaheadKind = computed(() => typeaheadTrigger.value?.kind || '/')

const typeaheadResult = computed(() => {
  const t = typeaheadTrigger.value
  if (!t) return null
  if (t.kind === '/') {
    // #2213: search the SEARCHABLE set, not the card-bounded `playbooks` — the
    // latter stops at 24 (the hint-grid bound), which made every skill past it
    // unmatchable by name with nothing on screen saying so.
    return {
      kind: '/',
      enabled: true,
      ...filterPlaybookCandidates(playbookSearchSource(props.agent), t.query),
    }
  }
  return {
    kind: '@',
    ...filterAgentCandidates(props.roster, t.query, {
      exclude: [props.agent?.name],
      // #2128: with no rooms substrate an @mention is deliberately ordinary
      // text, so offering a picker for it is a dead-end affordance. The gate
      // lives in the pure filter, so it is tested rather than grepped.
      enabled: store.multiAgentChatAvailable,
    }),
  }
})

const typeaheadBound = computed(() => boundCandidates(typeaheadResult.value?.items || []))
// #2213: rows the popup could not RENDER are `typeaheadBound.overflow`; skills that
// never reached the client at all are this. Two different omissions, so the popup
// is told about them separately rather than adding them into one misleading number.
const typeaheadHidden = computed(() => {
  // Review finding: only on a BARE `/`. Mid-query the same number reads as "N more
  // results matching what you typed", which is false — these are entries that never
  // reached the browser and no amount of typing will surface them. On a bare trigger
  // it reads correctly as a property of the list.
  if (typeaheadResult.value?.kind !== '/') return 0
  if (typeaheadTrigger.value?.query) return 0
  return hiddenPlaybookCount(props.agent, playbookSearchSource(props.agent).length)
})

// The two empty conditions are NOT the same: a source with nothing in it shows
// one honest line, while a query that matches nothing CLOSES the popup (AC#6 —
// the user is writing "50/50", not picking). Restricting the line to a bare
// trigger keeps a playbook-less agent from floating a panel over the rest of the
// message as they keep typing.
const typeaheadEmpty = computed(() => {
  const t = typeaheadTrigger.value
  const r = typeaheadResult.value
  if (!t || !r || r.items.length || t.query !== '') return null
  return typeaheadEmptyMessage(r.kind, r)
})

// A computed, not an imperative ref: it self-heals when the roster refreshes,
// the capability flips, or playbooks arrive late.
const typeaheadOpen = computed(() => {
  const t = typeaheadTrigger.value
  const r = typeaheadResult.value
  if (!t || !r || r.enabled === false) return false
  if (isSuppressed(dismissed.value, t)) return false
  return typeaheadBound.value.visible.length > 0 || !!typeaheadEmpty.value
})

const typeaheadRows = computed(() => typeaheadBound.value.visible.map((c, i) => (
  typeaheadKind.value === '/'
    ? { key: `pb-${i}-${c.title}`, primary: c.title, secondary: c.description || '' }
    // The slug rides along beside the label: it IS the token, and teaching it is
    // half of why this exists.
    : { key: `ag-${c.name}`, primary: agentDisplayName(c), secondary: `@${c.name}` }
)))

// The selection has to follow the list. A stale index is DROPPED rather than
// clamped to a neighbour — "whatever is now at index 5" is not the row the user
// chose, and inserting it is how the wrong agent (or `@undefined`) ships.
watch(typeaheadBound, (b) => { activeIndex.value = clampActiveIndex(activeIndex.value, b.visible.length) })

// The only part of this change that reaches a user who does not already know the
// feature exists. `@` is advertised only with the capability — a placeholder
// promising something the build cannot do is the #2128 dead end in text form.
const composerPlaceholder = computed(() => {
  if (listening.value) return 'Listening…'
  const base = `Message ${agentDisplayName(props.agent)}…  ·  / for playbooks`
  return store.multiAgentChatAvailable ? `${base}  ·  @ to add an agent` : base
})

function closeTypeahead() {
  typeaheadTrigger.value = null
  activeIndex.value = -1
}

// ONLY Esc arms the sentinel. A click-outside, an agent switch or a capability
// flip closes without arming — otherwise "type @, click away to read something,
// click back, keep typing" stays suppressed until the @ is deleted.
function dismissTypeahead() {
  dismissed.value = nextDismissState(typeaheadTrigger.value)
  closeTypeahead()
}

// Called from every path that writes `input.value` PROGRAMMATICALLY — send(),
// the prefill watcher, the thread switch, and both dictation handlers. None of
// them fires an input event, so without this a sentinel armed while composing
// message 1 kills the popup for every later message that starts the same way: a
// feature that is dead for the rest of the session.
function resetTypeahead() {
  closeTypeahead()
  dismissed.value = null
}

// #2703 — an external client's Workspace has no `/ws` (portal token; the
// ticket mint is JWT-only), so the `agent_skills_changed` trigger never reaches
// it. Opening the `/` popup is the moment the playbook list is about to be
// read, so re-validate the active agent's briefing then — bounded to once a
// minute per agent, stale-while-revalidate (the list on screen is never
// blanked). On a platform session this is a cheap no-op most of the time,
// since the WS trigger already refreshed the card.
const TYPEAHEAD_REVALIDATE_MAX_AGE_MS = 60_000

function refreshTypeahead(el) {
  if (!el) return
  const wasOpen = !!typeaheadTrigger.value
  // Read the EVENT TARGET, never the v-model ref: reading the ref makes
  // correctness depend on Vue's internal listener ordering, which is true today
  // and an implementation detail.
  typeaheadTrigger.value = detectTypeaheadTrigger(el.value, el.selectionStart, el.selectionEnd)
  if (!typeaheadTrigger.value) activeIndex.value = -1
  if (!wasOpen && typeaheadTrigger.value?.kind === '/' && props.agent?.name) {
    void store.revalidateBriefing(props.agent.name, { maxAge: TYPEAHEAD_REVALIDATE_MAX_AGE_MS })
  }
}

function onComposerInput(e) {
  autoGrow()
  // A paste that happens to end in a token must not open a popup nobody asked
  // for, whose very next keystroke is Enter.
  if (e?.inputType === 'insertFromPaste' || e?.inputType === 'insertFromDrop') {
    closeTypeahead()
    return
  }
  refreshTypeahead(e?.target)
}

// The caret moves with no input event — a click, a drag-select — and accepting
// against bounds computed for where it used to be splices over the wrong text.
function onComposerCaret(e) { refreshTypeahead(e?.target) }
/**
 * #2662: click anywhere on the composer shell lands in the field. Guarded, not
 * unconditional — a click that already reached a control keeps its own effect,
 * and the typeahead is excluded by role because it picks on `mousedown` and the
 * click that follows would otherwise arrive here and steal the focus back.
 */
const SHELL_INTERACTIVE = 'button, select, textarea, input, a, [role="listbox"], [role="option"]'
function focusComposerFromShell(event) {
  if (voiceCallActive.value) return
  if (event.target?.closest?.(SHELL_INTERACTIVE)) return
  textarea.value?.focus()
}

function onComposerKeydown(e) {
  const length = typeaheadBound.value.visible.length
  switch (resolveComposerKey({
    key: e.key,
    shiftKey: e.shiftKey,
    ctrlKey: e.ctrlKey,
    metaKey: e.metaKey,
    altKey: e.altKey,
    isComposing: e.isComposing,
    keyCode: e.keyCode,
    open: typeaheadOpen.value,
    hasActive: activeIndex.value >= 0,
    hasCandidates: length > 0,
  })) {
    case 'move-down': e.preventDefault(); activeIndex.value = nextActiveIndex(activeIndex.value, 1, length); break
    case 'move-up': e.preventDefault(); activeIndex.value = nextActiveIndex(activeIndex.value, -1, length); break
    case 'accept': e.preventDefault(); acceptActive(activeIndex.value >= 0 ? activeIndex.value : 0); break
    case 'dismiss': e.preventDefault(); dismissTypeahead(); break
    case 'close': closeTypeahead(); break
    case 'send': e.preventDefault(); send(); break
    default: break
  }
}

function acceptActive(index) {
  const t = typeaheadTrigger.value
  const row = typeaheadBound.value.visible[index]
  if (!t || !row) return                  // never insert `@undefined`
  const insert = t.kind === '/' ? starterFor(row) : buildMentionToken(row.name)
  const { value, caret } = applyTypeaheadInsert(input.value, t, insert)
  input.value = value
  closeTypeahead()
  // A pick that lands mid-sentence leaves the caret inside the token it just
  // inserted, and the setSelectionRange() below fires a `select` that would
  // re-detect it — the popup reopening on top of its own successful choice.
  // Suppress exactly that token; editing it back re-arms.
  const settled = dismissAfterInsert(value, caret)
  if (settled) dismissed.value = settled
  nextTick(() => {
    const el = textarea.value
    // focus() BEFORE setSelectionRange(): Safari resets and scrolls the
    // selection when focusing a textarea that did not previously have focus.
    if (el) { el.focus(); el.setSelectionRange(caret, caret) }
    autoGrow()
  })
}

// ---- Attachments: upload to the agent inbox; the next turn sees them ----------
// ent#524: the whole selection, not `[0]`. Both the picker and the drop target
// funnel into `addFiles`, so there is one batch implementation and one place
// where a per-file outcome is decided.
function onPickFile(e) {
  const files = e.target.files
  const p = addFiles(files)
  e.target.value = ''
  return p
}

// ---- Send + resilient retry ---------------------------------------------------
let elapsedTimer = null
// ent#525: the card renders the clock (`formatElapsed`); the three-tier
// "Thinking… / Working on it… / Still working…" label went with the dots.
const elapsed = ref(0)

async function deliver(text) {
  terminalOutcome.value = null
  sending.value = true
  elapsed.value = 0
  clearInterval(elapsedTimer)
  elapsedTimer = setInterval(() => { elapsed.value += 1 }, 1000)
  const startedNew = currentSessionId.value === null
  try {
    // ent#286: stream the turn so tool activity is visible while it runs.
    //
    // The fallback to the synchronous send is allowed in exactly ONE case: the
    // DISPATCH itself failed, so no turn exists. Once dispatch returns, the turn
    // is running on the server and re-sending would run it a second time —
    // double spend, double side effects. Anything that goes wrong after that
    // point is a failure to WATCH the turn, and the answer is to go and read
    // its result, never to send it again.
    let data = null
    let started = null
    // Read before anything is dispatched: after the fact it is impossible to
    // tell this turn's reply from the previous one's.
    const baseline = await persistedReplyBaseline(currentSessionId.value)
    // ent#403: read the choice ONCE, here, so the streaming dispatch and its
    // synchronous fallback below run the same turn on the same model — a value
    // re-read between the two could differ if the record settled in between.
    const chosenModel = modelControl.value.enabled ? selectedModel.value : ''
    try {
      started = await store.startPortalChat(props.agent.name, text, currentSessionId.value,
                                            { newThread: props.newChat && !currentSessionId.value,
                                              model: chosenModel })
    } catch (dispatchErr) {
      // Nothing was created, so a retry is safe — but only retry when the
      // ROUTE is what failed. A 404/405 means an older backend without this
      // endpoint; a network error means the request never landed. Any other
      // status is the server's real answer about this turn ("the agent is
      // offline", "you are rate limited"), and re-asking synchronously just
      // earns the same answer a second time while the user waits twice as long
      // to hear it.
      const status = dispatchErr?.response?.status
      const routeMissing = status === 404 || status === 405 || !dispatchErr?.response
      if (!routeMissing) throw dispatchErr
      // eslint-disable-next-line no-console
      console.debug('[workspace] streaming route unavailable, using sync send', dispatchErr)
      data = await store.sendPortalChat(props.agent.name, text, currentSessionId.value,
                                        { newThread: props.newChat && !currentSessionId.value,
                                          model: chosenModel })
    }

    if (started) {
      // ent#155: this is the first moment a Stop is possible — before it there
      // is no turn to stop, and a control offered earlier would be a lie.
      activeExecutionId.value = started.execution_id || null
      lastDeliveredExecutionId.value = started.execution_id || null
      pendingUserText.value = text
      // Stamp the dispatch instant: the server's marker TTL starts here, so the
      // client's ceiling must too (see awaitPersistedReply).
      const dispatchedAt = Date.now()
      if (started.session_id && currentSessionId.value !== started.session_id) {
        // Adopt the thread NOW rather than at the end, so a refresh mid-turn
        // reattaches to it instead of opening a second conversation.
        adoptSession(started.session_id)
      }
      streaming.value = true
      liveActivity.value = []
      try {
        await store.streamPortalExecution(props.agent.name, started.execution_id, onStreamEvent)
      } catch (streamErr) {
        // Watching failed; the turn did not. Fall through and read its result.
        // eslint-disable-next-line no-console
        console.debug('[workspace] lost the stream, reading the result instead', streamErr)
      } finally {
        streaming.value = false
        liveActivity.value = []
      }
      data = await awaitPersistedReply(
        started.session_id || currentSessionId.value, baseline,
        started.wait_budget_seconds, dispatchedAt, started.execution_id,
      )
      if (data?.failed) {
        // #2320: the server diagnosed this turn. Say what it said, and offer a
        // Retry only where the server states nothing reached the agent — the
        // #2120/#2133 rule is about turns that MAY HAVE RUN AND BILLED, and a
        // verdict of "never started" is precisely the evidence that rule always
        // lacked.
        return { failed: true, error: data.outcome.message,
                 retryable: data.outcome.retryable === true,
                 // ent#403: the TOKEN, not the prose. `invalid_model` is the one
                 // verdict the client acts on rather than merely renders — it
                 // clears the stored choice. Matching on the sentence would
                 // break on the next copy edit.
                 category: data.outcome.category }
      }
      if (data?.lost && data.idle) {
        // The server reports nothing running, and offered no verdict either.
        // Distinct from the budget case below: nothing is still going, so
        // "it may still finish" would be a fabrication. Still no Retry —
        // `mark_turn_inflight` no-ops when Redis is down, so a perfectly
        // healthy, already-billed turn reaches this branch on its first poll.
        return { lost: true, retryable: false,
                 error: 'The agent did not reply. Check the conversation in a moment.' }
      }
      if (data?.lost) {
        // #2133: we ran out of budget while the marker still claimed a turn.
        // That turn probably RAN and was billed — we merely lost sight of it.
        // So this is reported without a Retry: re-sending is the one action
        // guaranteed to be wrong.
        return { lost: true, retryable: false, error: "Still no reply — we've lost track of this turn. It may still finish; check the conversation shortly." }
      }
      if (!data) {
        // Defensive only: every return above is enumerated. Kept non-retryable
        // because an unclassifiable outcome must take the unprivileged answer.
        // (Before #2320 this branch was unreachable AND retryable — #2150
        // believed it preserved a Retry for a genuine no-answer, but
        // `awaitPersistedReply` never returned null, so `idle` fell into the
        // lost-track message above instead. That is the bug #2320 reported.)
        return { lost: true, retryable: false,
                 error: 'The agent did not reply. Check the conversation in a moment.' }
      }
    }

    // #2580: same row shape, same id. On the streaming path `data` comes from
    // `awaitPersistedReply` and carries the persisted id; on the synchronous
    // fallback it comes from `POST .../chat`, which now returns `message_id`
    // (the server used to mint that id inline and throw it away). Either way the
    // gate below is the id itself, never a flag that rose before it existed —
    // the 2026-09-07 ledger rule.
    messages.value.push(assistantRow({
      content: data.response || '(no response)',
      id: data.id || data.message_id,
      my_rating: data.myRating,
    }))
    terminalOutcome.value = null   // ent#525: the reply IS the outcome
    // ent#534: never narrate over a live voice call — the orb owns playback
    // then (the speaker toggle is hidden for the call's duration).
    if (voiceMode.value && ttsEnabled.value && data.response
        && !voiceCallActive.value) speak(data.response)
    if (data.session_id && currentSessionId.value !== data.session_id) {
      adoptSession(data.session_id)
    }
    // ent#359: carry WHICH thread finished. The shell marks a completed turn
    // read only when the user is still looking at it — this event fires even
    // after the user has navigated away (the send is an async closure that
    // outlives the component), and without the id the shell cannot tell the two
    // apart, so it cleared the badge for a reply the user never saw.
    if (startedNew || currentSessionId.value) emit('sessions-changed', currentSessionId.value)
    // ent#365: a turn is the only thing that can produce a deliverable in this
    // chat, so the card list is re-read exactly then — no poll, and nothing to
    // refresh on a conversation nobody is talking in.
    deliverableTick.value += 1
    clearAttachments()
    return true
  } catch (err) {
    return { error: deliveryFailureReason(err) }
  } finally {
    sending.value = false
    clearInterval(elapsedTimer)
    activeExecutionId.value = null
    pendingUserText.value = ''
    cancelling.value = false
    // #2624: the reply landing is an arrival. The SEND that started this turn
    // already pinned and re-armed (below), so a reader who stayed at the bottom
    // still follows the answer down — and one who scrolled up mid-turn, to
    // re-read what they asked about, keeps their place. A long streaming reply
    // is the same story: nothing here moves the viewport while it grows, and
    // this settle is the only scroll it can cause.
    await onMessagesArrived(1)
  }
}

// ent#155: stop the turn, give the words back. The server keeps the cancel
// semantics the rest of the platform uses (CANCELLED, CAS-guarded), and the
// in-flight marker + resume lock are released by the turn's own `finally`, so
// there is nothing to unwind here.
// Escape stops the turn — but only when nothing else owns Escape. The rule
// itself is pure and lives in `utils/turnCancel.js`; what belongs here is the
// list of things on THIS surface that Escape would otherwise be dismissing.
// (`voiceMode` is a speak-replies TTS toggle, not an overlay. The ent#534 voice
// call is asked FIRST, below, and it is not on the overlay list because while a
// call is up there is no turn to cancel.)
function onEscapeKeydown(event) {
  // ent#534: while a call is on, Escape ends the call — the composer and the
  // picker are inert, so nothing on this surface competes for it.
  //
  // #2598: asked through the shared rule, not re-decided here. This branch used
  // to test `voiceCallActive.value && event.key === 'Escape'` and nothing else,
  // so it ran ABOVE `shouldCancelOnEscape` while reading none of its
  // preconditions — and #2582's overlays (the file preview, the delete confirm)
  // claim Escape in the capture phase with `preventDefault()` exactly so they
  // cannot destroy an in-flight turn. That protocol worked for the cancel rule,
  // which reads `defaultPrevented`, and was invisible to this one: the overlay
  // closed AND the call ended on a single keystroke. Both branches now start
  // from the same `ownsEscape` preconditions, so they cannot drift again.
  if (shouldEndCallOnEscape(event, { callActive: voiceCallActive.value })) {
    event.preventDefault()
    void endVoiceCall()
    return
  }
  if (!shouldCancelOnEscape(event, {
    inFlight: canCancelTurn.value,
    cancelling: cancelling.value,
    // Review finding NEW-3: this list was `[typeaheadOpen]` alone, narrower
    // than ChatPanel's for no reason anyone had argued. Two other things on
    // this surface own Escape while a turn can be in flight, and both are
    // reachable mid-turn:
    //
    //   - the agent picker (`pickerOpen`) closes on outside-click only, so a
    //     user pressing Escape to dismiss it killed the turn instead;
    //   - dictation (`listening`) is disabled only while `transcribing`, so
    //     the mic can be live during a turn and Escape is how you stop it.
    //
    // The module's own bias decides the direction: a missed cancel costs one
    // click on the Stop button, a wrong one destroys a turn the user is still
    // waiting for. So an overlay is added whenever it plausibly owns Escape,
    // not only when it provably does.
    overlays: [typeaheadOpen.value, pickerOpen.value, listening.value],
  })) return
  event.preventDefault()
  cancelTurn()
}

async function cancelTurn() {
  const executionId = activeExecutionId.value
  if (!executionId || cancelling.value) return
  cancelling.value = true
  cancelError.value = ''
  const restoreText = pendingUserText.value
  try {
    const res = await store.cancelPortalTurn(props.agent.name, executionId)
    const outcome = cancelOutcome({ ok: true, alreadyTerminal: isNoopCancel(res?.status) })
    if (outcome.kind === 'cancelled') {
      cancelledExecutionIds.value.add(executionId)
      input.value = restoreDraft(restoreText, input.value)
      autoGrowAfterUpdate()
      // ent#525: a stop the person asked for is an honest terminal the card
      // keeps ("Stopped by you") — recorded at the act, shown once the turn
      // settles. The red-message path deliberately never marks it.
      rememberVerdict({ category: 'cancelled', message: 'Stopped by you.', execution_id: executionId })
    }
  } catch (err) {
    // A 404 is the lost race (the row went terminal, or the agent no longer
    // holds the turn), not a refusal — say nothing.
    if (err?.response?.status === 404) {
      cancelling.value = false
      return
    }
    // Still running, still spending — say so and leave the composer alone.
    cancelError.value = cancelOutcome({ ok: false, alreadyTerminal: false }).message
    cancelling.value = false
  }
}

// ent#286 — live turn state. `liveActivity` holds a short, human-readable trail
// of what the agent is doing right now; it is transient and never persisted.
const streaming = ref(false)
const liveActivity = ref([])
// Bumped after each completed turn; `PortalDeliverables` watches it.
const deliverableTick = ref(0)
const LIVE_ACTIVITY_MAX = 6

// One log entry from the agent's stream → at most one line of visible activity.
// Deliberately conservative: the stream is Claude's raw log, so anything not
// recognised is ignored rather than rendered as noise at a client.
function onStreamEvent(evt) {
  if (!evt || evt.type === 'stream_end') return
  let label = null
  if (evt.type === 'tool_use' || evt.tool_name) label = `Using ${evt.tool_name || 'a tool'}…`
  else if (evt.type === 'thinking') label = 'Thinking…'
  else if (evt.type === 'error') label = 'Hit a problem — recovering…'
  if (!label) return
  if (liveActivity.value[liveActivity.value.length - 1] === label) return  // don't stutter
  liveActivity.value.push(label)
  if (liveActivity.value.length > LIVE_ACTIVITY_MAX) liveActivity.value.shift()
}

// The stream ends when the AGENT's execution ends, but the reply is persisted
// by the backend a moment later — and sometimes much later, because a failed
// `--resume` retries the whole turn cold under a NEW execution the client is
// no longer watching.
//
// So the wait is bounded by the SERVER's own answer, not by a stopwatch. While
// `in_flight_execution_id` is set on the thread, a turn is still running and
// the only correct thing to do is keep waiting; a fixed deadline here declared
// live, billed turns "not delivered" and offered a Retry that ran and billed
// them a second time.
//
// The baseline is read from the SERVER before dispatch (`persistedReplyBaseline`).
// Using the local list instead let a retry return the PREVIOUS turn's reply on
// its first poll — the answer to the wrong question, while a second turn ran unseen.
const REPLY_POLL_MS = 700
// #2694: how many of the newest rows the poll reads. The reply it waits for is
// the newest row of the thread (the composer is inert during a call and a call
// cannot start over a reply in flight), so a handful is enough; the server
// caps the parameter at 50.
const REPLY_POLL_ROWS = 8
// Time-based, NOT poll-count-based. With the backoff below, 8 polls is up to
// 8 x 15s = 120s late in a turn — so a count silently stretched this debounce
// into two minutes of spinner before the no-answer message appeared.
const REPLY_IDLE_GIVE_UP_MS = 6_000

// #2133/#2214: the absolute ceiling, for when the marker is ORPHANED rather
// than merely slow — a hard backend kill skips the `finally` that clears it,
// and `except Exception` does not catch `CancelledError`.
//
// The server owns the turn timeout (per-agent since #2214) and sends the budget
// with the 202 (`wait_budget_seconds`) and with the history response on
// reattach (`in_flight_wait_budget_seconds` — the marker's remaining TTL). The
// pick lives in portalUtils' `resolveWaitBudgetMs`: a positive server budget
// wins, anything else falls back to a literal frozen at the pre-#2214 server
// bound — see its comment for why that literal must never chase the new
// arithmetic.

// Polling every 700ms for ten minutes is ~850 history reads per tab. The reply
// almost always lands in the first seconds, so the fast interval is what
// matters; after that, widen. Same total wait, an order of magnitude fewer
// requests on the long tail.
const REPLY_POLL_STEPS = [
  { afterMs: 30_000, everyMs: 2_000 },
  { afterMs: 120_000, everyMs: 5_000 },
  { afterMs: 300_000, everyMs: 15_000 },
]

function replyPollInterval(elapsedMs) {
  let every = REPLY_POLL_MS
  for (const step of REPLY_POLL_STEPS) if (elapsedMs >= step.afterMs) every = step.everyMs
  return every
}

// #2320: `executionId` is the turn THIS call is waiting on. The outcome record
// is matched against it before it is believed — a thread can hold a verdict from
// an earlier turn, and reporting that one as this turn's failure would be a new
// way to lie about the same thing.
async function awaitPersistedReply(sessionId, baseline, budgetSeconds,
                                   dispatchedAtMs, executionId = null) {
  let idleSince = null
  // Measured from DISPATCH, not from when this function was reached. The
  // server's marker TTL starts ticking at dispatch, so a client clock that
  // starts after the stream breaks (which can be minutes later, on exactly the
  // orphaned-marker path this ceiling exists for) would outlive the marker —
  // and the marker's disappearance would be read as "nothing running" instead
  // of tripping the ceiling. The two clocks now share an origin.
  const startedAt = dispatchedAtMs || Date.now()
  const budgetMs = resolveWaitBudgetMs(budgetSeconds)
  const deadline = startedAt + budgetMs
  const wait = () => new Promise((r) => setTimeout(r, replyPollInterval(Date.now() - startedAt)))

  for (;;) {
    // `lost` — NOT a failure. The turn may well have run and been billed; we
    // simply stopped being able to see it. The caller must not offer a Retry.
    if (Date.now() > deadline) return { lost: true }
    let data
    try {
      // #2694: the NARROW read — the newest few rows, never the thread window.
      // This runs every 700 ms early in a turn; the window (100 typed turns
      // plus their calls) is the wrong thing to pay for here, and a count over
      // it is the wrong thing to compare (see `replyFromHistory`).
      data = await store.fetchHistory(props.agent.name, sessionId || null, { limit: REPLY_POLL_ROWS })
    } catch {
      // A hiccup reading history is not evidence the turn failed.
      await wait()
      continue
    }
    // #2580: `replyFromHistory` carries the persisted row's `id` and the
    // caller's own rating out with the text. They were always in hand here —
    // this reads the row the server WROTE — and were being dropped, which is
    // the whole of the "not rateable until reload" defect.
    const reply = replyFromHistory(data.messages, baseline)
    if (reply) return { ...reply, session_id: data.sessionId || sessionId }
    // #2320: the server told us how this turn ended. Authoritative regardless
    // of the marker — a verdict naming THIS execution means it is over — and
    // read before the idle timer so a diagnosed failure is reported at once
    // instead of after a 6s wait that pretends we do not know.
    const outcome = data.lastTurnOutcome
    if (outcome && executionId && outcome.execution_id === executionId) {
      return { failed: true, outcome }
    }
    // Still running server-side? Then keep waiting, however long it takes.
    if (data.inFlightExecutionId) { idleSince = null }
    else {
      if (idleSince === null) idleSince = Date.now()
      // The server has said "nothing running" for long enough. NOT retryable:
      // the turn may well have run and been billed — notably when Redis is down,
      // `mark_turn_inflight` no-ops and this branch is reached on the very first
      // poll of a perfectly healthy turn.
      else if (Date.now() - idleSince >= REPLY_IDLE_GIVE_UP_MS) return { lost: true, idle: true }
    }
    await wait()
  }
}

// The newest typed reply as the SERVER sees it — the baseline this turn's
// reply must differ from (#2694: by identity, read from the same narrow rows
// the poll reads, so the two can never disagree about the window).
async function persistedReplyBaseline(sessionId) {
  if (!sessionId) return replyBaseline([])
  try {
    const data = await store.fetchHistory(props.agent.name, sessionId, { limit: REPLY_POLL_ROWS })
    return replyBaseline(data.messages)
  } catch {
    return replyBaseline([])
  }
}

// Mark a sent message as undelivered, THROUGH the reactive array.
//
// `messages` is a ref([]), so pushing a plain object stores the raw target and
// Vue only proxies it when you read `messages.value[i]`. Mutating the local
// variable you pushed writes past the proxy: the value changes, nothing
// re-renders, and the failure is invisible.
function markFailed(index, content, error, { retryable = true } = {}) {
  const row = messages.value[index]
  // The array can be replaced under us (thread switch, history reload). Only
  // mark the row if it is still the message we sent.
  if (!row || row.role !== 'user' || row.content !== content) return
  row.failed = true
  row.error = error || null
  // #2133: a turn we merely lost sight of is NOT offered a Retry. Re-sending
  // a turn that already ran is the double-billing this whole path exists to
  // prevent, so the distinction lives on the row rather than in the copy.
  row.retryable = retryable
}

async function send() {
  const text = input.value.trim()
  if (!text || sending.value) return
  // The composer is about to be cleared programmatically, which fires no input
  // event — so the popup and its Esc sentinel are cleared here rather than left
  // armed against a message that no longer exists.
  resetTypeahead()

  // ent#361: @mentioning another agent from a 1:1 makes this a group discussion.
  // Handled BEFORE the message is appended: the conversation moves to a room, so
  // leaving a copy of it in this thread would show the user their message in two
  // places and only one of them would ever get a reply.
  //
  // Gated on the rooms capability for the same reason the picker is (#2128) —
  // without it there is nowhere to escalate TO, and an @mention has to keep
  // working as ordinary text.
  if (store.multiAgentChatAvailable) {
    const others = mentionedAgents(text, props.roster, { exclude: [props.agent.name] })
    if (others.length) {
      input.value = ''
      autoGrowAfterUpdate()
      emit('escalate-to-room', { agents: [props.agent.name, ...others], message: text })
      return
    }
  }

  input.value = ''
  autoGrowAfterUpdate()
  await submitUserText(text)
}

// The tail every user utterance shares, typed or spoken (ent#440). Extracted
// rather than cloned: a voice turn that appended its message a second way would
// be a second conversation wearing the same thread, which is the whole thing
// this feature exists not to be. Returns the outcome so a caller that is not a
// person watching the screen — the voice loop — can decide what to do next.
async function submitUserText(text) {
  // ent#491: the user's own activity is the ordering signal, so the bump happens
  // HERE — on send — and not when a reply lands. Any agent this message wakes
  // counts, mirroring the room fan-out (`unreadByAgent`): if you @mention two
  // agents, you just collaborated with both.
  try {
    const woken = [props.agent?.name, ...(props.agent
      ? mentionedAgents(text, props.roster, { exclude: [props.agent.name] })
      : [])].filter(Boolean)
    store.noteAgentInteraction(woken)
  } catch {
    // Ordering is a convenience; it must never be able to block a send.
  }
  const index = messages.value.push({ role: 'user', content: text, failed: false, error: null }) - 1
  // #2624: sending is an explicit intent to follow the bottom — it pins and
  // re-arms whatever the prior scroll position, so the reader is never handed
  // an unread badge for their own message.
  await pinToBottom()
  // A stale "couldn't stop the turn" must not outlive the turn it described.
  cancelError.value = ''
  const res = await deliver(text)
  return settleDelivery(index, text, res)
}

// Both `deliver()` callers have to settle a turn the same way, so they share
// one function rather than one of them carrying the rules. Review finding:
// `send()` grew the cancel-aware handling and `retry()` did not, so a cancelled
// RETRY still struck the message out in red with a Retry button — reproducing,
// on the other caller, the exact defect this change exists to remove — and a
// refused cancel's error stayed pinned above the composer across every later
// turn because only `send()` cleared it.
function settleDelivery(index, text, res) {
  if (res === true) return { ok: true }
  // #2320: `retryable` when `deliver` decided it (a server verdict, or a
  // give-up it enumerated); otherwise the pre-existing rule, which still covers
  // the one path `deliver` does not classify — a dispatch that threw, where
  // nothing reached the server and re-sending is correct.
  //
  // A cancel the user asked for is not a failure. `markFailed` would strike the
  // message out in red and offer a Retry for a turn they deliberately stopped —
  // and the words are already back in the composer.
  //
  // ent#440 merge: the outcome is RETURNED, because the caller is no longer
  // always a person watching the screen — the voice loop reads `{ok}` to decide
  // whether to keep listening. A cancel reports `ok: false` like any other
  // non-delivery (the turn did not produce an answer) but carries `cancelled`
  // so the loop can tell "the user stopped this" from "this broke".
  if (lastDeliveredExecutionId.value && cancelledExecutionIds.value.has(lastDeliveredExecutionId.value)) {
    cancelledExecutionIds.value.delete(lastDeliveredExecutionId.value)
    return { ok: false, cancelled: true }
  }
  markFailed(index, text, res?.error, { retryable: res?.retryable ?? !res?.lost })
  // ent#403: a turn the chosen model could not complete clears that choice, so
  // the server's "switched back to the agent's default" is true next turn.
  // AFTER `markFailed` deliberately: `turnCancel.spec.js` pins the adjacency of
  // the cancel check to `markFailed`, and that rule is the more important one.
  clearModelChoiceOnFailure(res)
  terminalOutcome.value = { category: res?.category || (res?.lost ? 'lost' : 'failed'),
                            message: res?.error || 'Something went wrong.',
                            retryable: res?.retryable ?? !res?.lost, execution_id: lastDeliveredExecutionId.value }
  return { ok: false, error: res?.error, lost: res?.lost }
}

async function retry(i) {
  const msg = messages.value[i]
  if (!msg || sending.value) return
  const content = msg.content
  msg.failed = false
  msg.error = null
  // A stale "couldn't stop the turn" must not outlive the turn it described —
  // and `retry` is a new turn, so it clears it for the same reason `send` does.
  cancelError.value = ''
  const res = await deliver(content)
  settleDelivery(i, content, res)
}

// ---- Voice: speak replies (TTS) + dictate (STT) — carried over from #78 -------
const SpeechRec = typeof window !== 'undefined' ? (window.SpeechRecognition || window.webkitSpeechRecognition) : null
const canRecord = typeof navigator !== 'undefined' && !!navigator.mediaDevices?.getUserMedia && typeof MediaRecorder !== 'undefined'
// #2212: the mic used to prefer the browser Web Speech API whenever the object
// merely existed — a Google-hosted service that reports nothing at all in
// Chromium (measured) and ends at the first pause everywhere. Recording + our
// own /stt wins when the platform can transcribe; see `resolveMicMode`.
const micMode = computed(() => resolveMicMode({
  speechApi: !!SpeechRec,
  canRecord,
  serverStt: !!props.agent.stt_available,
}))
// No mic button at all when neither path can work, so the control is never a
// dead affordance on an instance with no voice provider.
const sttSupported = computed(() => micMode.value !== null)
const ttsEnabled = computed(() => !!props.agent.voice_available)
// The one place any voice failure becomes words. Every path below sets it
// instead of returning silently, which is what made this read as "just dies".
const voiceError = ref('')

// #2157: the speaker choice sticks per client+agent instead of resetting to off
// on every page load. Narration was already hard to find — an agent that had
// just told the client this surface was "text-only" was the only hint it existed
// — and re-muting it every reload taught clients it had not really worked.
const voiceModeKey = computed(() => `trinity.portal.voiceMode.${props.agent?.name || ''}`)
function loadVoiceMode() {
  try { return localStorage.getItem(voiceModeKey.value) === '1' } catch { return false }
}
const voiceMode = ref(loadVoiceMode())
watch(voiceMode, (on) => {
  try { localStorage.setItem(voiceModeKey.value, on ? '1' : '0') } catch { /* private mode: session-only */ }
})
// Switching agents adopts that agent's own remembered choice.
watch(() => props.agent?.name, () => {
  // ent#534: a call belongs to the agent it was started with — carrying an
  // open microphone across a switch would send the next utterance to someone
  // the user never chose to talk to.
  if (voiceCallActive.value) void voice.stop()
  stopSpeaking(); voiceError.value = ''; voiceMode.value = loadVoiceMode()
})
const speaking = ref(false)
const listening = ref(false)
const transcribing = ref(false)
const micTitle = computed(() => (
  transcribing.value ? 'Transcribing…'
    : listening.value ? 'Listening… click to stop'
      : 'Speak your message'
))
const audioEl = ref(null)
let recog = null, mediaRec = null, mediaStream = null, recChunks = [], lastAudioUrl = null
let speechWatchdog = null

function revokeAudio() { if (lastAudioUrl) { URL.revokeObjectURL(lastAudioUrl); lastAudioUrl = null } }
// Synthesis is a real 1-3 s round trip, and `speak` COMMITS playback after the
// await regardless of what happened during it. Without a generation token an
// explicit stop (the speaker toggled off, an agent switch, a voice call
// starting — ent#534) would release everything and the audio would play
// anyway. The check lives here, where the commit happens.
let narrationToken = 0
async function speak(text) {
  if (!text) return
  const token = ++narrationToken
  speaking.value = true
  try {
    const url = await store.synthesizeTts(props.agent.name, text)
    // Abandoned mid-synthesis (barge-in, Stop, agent switch, unmount). Discard
    // the audio rather than playing it into a conversation that moved on; the
    // URL is ours and nothing else holds it, so revoke it here.
    if (token !== narrationToken) { if (url) URL.revokeObjectURL(url); return }
    // The store answers `null` for every failure shape, so this is the only
    // place narration can report that it did not happen (#2212).
    if (!url) { speaking.value = false; voiceError.value = TTS_FAILED_MESSAGE; return }
    revokeAudio(); lastAudioUrl = url
    if (audioEl.value) { audioEl.value.src = url; await audioEl.value.play() } else speaking.value = false
  } catch { if (token === narrationToken) { speaking.value = false; voiceError.value = TTS_FAILED_MESSAGE } }
}
// Bumping the token is what makes an in-flight synthesis abandon itself; the
// pause alone cannot reach audio that has not been assigned yet.
function stopSpeaking() { narrationToken++; if (audioEl.value) audioEl.value.pause(); speaking.value = false }
// The narration element finished (or failed) — the speaker is free again.
function onNarrationDone() { speaking.value = false }
// Dictated text lands at the end of whatever is already typed — one place, so
// the two mic paths cannot drift on how a transcript is applied.
function appendTranscript(text) {
  const t = (text || '').trim()
  if (!t) return
  input.value = input.value ? `${input.value} ${t}` : t
  resetTypeahead(); autoGrowAfterUpdate()
}
function toggleMic() {
  if (!sttSupported.value || transcribing.value) return
  voiceError.value = ''
  micMode.value === 'speech' ? toggleSpeech() : toggleRecord()
}
function clearSpeechWatchdog() { if (speechWatchdog) { clearTimeout(speechWatchdog); speechWatchdog = null } }
function toggleSpeech() {
  if (listening.value) { try { recog?.stop() } catch { /* noop */ } return }
  recog = new SpeechRec()
  recog.lang = 'en-US'
  // `continuous` defaults to false — recognition ends at the first pause, which
  // on its own reads as "the mic died" mid-sentence (#2212).
  recog.continuous = true
  recog.interimResults = false
  recog.maxAlternatives = 1
  // Local to THIS attempt: a stale flag from the previous one would decide the
  // next attempt's verdict. `settle` runs once, whichever handler gets there
  // first — engines disagree on whether `error` is followed by `end`.
  let gotText = false, errorCode = null, timedOut = false, settled = false
  const settle = () => {
    if (settled) return
    settled = true
    clearSpeechWatchdog()
    listening.value = false
    const message = speechAttemptOutcome({ gotText, errorCode, timedOut })
    if (message) voiceError.value = message
  }
  recog.onstart = () => { clearSpeechWatchdog() }
  recog.onresult = (e) => {
    // With `continuous` the event carries only the results from `resultIndex`
    // on, and interim ones are filtered out by `isFinal`.
    let text = ''
    for (let i = e.resultIndex ?? 0; i < (e.results?.length || 0); i++) {
      const r = e.results[i]
      if (r?.isFinal) text += r[0]?.transcript || ''
    }
    if (!text.trim()) return
    gotText = true
    appendTranscript(text)
  }
  // The error CODE is the signal this component used to throw away; it is the
  // whole difference between "blocked permission" and "service unreachable".
  recog.onerror = (e) => { errorCode = e?.error || ''; settle() }
  recog.onend = () => settle()
  listening.value = true
  // Measured in Chromium: `start()` resolves and the engine then emits NO event
  // of any kind, so without this the button stays lit forever with no recourse.
  speechWatchdog = setTimeout(() => {
    speechWatchdog = null
    if (settled) return
    timedOut = true
    settle()
    try { recog?.abort() } catch { /* noop */ }
  }, SPEECH_START_TIMEOUT_MS)
  try { recog.start() } catch (e) {
    settled = true
    clearSpeechWatchdog()
    listening.value = false
    // `start()` throws InvalidStateError only when a session is already live.
    voiceError.value = e?.name === 'InvalidStateError'
      ? 'Dictation is already running — stop it and try again.'
      : speechErrorMessage('')
  }
}
async function toggleRecord() {
  if (listening.value) { try { mediaRec?.stop() } catch { /* noop */ } return }
  try { mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true }) }
  catch (e) { voiceError.value = recorderErrorMessage(e); return }
  recChunks = []
  try { mediaRec = new MediaRecorder(mediaStream) }
  catch (e) { stopStream(); voiceError.value = recorderErrorMessage(e); return }
  mediaRec.ondataavailable = (e) => { if (e.data && e.data.size) recChunks.push(e.data) }
  mediaRec.onerror = (e) => {
    listening.value = false; stopStream()
    voiceError.value = recorderErrorMessage(e?.error || e)
  }
  mediaRec.onstop = async () => {
    stopStream(); listening.value = false
    // Firefox leaves `mediaRec.mimeType` empty and puts the real type on the
    // chunks; mislabelling Ogg as WebM is a silent upload bug (#2212).
    const type = resolveRecordingMimeType(mediaRec?.mimeType, recChunks)
    const blob = new Blob(recChunks, { type }); recChunks = []
    if (blob.size < MIN_RECORDING_BYTES) { voiceError.value = RECORDING_TOO_SHORT_MESSAGE; return }
    transcribing.value = true
    try {
      const t = await store.transcribeStt(props.agent.name, blob)
      if (t) appendTranscript(t)
      else voiceError.value = TRANSCRIPT_EMPTY_MESSAGE
    } catch (e) {
      // /stt answers with a user-facing `detail`; it used to be swallowed.
      voiceError.value = transcriptionErrorMessage(e)
    } finally { transcribing.value = false }
  }
  listening.value = true
  try { mediaRec.start(200) } catch (e) { listening.value = false; stopStream(); voiceError.value = recorderErrorMessage(e) }
}
function stopStream() { try { mediaStream?.getTracks().forEach((t) => t.stop()) } catch { /* noop */ } mediaStream = null }
function cleanupVoice() {
  // ent#534: an unmount mid-call ends it on the server (the bridge keeps the
  // transcript); nothing here waits for it.
  if (voice.isActive.value) void voice.stop()
  clearSpeechWatchdog()
  try { recog?.stop() } catch { /* noop */ }
  try { if (mediaRec && mediaRec.state !== 'inactive') mediaRec.stop() } catch { /* noop */ }
  stopStream(); stopSpeaking(); revokeAudio()
}

// ---- ent#534: the voice call — the orb takes the conversation ----------------
// Modal, the way ChatGPT's voice mode is: you are either in the chat or in the
// call. The call is the platform's real-time voice session (the shared orb
// `chat/VoiceOverlay.vue` + `useVoiceSession`, reused not forked — and since
// #2559 this is its ONLY consumer; Agent Detail offers a door here, not an orb
// of its own), bound to THIS thread: its context is this chat's recent turns and
// its transcript is written back here, turn by turn, as one collapsed
// "Voice call · N min" block. While it is on, the header controls, the tabs and
// the composer are inert; the shell swaps the rail for the agent's canvas. End
// (button, orb, Escape) returns to the chat exactly where it was.
//
// The ent#440 hands-free STT→typed-turn→TTS loop that used to live here is
// retired by the same ruling (one voice entry point). Hold-to-dictate (#2212)
// and spoken replies (#2157) stay: they are input/output aids, not a mode.
// ---- The model choice (trinity-enterprise#403) ---------------------------------
//
// A short curated dropdown for PLATFORM users. The rules are in
// `portalModelChoice.js`; this is the dispatcher over them plus the two wires
// they cannot own: the server preference record, and the send path.
//
// The choice is the user's SERVER record (`workspace_model`), not browser
// storage — per (user, agent) by construction, since the server keys the row by
// user. Known and accepted: the record arrives asynchronously, so the select can
// read "Agent's default" for one frame before adopting the stored value. It
// causes no layout jank (unlike a column width) and NO turn can run on the wrong
// model, because nothing is sent until Send.
const prefs = useUserPreferencesStore()
const modelPrefRecord = computed(() => prefs.records[PREF_KEYS.workspaceModel]?.value || {})
const modelOptions = computed(() => store.modelOptions || [])
// ent#361: the same rule `send()` applies — while the draft @mentions another
// agent it is bound for a ROOM, whose composer has no model control.
const draftIsRoomBound = computed(() => {
  if (!store.multiAgentChatAvailable || !props.agent?.name) return false
  const text = input.value.trim()
  if (!text) return false
  return mentionedAgents(text, props.roster, { exclude: [props.agent.name] }).length > 0
})
const modelControl = computed(() => modelControlState({
  isPlatform: store.isPlatformSession,
  modelDefault: props.agent?.model_default || null,
  options: modelOptions.value,
  roomBound: draftIsRoomBound.value,
}))
// `serverGeneration` bumps on load and on a 409 adoption, so the select follows
// the record the server actually holds rather than a value this tab guessed.
// Arrow properties, not `get() {}` / `set() {}` shorthand: `portalUndefinedCalls.spec.js`
// scans this file for `name(` and would read the shorthand method names as
// calls to undefined functions.
const selectedModel = computed({
  get: () => {
    void prefs.serverGeneration
    return storedFor(modelPrefRecord.value, props.agent?.name, modelOptions.value)
  },
  set: (value) => setModelChoice(value),
})
const modelDefaultText = computed(() => defaultOptionText(props.agent?.model_default))

function setModelChoice(value) {
  if (!props.agent?.name) return
  prefs.save(
    PREF_KEYS.workspaceModel,
    withChoice(modelPrefRecord.value, props.agent.name, value),
    { origin: 'gesture' },
  )
}

// The self-heal. A model the agent could not complete on is cleared back to
// inherit, so the server's "switched back to the agent's default" sentence is
// TRUE on the next turn instead of looping the person into the same failure on
// every retry and every reload.
function clearModelChoiceOnFailure(outcome) {
  if (shouldClearChoice(outcome)) setModelChoice(INHERIT_VALUE)
}

// Read the record once the agent is known. `load()` is idempotent per identity
// and shared with the Dashboard Grid, so this is free when it has already run.
onMounted(() => { void prefs.load() })

const voice = useVoiceSession(props.agent.name)
const voiceStarting = ref(false)
const voiceEndNotice = ref('')
const voiceEntry = computed(() => voiceEntryState({
  isPlatform: store.isPlatformSession,
  realtimeVoice: store.realtimeVoice,
}))
const voiceCallActive = computed(() => voice.isActive.value)
const voiceHeaderText = computed(() => voiceHeaderLine({
  status: voice.status.value,
  toolName: voice.toolName.value,
  muted: voice.muted.value,
  error: voice.error.value,
  backgroundTasks: voice.backgroundTasks.value,
}))
// The thread, with each voice call's rows folded into one block.
const threadItems = computed(() => groupVoiceBlocks(messages.value))

// The shell reads these to swap the rail for the canvas column and to refuse
// chat navigation while the call is on.
// Found live: `active` rises BEFORE the start request answers, so an emit on
// `active` alone carried no session id and the canvas column fetched
// `/voice//panel`. Both facts are watched; the shell mounts the column only
// once the id is known.
watch([voiceCallActive, () => voice.voiceSessionId.value], ([on, sid]) => {
  emit('voice-call', { active: on, agentName: props.agent?.name, voiceSessionId: on ? sid : null })
})
watch(() => voice.panelVersion.value, (v) => emit('voice-panel', v))
// ent#551: the thread is on screen for the whole call, so nothing that lands in
// it during the call is unread — a spoken turn, or a background task's reply.
// Without this the sidebar badge counted up while the person was talking to the
// agent. The read cursor is advanced directly (no list refresh, no title-settle
// cycle); the next chat-state fetch then reads zero. Debounced: turns land in
// bursts.
let voiceReadTimer = null
watch([() => voice.transcriptEntries.value.length, () => voice.panelVersion.value], () => {
  if (!voiceCallActive.value || !currentSessionId.value) return
  clearTimeout(voiceReadTimer)
  voiceReadTimer = setTimeout(() => {
    if (voiceCallActive.value && currentSessionId.value) void store.markChatRead('thread', currentSessionId.value)
  }, 800)
})
// The call ended — by End, by the cap, by the provider — and the bridge has
// confirmed (or given up on) the write: reload the thread so the persisted
// block replaces nothing local, and say why when it did not end by choice.
watch(voiceCallActive, async (on, was) => {
  if (!was || on) return
  voiceEndNotice.value = endedNotice({ reason: voice.endReason.value, message: voice.endMessage.value })
  if (voice.error.value && !voiceEndNotice.value) voiceError.value = voice.error.value
  if (currentSessionId.value) await loadThread(currentSessionId.value)
  emit('sessions-changed', currentSessionId.value)
})

async function startVoiceCall() {
  if (voiceCallActive.value || voiceStarting.value) return
  voiceError.value = ''
  voiceEndNotice.value = ''
  // Re-checked at click even though the control is disabled when unavailable:
  // the roster refreshes in the background, so the answer has to be words.
  const entry = voiceEntry.value
  if (!entry.render || !entry.enabled) { voiceError.value = entry.reason || VOICE_UNAVAILABLE_FALLBACK; return }
  const pre = voicePreflight({
    canCapture: canRecord,
    secureContext: typeof window === 'undefined' ? true : window.isSecureContext !== false,
  })
  if (pre) { voiceError.value = pre; return }
  if (sending.value) { voiceError.value = 'Wait for the current reply, then start the call.'; return }
  voiceStarting.value = true
  try {
    // Dictation and narration must not overlap the call's own mic and speaker.
    try { recog?.stop() } catch { /* noop */ }
    try { if (mediaRec && mediaRec.state !== 'inactive') mediaRec.stop() } catch { /* noop */ }
    stopStream(); stopSpeaking()
    // A brand-new chat has no thread yet; the transcript needs a home BEFORE
    // the first word is spoken, so the thread is created and adopted first.
    let sid = currentSessionId.value
    if (!sid) {
      const created = await store.createSession(props.agent.name)
      sid = created?.id || created?.session_id || null
      if (!sid) { voiceError.value = 'Could not open a chat for the call.'; return }
      adoptSession(sid)
      emit('sessions-changed', sid)
    }
    const ok = await voice.startWith(
      () => store.startWorkspaceVoice(props.agent.name, sid),
      { restStop: false },
    )
    if (!ok) voiceError.value = voice.error.value || VOICE_UNAVAILABLE_FALLBACK
  } catch (e) {
    voiceError.value = startFailureReason({ status: e?.response?.status, detail: e?.response?.data?.detail })
  } finally {
    voiceStarting.value = false
  }
}

async function endVoiceCall() {
  if (!voiceCallActive.value) return
  await voice.stop()
}


// `startVoiceCall` is exposed for the Talk door (#2559): `Portal.vue` holds a
// `ref="conversationRef"` and calls it once, after `bootstrap()` consumed an
// armed `?voice=1`. Both tokens are live — `focusComposer` gained its own
// consumer in #2579 (`nextTick(focusComposer)` on a new chat) — so dropping
// either is a break, not dead-code cleanup.
defineExpose({ focusComposer, startVoiceCall })

// ent#474 — the rail's Work signal for a 1:1, DERIVED from the in-flight flag
// on every change and never latched: it clears in the same `finally` that ends
// a turn, failed or not. The unmount emit is the belt for a chat switch
// mid-turn (this component is keyed by the shell and remounts on a switch);
// the shell resets on every switch as well, so neither side can leave the
// rail reading "running" for a conversation that is no longer on screen.
watch(
  [sending, activeExecutionId],
  ([on, id]) => emit('work-state', workSignalFrom({ sending: on, agent: props.agent?.name, executionId: id })),
  { immediate: true }
)
onBeforeUnmount(() => emit('work-state', workSignalFrom({ sending: false })))
</script>
