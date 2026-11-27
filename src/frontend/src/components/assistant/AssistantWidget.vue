<template>
  <!-- A first visit: the assistant says hello beside its button, so nobody has to guess what the circle is -->
  <div
    v-if="store.peek && !isOpen && !firstRun.overlayOpen"
    class="assistant-peek fixed z-50 bottom-24 left-4 md:bottom-auto md:top-[4.25rem] md:left-2 max-w-[250px] flex items-start gap-1 rounded-2xl rounded-bl-sm md:rounded-bl-2xl md:rounded-tl-sm bg-action-primary-600 text-white shadow-xl shadow-action-primary-600/30"
    data-testid="assistant-peek"
  >
    <button type="button" class="flex-1 text-left text-[13.5px] leading-snug pl-3.5 py-2.5 focus:outline-none focus-visible:underline" data-testid="assistant-peek-open" @click="open()">
      Hi, I'm your Trinity assistant. Want a hand getting an agent working for you?
    </button>
    <button
      type="button"
      class="w-7 h-7 mt-1 mr-1 flex-none rounded-full text-white/80 hover:text-white hover:bg-white/15 flex items-center justify-center"
      aria-label="Not now"
      data-testid="assistant-peek-close"
      @click="store.dismissPeek()"
    >
      <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 18L18 6M6 6l12 12" /></svg>
    </button>
  </div>

  <!--
    Collapsed: a slim rail down the left edge of every page. The page is laid
    out beside it (App.vue pads for it), so it never covers anything. Its top
    corner belongs to the top bar: the assistant's button is the logo there,
    with "Trinity" beside it, open or collapsed. On a phone there is no room
    for a rail and the button floats in the bottom-left corner instead.
  -->
  <div
    v-if="!isOpen"
    class="assistant-corner hidden md:block fixed top-0 left-0 z-40 w-12 h-16 bg-white dark:bg-gray-800 shadow dark:shadow-gray-900"
    aria-hidden="true"
  ></div>
  <!-- One "Trinity" for both states: on opening it glides up and turns white
       into the header's title line instead of being swapped for another. -->
  <router-link
    to="/"
    class="hidden md:flex fixed top-0 left-[3.25rem] z-50 h-16 items-center text-xl font-bold leading-tight hover:opacity-80 transition-[transform,color,opacity] duration-300 ease-out motion-reduce:transition-none"
    :class="isOpen ? 'text-white -translate-y-[9px]' : 'text-gray-900 dark:text-white'"
    data-testid="assistant-wordmark"
  >Trinity</router-link>
  <div
    v-if="!isOpen"
    class="hidden md:flex fixed top-16 bottom-0 left-0 z-40 w-12 flex-col items-center pt-3 bg-white dark:bg-gray-800 border-r border-gray-200 dark:border-gray-750"
    data-testid="assistant-rail"
  >
    <button
      type="button"
      class="w-8 h-8 rounded-md flex items-center justify-center text-gray-500 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-100 hover:bg-gray-100 dark:hover:bg-gray-750 focus:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500"
      aria-label="Expand the Trinity assistant"
      :title="`Open the assistant (${toggleKeys})`"
      data-testid="assistant-expand"
      @click="open()"
    >
      <svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24" aria-hidden="true"><path d="M13 5l7 7-7 7M5 5l7 7-7 7" /></svg>
    </button>
    <!-- Written down the rail, so the strip says what it is without being opened. -->
    <button
      type="button"
      class="assistant-rail-label mt-3 flex-1 min-h-0 w-full text-[11px] font-semibold uppercase tracking-wider text-gray-500 dark:text-gray-400 hover:text-action-primary-700 dark:hover:text-action-primary-400 focus:outline-none"
      tabindex="-1"
      aria-hidden="true"
      @click="open()"
    >Assistant</button>
  </div>

  <button
    type="button"
    data-testid="assistant-launcher"
    :aria-expanded="isOpen ? 'true' : 'false'"
    class="assistant-orb fixed z-50 bottom-5 left-4 w-14 h-14 md:bottom-auto md:top-3.5 md:left-1.5 md:w-9 md:h-9 bg-gradient-to-b from-action-primary-500 to-action-primary-700 hover:from-action-primary-500 hover:to-action-primary-600 text-white rounded-full shadow-xl md:shadow-md shadow-action-primary-600/40 ring-1 ring-inset flex items-center justify-center transition-transform hover:scale-105 active:scale-100 motion-reduce:transition-none motion-reduce:hover:scale-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500 focus-visible:ring-offset-2 dark:focus-visible:ring-offset-gray-900"
    :class="[isOpen ? 'ring-white/50' : 'ring-white/25', { 'max-md:hidden': hideLauncher || isOpen }]"
    :aria-label="isOpen ? 'Collapse the Trinity assistant' : store.unread ? 'Open the Trinity assistant: it has an answer for you' : busy ? 'Open the Trinity assistant: it is working' : 'Open the Trinity assistant'"
    @click="isOpen ? close() : open()"
  >
    <!-- Depth: light from above, and a soft shadow on the page below it. -->
    <span class="absolute inset-x-2 top-1 h-1/2 rounded-full bg-gradient-to-b from-white/30 to-transparent pointer-events-none" aria-hidden="true"></span>
    <span class="assistant-orb-ground md:hidden" aria-hidden="true"></span>
    <!-- Now and then a ripple leaves it; sooner when there is something to read. -->
    <span
      v-if="!busy && !isOpen"
      class="assistant-halo"
      :class="{ eager: store.unread > 0 }"
      data-testid="assistant-launcher-halo"
      aria-hidden="true"
    ></span>
    <!-- Working while closed: a slow ring. An answer waiting: a count. -->
    <span
      v-if="busy && !store.unread && !isOpen"
      class="absolute inset-0 rounded-full ring-2 ring-action-primary-400 animate-ping motion-reduce:animate-none"
      data-testid="assistant-launcher-busy"
      aria-hidden="true"
    ></span>
    <img src="../../assets/trinity-logo-white.svg" alt="" class="relative w-8 h-8 md:w-6 md:h-6 drop-shadow" />
    <span
      v-if="store.unread"
      class="absolute -top-1 -right-1 min-w-5 h-5 px-1 rounded-full bg-status-danger-600 text-white text-[11px] font-bold tabular-nums flex items-center justify-center ring-2 ring-white dark:ring-gray-900"
      data-testid="assistant-launcher-unread"
      aria-hidden="true"
    >{{ store.unread }}</span>
  </button>

  <Transition name="assistant-dock">
  <section
    v-if="isOpen"
    ref="panelEl"
    data-testid="assistant-panel"
    class="assistant-dock fixed inset-y-0 left-0 z-40 w-full md:w-[var(--assistant-w,24rem)] max-w-full bg-white dark:bg-gray-800 flex flex-col rounded-r-2xl border-r-[3px] border-gray-300 dark:border-gray-600 shadow-2xl overflow-hidden"
    role="complementary"
    aria-label="Trinity assistant"
    @keydown.escape="wide || close()"
  >
    <!-- Its right edge can be dragged: the panel is as wide as the person wants it. -->
    <div
      class="hidden md:block absolute top-0 right-0 z-10 h-full w-1.5 cursor-col-resize hover:bg-action-primary-500/40 focus:outline-none focus-visible:bg-action-primary-500/60 transition-colors"
      :class="{ 'bg-action-primary-500/60': resizing }"
      role="separator"
      aria-orientation="vertical"
      aria-label="Resize the assistant"
      :aria-valuenow="width"
      :aria-valuemin="MIN_W"
      :aria-valuemax="maxWidth()"
      tabindex="0"
      title="Drag to resize. Double-click for the usual width."
      data-testid="assistant-resize"
      @pointerdown="startResize"
      @dblclick="setWidth(DEFAULT_W, true)"
      @keydown.left.prevent="setWidth(width - 24, true)"
      @keydown.right.prevent="setWidth(width + 24, true)"
    ></div>

    <!-- Header. The button at its left is the one the collapsed rail shows, in the same place. -->
    <header class="flex items-center gap-2.5 pl-4 md:pl-[3.25rem] pr-2 h-16 bg-action-primary-600 text-white shrink-0">
      <div class="flex-1 min-w-0">
        <p class="flex items-center gap-1.5 text-xl font-bold leading-tight">
          <!-- Held open for the gliding wordmark above on md+; shown as is on a phone. -->
          <span class="md:invisible" data-testid="assistant-title">Trinity</span>
          <span v-if="store.subscribed" class="px-1.5 py-0.5 text-[10px] font-bold leading-none rounded bg-white/20">PRO</span>
        </p>
        <p class="text-[12.5px] text-white/90 truncate" data-testid="assistant-plan">{{ store.planLine }}</p>
      </div>
      <button
        type="button"
        class="w-9 h-9 rounded-lg text-white/90 hover:text-white hover:bg-white/10 flex items-center justify-center disabled:opacity-45"
        aria-label="New conversation"
        title="New conversation"
        :disabled="store.sending || store.messages.length === 0"
        @click="store.newConversation()"
      >
        <svg class="w-[18px] h-[18px]" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14" /></svg>
      </button>
      <button
        type="button"
        class="w-9 h-9 rounded-lg text-white/90 hover:text-white hover:bg-white/10 flex items-center justify-center"
        aria-label="Close the assistant"
        title="Collapse to the side"
        @click="close"
      >
        <svg class="w-[18px] h-[18px]" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24" aria-hidden="true"><path d="M11 19l-7-7 7-7M19 19l-7-7 7-7" /></svg>
      </button>
    </header>

    <!--
      Getting started: the same server-derived steps the systems rail shows where
      there is no assistant (stores/onboarding.js). One quiet line that says what
      is next; it opens into the list. A step added to the server's catalogue
      shows here with nothing changed on this side. Gone when all are done or
      the person has said not to show it.
    -->
    <section
      v-if="setup.visible && !firstSessionOn"
      class="shrink-0 border-b border-action-primary-200 dark:border-action-primary-500/40 bg-action-primary-50 dark:bg-action-primary-500/16 text-action-primary-700 dark:text-action-primary-200"
      data-testid="assistant-setup"
    >
      <button
        type="button"
        class="w-full flex items-center gap-2 px-4 py-2 text-left text-[12.5px] focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-action-primary-500"
        :aria-expanded="setupOpen ? 'true' : 'false'"
        :aria-label="`Getting started: ${setup.completedCount} of ${setup.totalCount} done.${nextStep ? ' Next: ' + nextStep.title : ''}`"
        data-testid="assistant-setup-toggle"
        @click="toggleSetup"
      >
        <span class="font-semibold flex-none">Getting started</span>
        <!-- One mark per step, so the length of the road is seen at a glance. -->
        <span class="flex items-center gap-1 flex-none" aria-hidden="true">
          <span
            v-for="i in setup.items"
            :key="i.key"
            class="w-1.5 h-1.5 rounded-full"
            :class="i.done ? 'bg-status-success-500' : 'bg-current opacity-30'"
          ></span>
        </span>
        <span class="flex-none tabular-nums opacity-80">{{ setup.completedCount }} of {{ setup.totalCount }}</span>
        <span v-if="!setupOpen && nextStep" class="min-w-0 truncate opacity-80">· Next: {{ nextStep.title }}</span>
        <svg
          class="ml-auto w-3.5 h-3.5 flex-none transition-transform motion-reduce:transition-none"
          :class="setupOpen ? 'rotate-180' : ''"
          fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24" aria-hidden="true"
        ><path d="M19 9l-7 7-7-7" /></svg>
      </button>

      <div v-if="setupOpen" class="px-4 pb-3 space-y-2.5">
        <ul class="space-y-2">
          <li
            v-for="i in setup.items"
            :key="i.key"
            class="flex items-start gap-2 text-[13px]"
            :data-testid="`assistant-setup-item-${i.key}`"
            :data-done="i.done ? 'true' : 'false'"
          >
            <span
              class="flex-none mt-0.5 w-4 h-4 rounded-full flex items-center justify-center"
              :class="i.done ? 'bg-status-success-500 text-white' : 'border border-current opacity-50'"
              aria-hidden="true"
            >
              <svg v-if="i.done" class="w-3 h-3" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24"><path d="M5 13l4 4L19 7" /></svg>
            </span>
            <div class="min-w-0 flex-1">
              <p :class="i.done ? 'line-through opacity-60' : 'font-medium'">
                {{ i.title }}<span v-if="i.done" class="sr-only"> (done)</span>
              </p>
              <!-- Only the next step explains itself and offers a hand: one thing to do, not a wall of buttons. -->
              <template v-if="nextStep && i.key === nextStep.key">
                <p v-if="i.description" class="text-[12.5px] opacity-80 mt-0.5">{{ i.description }}</p>
                <BaseButton size="sm" class="mt-1.5" :disabled="store.sending" data-testid="assistant-setup-help" @click="helpWith(i)">Do this with me</BaseButton>
              </template>
            </div>
          </li>
        </ul>
        <button
          type="button"
          class="text-[12px] underline opacity-70 hover:opacity-100 rounded focus:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500"
          title="Hides these steps for good: they do not come back"
          data-testid="assistant-setup-dismiss"
          @click="setup.dismiss()"
        >Don't show this again</button>
      </div>
    </section>

    <!-- Two tabs: the assistant, and the documentation's Ask -->
    <div class="px-4 py-2 border-b border-gray-200 dark:border-gray-700 shrink-0">
      <BaseSegmented v-model="tab" :options="TABS" aria-label="What to talk to" />
    </div>

    <!-- Ask: free, always there, needs nothing -->
    <div v-show="tab === 'ask'" class="flex flex-col flex-1 min-h-0" data-testid="assistant-ask">
      <HelpChatWidget embedded ask-only />
    </div>

    <template v-if="tab === 'trinity'">
      <!-- Not loaded yet, or the load failed -->
      <div v-if="!store.status" class="flex-1 min-h-0 p-4 space-y-3">
        <InlineError v-if="store.statusError" :message="store.statusError" retryable @retry="store.loadStatus()" />
        <div v-else aria-busy="true" class="space-y-3">
          <span class="sr-only">Loading</span>
          <div class="h-4 w-2/3 rounded bg-gray-100 dark:bg-gray-750 animate-pulse motion-reduce:animate-none"></div>
          <div class="h-10 rounded-md bg-gray-100 dark:bg-gray-750 animate-pulse motion-reduce:animate-none"></div>
        </div>
      </div>

      <template v-else>
        <!-- Conversation -->
        <!-- On the ground colour, so each reply (a surface) reads as its own box. -->
        <div ref="messagesEl" class="flex-1 min-h-0 overflow-y-auto p-4 space-y-3 text-sm bg-gray-50 dark:bg-gray-900" aria-live="polite">
          <!-- The opening is not decided yet (trinity-enterprise#1356): nothing is shown that could be replaced a moment later -->
          <!-- No statusError guard here: the retry error shows only while there is no status (the branch above), so this
               placeholder can never pulse under it; a failed re-read with the status on screen keeps it, never a blank panel -->
          <div
            v-if="store.messages.length === 0 && !openingDecided"
            class="space-y-2 animate-pulse motion-reduce:animate-none"
            aria-busy="true"
            data-testid="assistant-opening-pending"
          >
            <span class="sr-only">Loading the assistant</span>
            <div class="h-3 w-3/4 rounded bg-gray-100 dark:bg-gray-750" />
            <div class="h-3 w-1/2 rounded bg-gray-100 dark:bg-gray-750" />
          </div>
          <div v-else-if="store.messages.length === 0 && openingDecided" class="space-y-3" data-testid="assistant-welcome">
            <p class="text-gray-900 dark:text-gray-100">
              I help you set Trinity up and get agents working for you: I build them with you, check on them and fix what I can.
            </p>
            <p class="text-[12.5px] text-gray-500 dark:text-gray-400">
              Found a problem or have an idea? Tell me and I'll pass it to the Trinity team. Anything that switches something on or gives access, I ask you to confirm first.
            </p>
          </div>

          <template v-for="m in store.messages" :key="m.id">
            <p
              v-if="m.role === 'note'"
              class="text-[12.5px] text-gray-500 dark:text-gray-400 text-center"
              data-testid="assistant-note"
            >{{ noteWords(m.content) }}</p>
            <!-- Its replies keep a little space on the right, as the person's do on the left. -->
            <div v-else-if="m.content" :class="m.role === 'user' ? '' : 'pr-6'">
              <ChatBubble :role="m.role" :content="m.content" boxed />
            </div>

            <template v-for="c in m.cards || []" :key="c.id">
            <!-- A question answered with a click (or by writing instead) -->
            <div v-if="c.card.action === 'choice'" class="space-y-1.5" data-testid="assistant-choice">
              <p class="font-medium text-gray-900 dark:text-gray-100">{{ c.card.question }}</p>
              <div class="flex flex-wrap gap-2">
                <BaseButton
                  v-for="o in c.card.options"
                  :key="o"
                  size="sm"
                  :variant="c.picked === o ? 'primary' : 'secondary'"
                  :aria-pressed="c.picked === o"
                  :disabled="store.sending || !['open', 'picked'].includes(c.state)"
                  @click="store.answerChoice(m.id, c.id, o, route)"
                >{{ o }}</BaseButton>
              </div>
            </div>
            <div
              v-else
              data-testid="assistant-card"
              class="rounded-lg border p-3 space-y-2 border-action-primary-200 dark:border-action-primary-500/40 bg-action-primary-50 dark:bg-action-primary-500/16"
            >
              <p class="font-mono text-[11px] font-semibold uppercase tracking-wider text-action-primary-700 dark:text-action-primary-300">
                {{ heading(c) }}
              </p>
              <p class="font-medium text-gray-900 dark:text-gray-100">{{ c.label }}</p>

              <!-- A design: what the person is about to get -->
              <template v-if="c.card.action === 'build'">
                <p v-if="c.card.design.summary" class="text-gray-900 dark:text-gray-100">{{ c.card.design.summary }}</p>
                <ul class="space-y-2" data-testid="assistant-design">
                  <li v-for="a in c.card.design.agents" :key="a.name" class="rounded-md bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 px-3 py-2">
                    <p class="font-medium text-gray-900 dark:text-gray-100">{{ a.name }}</p>
                    <p class="text-[12.5px] text-gray-600 dark:text-gray-300">{{ a.job }}</p>
                    <p v-if="a.schedule" class="text-[12.5px] text-gray-600 dark:text-gray-300">Schedule: {{ a.schedule.name }} (added switched off)</p>
                    <p v-if="a.calls.length" class="text-[12.5px] text-gray-600 dark:text-gray-300">Hands work to: {{ a.calls.join(', ') }}</p>
                    <p v-if="a.credentials.length" class="text-[12.5px] text-gray-600 dark:text-gray-300">You'll add: {{ a.credentials.join(', ') }}</p>
                  </li>
                </ul>
              </template>
              <!-- A note to the Trinity team: all of what will be sent -->
              <p
                v-if="c.card.action === 'feedback' && c.card.details"
                class="text-[12.5px] text-gray-900 dark:text-gray-100 whitespace-pre-wrap break-words max-h-40 overflow-y-auto"
                data-testid="assistant-feedback"
              >{{ c.card.details }}</p>
              <p v-if="!(FOLLOWED.includes(c.card.action) && c.state !== 'making')" class="text-[12.5px] text-gray-600 dark:text-gray-300">{{ c.note }}</p>
              <!-- A tool's parameters: exactly what will run, behind Details, never the body (ent#1390) -->
              <div v-if="c.card.action === 'tool'" class="space-y-1">
                <button
                  type="button"
                  class="text-[12.5px] font-medium underline text-action-primary-600 dark:text-action-primary-400"
                  :aria-expanded="detailsOpen[c.id] ? 'true' : 'false'"
                  data-testid="assistant-tool-details-toggle"
                  @click="detailsOpen[c.id] = !detailsOpen[c.id]"
                >Details</button>
                <dl v-if="detailsOpen[c.id]" class="text-[12.5px] text-gray-900 dark:text-gray-100 space-y-0.5" data-testid="assistant-tool">
                  <div v-for="d in toolWords(c.card).details" :key="d.key" class="flex gap-2">
                    <dt class="text-gray-500 dark:text-gray-400 flex-none">{{ d.key }}</dt>
                    <dd class="min-w-0 break-words">{{ d.value }}</dd>
                  </div>
                </dl>
              </div>

              <!-- A run that ended well: its result, one click away (ent#1355) -->
              <a
                v-if="c.result"
                :href="c.result"
                class="inline-block text-[12.5px] font-medium underline text-action-primary-600 dark:text-action-primary-400"
                data-testid="assistant-result"
                @click.prevent="openResult(c.result)"
              >Open the result</a>
              <div v-if="c.state === 'making'" class="space-y-1.5" data-testid="assistant-job">
                <!-- Steps known (a build): how far. Not known (a run): that it is alive. -->
                <div
                  class="h-1.5 rounded-full overflow-hidden bg-action-primary-100 dark:bg-action-primary-500/20"
                  role="progressbar"
                  :aria-label="c.label"
                  :aria-valuenow="c.fraction == null ? undefined : Math.round(c.fraction * 100)"
                  aria-valuemin="0"
                  aria-valuemax="100"
                >
                  <div
                    class="h-full rounded-full bg-action-primary-600 dark:bg-action-primary-500 transition-[width] duration-500 motion-reduce:transition-none"
                    :class="c.fraction == null ? 'w-full opacity-60 animate-pulse motion-reduce:animate-none' : ''"
                    :style="c.fraction == null ? null : { width: Math.max(6, Math.round(c.fraction * 100)) + '%' }"
                  ></div>
                </div>
                <p class="flex justify-between gap-2 text-[12.5px] font-medium text-action-primary-700 dark:text-action-primary-300">
                  <span>{{ c.progress || 'Starting' }}…</span>
                  <span class="tabular-nums flex-none">{{ elapsed(c.since) }}</span>
                </p>
                <!-- Off by default: a proposed build waits for the owner's click on Build (ent#1343) -->
                <label v-if="c.card.action === 'job'" class="flex items-center gap-2 text-[12.5px] text-gray-900 dark:text-gray-100">
                  <input v-model="c.auto" type="checkbox" class="rounded" data-testid="assistant-auto-create" />
                  Create them here as soon as they're ready
                </label>
              </div>
              <!-- A credential: a masked field whose value goes to this instance, never to the assistant -->
              <AssistantCredentialField v-else-if="c.card.action === 'credential'" :message-id="m.id" :entry="c" :route="route" />
              <!-- Agents run on the person's own machine: the steps, written by the page (ent#1353) -->
              <AssistantLocalMove v-else-if="c.card.action === 'local-move'" />
              <div v-else-if="c.state === 'open' || c.state === 'running'" class="flex flex-wrap gap-2 pt-1">
                <BaseButton
                  size="sm"
                  :loading="c.state === 'running'"
                  :loading-label="LOADING[c.card.action] || 'Confirming…'"
                  :disabled="store.sending"
                  @click="answer(m.id, c.id, true)"
                >{{ c.button || CONFIRM[c.card.action] || 'Confirm' }}</BaseButton>
                <BaseButton
                  variant="secondary"
                  size="sm"
                  :disabled="c.state === 'running' || store.sending"
                  @click="answer(m.id, c.id, false)"
                >Not now</BaseButton>
              </div>
              <p
                v-else
                class="flex items-start gap-1.5 text-[12.5px] font-medium"
                :class="OUTCOME[c.state].tone"
              >
                <svg class="w-4 h-4 flex-none mt-px" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24" aria-hidden="true"><path :d="OUTCOME[c.state].icon" /></svg>
                <span>{{ c.reason ? `${OUTCOME[c.state].words} ${c.reason}` : OUTCOME[c.state].words }}</span>
              </p>
            </div>
            </template>
          </template>

          <!-- What it can do, offered until the person has said something: also under a line it opened with -->
          <div v-if="fresh && !store.sending && openingDecided" class="space-y-3">
            <div v-for="g in starters" :key="g.heading" class="space-y-1.5" data-testid="assistant-starters">
              <p class="font-mono text-[11px] font-semibold uppercase tracking-wider text-gray-500 dark:text-gray-400">{{ g.heading }}</p>
              <div class="flex flex-wrap gap-2">
                <BaseButton v-for="s in g.items" :key="s.label" variant="secondary" size="sm" @click="pick(s)">{{ s.label }}</BaseButton>
              </div>
            </div>
          </div>

          <!-- The turn in hand: what it said it would do, each step so far, and how long -->
          <div v-if="store.sending" class="space-y-1.5" data-testid="assistant-working">
            <div v-if="store.saying" class="pr-6"><ChatBubble role="assistant" :content="store.saying" boxed /></div>
            <ul v-if="store.steps.length" class="space-y-0.5 text-[12.5px] text-gray-600 dark:text-gray-300">
              <li v-if="store.steps.length > SHOWN_STEPS" class="text-gray-500 dark:text-gray-400">{{ store.steps.length - SHOWN_STEPS }} earlier steps</li>
              <li v-for="(step, i) in store.steps.slice(-SHOWN_STEPS)" :key="i" class="flex items-start gap-1.5">
                <svg class="w-3.5 h-3.5 flex-none mt-0.5 text-status-success-700 dark:text-status-success-400" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24" aria-hidden="true"><path d="M5 13l4 4L19 7" /></svg>
                <span>{{ step }}</span>
              </li>
            </ul>
            <p class="flex gap-2 text-[12.5px] text-gray-500 dark:text-gray-400">
              <span class="animate-pulse motion-reduce:animate-none">{{ store.steps.length ? 'Working on the next step…' : 'Thinking…' }}</span>
              <span v-if="now - store.startedAt >= 5000" class="tabular-nums">{{ elapsed(store.startedAt) }}</span>
            </p>
          </div>
        </div>

        <InlineError v-if="store.sendError" class="mx-4 mb-2" :message="store.sendError" />

        <!-- The allowance ran out -->
        <div
          v-if="store.limit && !store.account.step"
          class="mx-4 mb-2 rounded-lg border p-3 space-y-2 border-action-primary-200 dark:border-action-primary-500/40 bg-action-primary-50 dark:bg-action-primary-500/16"
          data-testid="assistant-limit"
        >
          <p class="font-medium text-gray-900 dark:text-gray-100">{{ limitWords }}</p>
          <p class="text-[12.5px] text-gray-600 dark:text-gray-300">
            <template v-if="trialEnded">Subscribe to Trinity Pro to keep using the assistant; nothing you built is stopped. Ask stays free either way.</template>
            <template v-else>
              <template v-if="resetWords">It comes back {{ resetWords }}. </template>
              <template v-if="!store.subscribed">Trinity Pro gives you much more, and bigger teams. Ask stays free either way.</template>
            </template>
          </p>
          <BaseButton v-if="!store.subscribed" size="sm" data-testid="assistant-upgrade" @click="store.openAccount()">Subscribe to Trinity Pro</BaseButton>
        </div>

        <!-- Signing in and subscribing -->
        <div
          v-if="store.account.step"
          class="mx-4 mb-2 rounded-lg border border-gray-200 dark:border-gray-700 p-3 space-y-3 text-sm"
          data-testid="assistant-account"
        >
          <div class="flex items-center justify-between gap-2">
            <p class="font-medium text-gray-900 dark:text-gray-100">Subscribe to Trinity Pro</p>
            <BaseButton variant="ghost" size="sm" :disabled="store.account.busy" @click="store.closeAccount()">Cancel</BaseButton>
          </div>

          <!-- Subscribing by code (trinity-enterprise#1331): the terms, then the code sent to the registered admin -->
          <form
            v-if="store.account.step === 'sub-terms'"
            class="space-y-3"
            data-testid="assistant-sub-terms"
            @submit.prevent="store.sendSubscriptionCode()"
          >
            <p v-if="store.account.busy && !store.account.subTerms" class="text-[12.5px] text-gray-500 dark:text-gray-400 animate-pulse motion-reduce:animate-none">Loading the subscription terms…</p>
            <InlineError v-if="store.account.error" :message="store.account.error" />
            <BaseButton
              v-if="!store.account.subTerms && store.account.error && !store.account.stuck && !store.account.busy"
              type="button"
              variant="secondary"
              size="sm"
              data-testid="assistant-sub-terms-retry"
              @click="store.reloadSubscriptionTerms()"
            >Try again</BaseButton>
            <template v-if="store.account.subTerms && !store.account.stuck">
              <p class="font-medium text-gray-900 dark:text-gray-100">
                {{ store.account.subTerms.title || 'Subscription terms' }}
                <span v-if="store.account.subTerms.version" class="font-normal text-gray-500 dark:text-gray-400">(version {{ store.account.subTerms.version }})</span>
              </p>
              <div
                v-if="store.account.subTerms.text"
                class="max-h-48 overflow-y-auto rounded-md border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 px-3 py-2 prose prose-sm dark:prose-invert max-w-none prose-headings:my-2 prose-p:my-1.5 prose-ul:my-1.5 prose-li:my-0 text-[12.5px]"
                tabindex="0"
                aria-label="Subscription terms"
                data-testid="assistant-sub-terms-text"
                v-html="renderMarkdown(store.account.subTerms.text)"
              ></div>
              <a
                v-if="webLink(store.account.subTerms.url)"
                :href="webLink(store.account.subTerms.url)"
                target="_blank"
                rel="noopener"
                class="inline-block underline text-action-primary-700 dark:text-action-primary-300"
                data-testid="assistant-sub-terms-link"
              >Read the subscription terms</a>
              <label class="flex items-start gap-2 text-gray-900 dark:text-gray-100">
                <input v-model="store.account.ticked" type="checkbox" class="mt-0.5 rounded" data-testid="assistant-sub-terms-tick" />
                <span>I accept the subscription terms.</span>
              </label>
              <p class="text-[12.5px] text-gray-600 dark:text-gray-300" data-testid="assistant-sub-address">
                The code goes to
                <span v-if="subAddress" class="font-medium">{{ subAddress }}</span>
                <template v-else>the email of this instance's registered admin</template>.
              </p>
              <BaseButton v-if="!store.account.blocked" type="submit" size="sm" :loading="store.account.busy" loading-label="Sending…" data-testid="assistant-sub-send">Send code</BaseButton>
            </template>
          </form>

          <form
            v-else-if="store.account.step === 'sub-code'"
            class="space-y-3"
            @submit.prevent="store.confirmSubscription(code, route)"
          >
            <p class="text-gray-900 dark:text-gray-100" data-testid="assistant-sub-sent">
              <template v-if="store.account.resent">A new code is on its way to</template>
              <template v-else>Enter the code sent to</template>
              <span class="font-medium">{{ store.account.sentTo || subAddress || "this instance's registered admin" }}</span>.
              <span v-if="subMinutes" class="text-gray-500 dark:text-gray-400">It works for {{ subMinutes }} minutes.</span>
            </p>
            <BaseInput
              v-model="code"
              label="Code"
              placeholder="123456"
              inputmode="numeric"
              autocomplete="one-time-code"
              maxlength="12"
              :error="store.account.error"
              data-testid="assistant-sub-code"
            />
            <div v-if="!store.account.blocked" class="flex flex-wrap gap-2">
              <BaseButton type="submit" size="sm" :loading="store.account.busy" loading-label="Checking…" data-testid="assistant-sub-confirm">Confirm</BaseButton>
              <BaseButton variant="ghost" size="sm" :disabled="store.account.busy" data-testid="assistant-sub-resend" @click="store.sendSubscriptionCode({ resend: true })">Send a new code</BaseButton>
            </div>
          </form>

          <form v-else-if="store.account.step === 'email'" class="space-y-3" @submit.prevent="store.startAccount(email)">
            <BaseInput
              v-model="email"
              type="email"
              label="Your email"
              placeholder="you@example.com"
              autocomplete="email"
              :error="store.account.error"
              data-testid="assistant-email"
            />
            <BaseButton type="submit" size="sm" :loading="store.account.busy" loading-label="Sending…">Send me a code</BaseButton>
            <p class="text-[12.5px] text-gray-500 dark:text-gray-400">This ties your subscription to your email and this instance.</p>
          </form>

          <form v-else-if="store.account.step === 'code'" class="space-y-3" @submit.prevent="store.verifyAccount(code, accepted)">
            <p v-if="store.account.stale" class="text-[12.5px] text-gray-600 dark:text-gray-300" data-testid="assistant-terms-changed">
              The terms have changed since you last accepted them. Read the current terms and accept them to continue.
            </p>
            <p class="text-gray-900 dark:text-gray-100">
              Enter the 6-digit code sent to <span class="font-medium">{{ store.account.email }}</span>.
            </p>
            <p
              v-if="store.account.devCode"
              class="rounded-md px-3 py-2 text-[12.5px] bg-status-warning-100 text-status-warning-700 dark:bg-status-warning-500/16 dark:text-status-warning-300"
              data-testid="assistant-dev-code"
            >
              Test gateway: no email is sent. Your code is <span class="font-mono font-semibold">{{ store.account.devCode }}</span>.
            </p>
            <BaseInput
              v-model="code"
              label="Code"
              placeholder="123456"
              inputmode="numeric"
              autocomplete="one-time-code"
              maxlength="12"
              :error="store.account.error"
              data-testid="assistant-code"
            />
            <label class="flex items-start gap-2 text-gray-900 dark:text-gray-100">
              <input v-model="accepted" type="checkbox" class="mt-0.5 rounded" data-testid="assistant-terms" />
              <span>
                I accept the
                <a v-if="webLink(store.account.terms?.url)" :href="webLink(store.account.terms.url)" target="_blank" rel="noopener" class="underline text-action-primary-600 dark:text-action-primary-400" data-testid="assistant-full-terms-link">Trinity terms</a>
                <button
                  v-else
                  type="button"
                  class="underline text-action-primary-700 dark:text-action-primary-300"
                  :aria-expanded="fullTermsOpen"
                  data-testid="assistant-full-terms-link"
                  @click.prevent="toggleFullTerms"
                >Trinity terms</button>
                <span class="text-gray-500 dark:text-gray-400"> (version {{ store.account.terms?.version }})</span>.
                They turn on anonymous usage sharing (counts and versions only), which you can turn off in Settings.
              </span>
            </label>
            <div
              v-if="fullTermsOpen"
              class="max-h-48 overflow-y-auto rounded-md border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 px-3 py-2"
              data-testid="assistant-full-terms-text"
            >
              <InlineError v-if="store.termsError" :message="store.termsError" retryable @retry="store.readFullTerms()" />
              <div
                v-else-if="store.fullTermsText"
                class="prose prose-sm dark:prose-invert max-w-none prose-headings:my-2 prose-p:my-1.5 prose-ul:my-1.5 prose-li:my-0 text-[12.5px]"
                v-html="renderMarkdown(store.fullTermsText)"
              ></div>
              <p v-else class="text-[12.5px] text-gray-500 dark:text-gray-400 animate-pulse motion-reduce:animate-none">Loading the terms…</p>
            </div>
            <div class="flex flex-wrap gap-2">
              <BaseButton type="submit" size="sm" :loading="store.account.busy" loading-label="Checking…">Continue</BaseButton>
              <BaseButton variant="ghost" size="sm" :disabled="store.account.busy" @click="store.backToEmail()">Use a different email</BaseButton>
            </div>
          </form>

          <div v-else-if="store.account.step === 'subscribe'" class="space-y-3">
            <p class="text-gray-900 dark:text-gray-100">Signed in as <span class="font-medium">{{ store.status.email }}</span>.</p>
            <InlineError v-if="store.account.error" :message="store.account.error" />
            <BaseButton size="sm" :loading="store.account.busy" loading-label="Starting…" data-testid="assistant-subscribe" @click="store.subscribe(route)">Start Trinity Pro</BaseButton>
          </div>

          <p v-else class="text-gray-900 dark:text-gray-100">
            Finish paying in the tab that opened, then come back here.
          </p>
        </div>

        <!-- Composer -->
        <!-- The status is being read again before the terms are offered (trinity-enterprise#1330) -->
        <p
          v-if="store.settling && !store.agreed"
          class="mx-3 mb-1 mt-3 text-[12.5px] text-gray-500 dark:text-gray-400 animate-pulse motion-reduce:animate-none shrink-0"
          aria-live="polite"
          data-testid="assistant-finishing"
        >Finishing setup…</p>
        <!-- Before the first message, once per instance: one button, no email -->
        <div
          v-if="needsAgreement"
          class="mx-3 mb-1 mt-3 rounded-lg border p-3 space-y-2 border-action-primary-200 dark:border-action-primary-500/40 bg-action-primary-50 dark:bg-action-primary-500/16 shrink-0"
          data-testid="assistant-terms-first"
        >
          <p class="text-[12.5px] text-gray-900 dark:text-gray-100">
            To answer you, your messages and details of your agents are sent to Ability AI, the company behind Trinity, which also keeps what you tell it about your work so its help fits you. Agreeing also turns on anonymous usage sharing (counts and versions only; you can turn it off in Settings). It's free to try, with no sign-up. By continuing you agree to the
            <a
              v-if="webLink(store.status?.trial_terms?.url)"
              :href="webLink(store.status.trial_terms.url)"
              target="_blank"
              rel="noopener"
              class="underline font-medium text-action-primary-700 dark:text-action-primary-300"
              data-testid="assistant-terms-link"
            >trial terms</a>
            <button
              v-else
              type="button"
              class="underline font-medium text-action-primary-700 dark:text-action-primary-300"
              :aria-expanded="termsOpen"
              data-testid="assistant-terms-link"
              @click="toggleTerms"
            >trial terms</button>.
          </p>
          <!-- The terms themselves, read here when there is no public page -->
          <div
            v-if="termsOpen"
            class="max-h-48 overflow-y-auto rounded-md border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 px-3 py-2"
            data-testid="assistant-terms-text"
          >
            <InlineError v-if="store.termsError" :message="store.termsError" retryable @retry="store.readTerms()" />
            <div
              v-else-if="store.termsText"
              class="prose prose-sm dark:prose-invert max-w-none prose-headings:my-2 prose-p:my-1.5 prose-ul:my-1.5 prose-li:my-0 text-[12.5px]"
              v-html="renderMarkdown(store.termsText)"
            ></div>
            <p v-else class="text-[12.5px] text-gray-500 dark:text-gray-400 animate-pulse motion-reduce:animate-none">Loading the terms…</p>
          </div>
          <p v-if="store.waiting" class="text-[12.5px] text-gray-600 dark:text-gray-300 truncate" data-testid="assistant-waiting">You wrote: {{ store.waiting }}</p>
          <InlineError v-if="store.agreeError" :message="store.agreeError" />
          <BaseButton size="sm" :loading="store.agreeing" loading-label="Starting…" data-testid="assistant-agree" @click="store.agree(route)">
            {{ store.waiting ? 'Agree and send' : 'Agree and start' }}
          </BaseButton>
        </div>

        <!-- It goes where the person goes: say what it is looking at, so "this agent" and "here" mean something. -->
        <p class="flex items-center gap-1.5 px-3 pt-2 text-[12px] text-gray-500 dark:text-gray-400 border-t border-gray-200 dark:border-gray-700 shrink-0" data-testid="assistant-page">
          <svg class="w-3.5 h-3.5 flex-none" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24" aria-hidden="true"><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z" /><circle cx="12" cy="12" r="3" /></svg>
          <span class="truncate">Looking at: {{ page.label }}</span>
        </p>
        <!-- Enter sends; Shift+Enter starts a new line. No button: the line under the box says so. -->
        <form class="flex flex-col gap-1 px-3 pb-2 pt-2 shrink-0" @submit.prevent="send(draft)">
          <BaseTextarea
            v-model="draft"
            class="w-full"
            :rows="3"
            aria-label="Message the assistant"
            placeholder="Ask, or tell Trinity what to do"
            maxlength="4000"
            data-testid="assistant-input"
            @keydown.enter.exact.prevent="send(draft)"
          />
          <p class="text-[11.5px] text-gray-500 dark:text-gray-400 truncate" data-testid="assistant-keys">
            <kbd class="font-sans font-medium">Enter</kbd> to send<span class="hidden md:inline"> · <kbd class="font-sans font-medium">{{ toggleKeys }}</kbd> to hide or show</span>
          </p>
        </form>
      </template>
    </template>
  </section>
  </Transition>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import BaseButton from '../base/BaseButton.vue'
import BaseInput from '../base/BaseInput.vue'
import BaseSegmented from '../base/BaseSegmented.vue'
import BaseTextarea from '../base/BaseTextarea.vue'
import InlineError from '../InlineError.vue'
import ChatBubble from '../chat/ChatBubble.vue'
import AssistantCredentialField from './AssistantCredentialField.vue'
import AssistantLocalMove from './AssistantLocalMove.vue'
import HelpChatWidget from '../HelpChatWidget.vue'
import { maskAddress, useAssistantStore } from '../../stores/assistant'
import { useOnboardingStore } from '../../stores/onboarding'
import { useFirstRunStore } from '../../stores/firstRun'
import { irreversibleTool, toolWords, webLink } from '../../utils/assistantCards'
import { describePage } from '../../utils/assistantContext'
import { starterGroups } from '../../utils/assistantStarters'
import { OPENING, coreTeamSentence, inFirstSession, isFirstSession, openingGroup } from '../../utils/firstSession'
import { renderMarkdown } from '../../utils/markdown'

defineProps({
  // A route may name tabs where the launcher would cover a page control (#3446).
  hideLauncher: { type: Boolean, default: false },
})

const TABS = [
  { value: 'trinity', label: 'Trinity' },
  { value: 'ask', label: 'Ask the docs' },
]

const OUTCOME = Object.freeze({
  done: { words: 'Done.', tone: 'text-status-success-700 dark:text-status-success-400', icon: 'M5 13l4 4L19 7' },
  declined: { words: 'Not now.', tone: 'text-gray-600 dark:text-gray-300', icon: 'M6 12h12' },
  refused: { words: "That didn't work.", tone: 'text-status-danger-700 dark:text-status-danger-400', icon: 'M6 18L18 6M6 6l12 12' },
  unsure: { words: 'Not sure it all worked.', tone: 'text-status-warning-700 dark:text-status-warning-400', icon: 'M12 8v5M12 16.5h.01' },
})
const CONFIRM = Object.freeze({ build: 'Build', upgrade: 'Continue', feedback: 'Send', 'use-claude': 'Yes, switch them', 'email-results': 'Email it to me', brain: 'Set it up' })
const LOADING = Object.freeze({ build: 'Building…', feedback: 'Sending…', 'use-claude': 'Switching…', brain: 'Starting…' })
const HEADING = Object.freeze({ build: 'Ready to build', tool: 'Cannot be undone', job: 'Being made', run: 'Running', feedback: 'For the Trinity team', credential: 'A credential, kept from the assistant', 'local-move': 'On your machine', brain: 'Came with this instance' })
const SHOWN_STEPS = 4
// ent#1390: which tool cards have their parameters open (closed by default).
const detailsOpen = reactive({})
const FOLLOWED = ['job', 'run']
// A followed card's heading follows what happened to it.
const ENDED = Object.freeze({ done: 'Finished', refused: "Didn't finish", unsure: 'Still going' })
const BUILT = Object.freeze({ running: 'Creating', done: 'Created', refused: "Couldn't create", unsure: 'Partly created' })
const heading = (c) => (FOLLOWED.includes(c.card.action) && c.state !== 'making' && ENDED[c.state])
  || (c.card.action === 'build' && BUILT[c.state])
  || (c.card.action === 'tool' && !irreversibleTool(c.card) && 'Needs your OK')
  || HEADING[c.card.action] || 'Needs your OK'


const route = useRoute()
const store = useAssistantStore()
const isOpen = ref(false)
const tab = ref('trinity')
const draft = ref('')
const email = ref('')
const code = ref('')
const accepted = ref(false)
const panelEl = ref(null)
// Where the person is, in words: told to the assistant with every message, and shown above the box.
const page = computed(() => describePage(route))
const messagesEl = ref(null)

// What the empty screen offers, for this page and this fleet.
const starters = computed(() => {
  // trinity-enterprise#1355: the assistant-led first session asks one question, with two answers.
  // ent#1353: steps already given, "My agents are here" stays once the moved agents arrive (until the first result is seen).
  if (firstSession.value || (firstSessionOn.value && store.status?.local_agents === 'offered')) {
    return [openingGroup(store.status)]
  }
  const groups = starterGroups(describePage(route), store.fleet, { known: Boolean(store.status?.profile_known) })
  // Under its own hello there is one question to answer, not a menu: the rest comes once they have.
  return store.messages.length === 1 && store.messages[0].local ? groups.slice(0, 1) : groups
})

// Nothing said by the person yet: the only messages, if any, are lines the assistant opened with.
const fresh = computed(() => store.messages.every((m) => m.opener))

// What the assistant opens with for someone it does not know: written here, sent nowhere.
// The agents a new install comes with are examples, not theirs: a new person
// is taken to an agent of their own.
function hello() {
  const seeded = firstRun.seededAgents || []
  // ent#1416: the Company Brain and the Chief of Staff came with the install
  // but are the person's team, never "examples".
  const core = firstRun.coreAgents || []
  const examples = seeded.filter((n) => !core.includes(n))
  const own = store.fleet.filter((a) => !seeded.includes(a.name)).length
  if (own) {
    return `Hi, I'm Trinity's assistant 👋\n\nYou have ${own} ${own === 1 ? 'agent' : 'agents'} here. I can check how they're doing, fix what's failing, set them to run by themselves, or build the next one with you.\n\nTell me one thing about you first, so what I suggest fits your work.`
  }
  const team = coreTeamSentence(core.filter((n) => store.fleet.some((a) => a.name === n)))
  const shown = (team ? `\n\n${team}` : '') + (examples.length
    ? `\n\nThe ${team ? 'other ' : ''}${examples.length === 1 ? 'agent' : 'agents'} on the dashboard came with Trinity as ${examples.length === 1 ? 'an example' : 'examples'}.`
    : '')
  return `Hi, I'm Trinity's assistant 👋\n\nLet's get you **an agent of your own** working in a few minutes: tell me a little about your work, I'll suggest one, build it and run it once so you see what it produces.${shown}`
}
// trinity-enterprise#1355: while the first session runs, no settings steps are raised (checkpoint 4).
const firstSessionOn = computed(() => inFirstSession(store.status, store.fleet, firstRun.seededAgents || []))
// trinity-enterprise#1355: the instance says the assistant leads, and the person has no agent of their own yet.
const firstSession = computed(() => isFirstSession(store.status, store.fleet, firstRun.seededAgents || []))
// trinity-enterprise#1356: the opening is decided once, when there is nothing
// left to learn that would change it: the reads the hello is written from are
// in, and the status has settled. Right after setup the first status is read
// before the instance's account report lands and carries no line from Trinity;
// a hello written then was replaced by Trinity's own line when the re-read
// brought it, so the first thing a new person read changed under them.
// The first-session opening waits on the same gate: decided on an early,
// incomplete status it could ask the first-session question of the wrong person.
const prepared = ref(false)      // open() has read the fleet and which agents are examples
const openingDecided = computed(() => prepared.value && Boolean(store.status) && !store.settling)
function startTalking() {
  if (!isOpen.value || !openingDecided.value || store.messages.length) return
  if (firstSession.value) return store.welcome(OPENING)
  if (store.status.profile_known) return
  store.welcome(hello())
}
watch(openingDecided, (now) => { if (now) startTalking() })

// While the setup overlay has the screen the assistant stays out of its way,
// and takes over when it closes: open beside the page where there is room,
// a hello beside its button where there is not.
const firstRun = useFirstRunStore()
watch(() => firstRun.overlayOpen, (now, was) => {
  if (now) {
    if (isOpen.value) {
      isOpen.value = false
      store.setOpen(false)          // not remembered: the person did not choose this
    }
    return
  }
  if (!was) return
  firstRun.fetchState(true)
  let chosen = null
  try {
    chosen = localStorage.getItem(OPEN_KEY)
  } catch { /* no choice known */ }
  if (wide.value && chosen !== 'false') open({ quiet: true })
  else store.peek = true
})

// Getting started. The steps and their ticks are the server's; this only
// remembers whether the list is open (closed to begin with: one line, no more).
const setup = useOnboardingStore()
const SETUP_OPEN_KEY = 'trinity-assistant-setup-open'
const setupOpen = ref(false)
try {
  setupOpen.value = localStorage.getItem(SETUP_OPEN_KEY) === 'true'
} catch { /* closed */ }
function toggleSetup() {
  setupOpen.value = !setupOpen.value
  try {
    localStorage.setItem(SETUP_OPEN_KEY, String(setupOpen.value))
  } catch { /* not remembered */ }
}
const nextStep = computed(() => setup.items.find((i) => !i.done) || null)
// The assistant does the step with them, here, instead of sending them off to a page.
function helpWith(step) {
  setupOpen.value = false
  tab.value = 'trinity'
  send(`Help me with this getting-started step: "${step.title}".`)
}

// Nothing leaves this instance until someone here has agreed to the trial's terms.
// While the status is still being settled (trinity-enterprise#1330) the box waits:
// the next read may say the terms were already accepted.
const needsAgreement = computed(() => Boolean(store.status) && !store.agreed && !store.settling)

const termsOpen = ref(false)
function toggleTerms() {
  termsOpen.value = !termsOpen.value
  if (termsOpen.value) store.readTerms()
}

const fullTermsOpen = ref(false)
// Subscribing by code: the address is only ever shown masked, and the code's life in minutes.
const subAddress = computed(() => maskAddress(store.status?.email))
const subMinutes = computed(() => {
  const seconds = Number(store.account.expiresIn)
  return seconds > 0 ? Math.round(seconds / 60) : 0
})
function toggleFullTerms() {
  fullTermsOpen.value = !fullTermsOpen.value
  if (fullTermsOpen.value) store.readFullTerms()
}

// Something is going on that the person is waiting for.
const busy = computed(() => store.sending || store.following > 0)

// A clock for elapsed times, ticking only while there is something to time.
const now = ref(Date.now())
let clock = null
watch(busy, (on) => {
  clearInterval(clock)
  clock = on ? setInterval(() => { now.value = Date.now() }, 1000) : null
}, { immediate: true })
onBeforeUnmount(() => clearInterval(clock))
function elapsed(since) {
  const s = Math.max(0, Math.round((now.value - since) / 1000))
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s`
}

// Which limit was reached, in one sentence (Abilityai/trinity-gateway#37): the trial's days
// end it for good, so nothing "comes back"; messages name their number and period.
const trialEnded = computed(() => store.limit?.reached === 'days')
const limitWords = computed(() => {
  const l = store.limit || {}
  if (trialEnded.value) return l.days_limit ? `Your ${l.days_limit}-day free trial has ended.` : 'Your free trial has ended.'
  if (store.subscribed) return "You've reached this period's allowance."
  const period = l.period_hours === 24 ? "today's" : l.period_hours === 168 ? "this week's" : 'the'
  return l.messages_limit ? `You've used ${period} ${l.messages_limit} free messages.` : "You've used the free allowance."
})

// When the allowance comes back, in the person's own words for time.
const resetWords = computed(() => {
  const at = store.limit?.resets_at ? new Date(store.limit.resets_at) : null
  if (!at || Number.isNaN(at.getTime())) return ''
  const hours = Math.max(1, Math.round((at.getTime() - Date.now()) / 3600000))
  if (hours <= 1) return 'within the hour'
  return hours < 24 ? `in about ${hours} hours` : `on ${at.toLocaleDateString(undefined, { weekday: 'long' })}`
})

// A note is what the page told the assistant; the person needs only its first sentence.
const noteWords = (content) => String(content || '').split(/(?<=[.!?])\s/)[0]

const focusInput = () => nextTick(() => panelEl.value?.querySelector('textarea[data-testid="assistant-input"]')?.focus())

// Whether the panel is open is the person's choice and is kept. With no choice
// made yet it starts open where there is room for it beside the page.
const OPEN_KEY = 'trinity-assistant-open'
function rememberOpen(open) {
  try {
    localStorage.setItem(OPEN_KEY, String(open))
  } catch { /* it simply starts collapsed next time */ }
}
// Wide enough for the page to sit beside the panel; narrower, the panel lies over it.
const WIDE = '(min-width: 1280px)'
const wide = ref(false)
let wideQuery = null
const onWide = (e) => { wide.value = e.matches }

/** `quiet` opens it without taking the keyboard: the page did it, not the person. */
async function open({ quiet = false } = {}) {
  isOpen.value = true
  store.setOpen(true)
  if (!quiet) {
    rememberOpen(true)
    focusInput()
  }
  // What the empty screen offers depends on the agents; reading them is local.
  setup.fetchChecklist(true)
  const fleet = store.messages.length ? null : store.refreshFleet().catch(() => {})
  if (!store.status) await store.loadStatus()
  await Promise.all([fleet, firstRun.fetchState()])     // which agents are the install's examples: read once, kept
  prepared.value = true
  startTalking()
  if (!quiet) focusInput()
}

// Option+Z on a Mac, Alt+Z elsewhere, opens or collapses the panel from
// anywhere, the message box included. Matched on the physical key: on a Mac
// Option turns the letter into "Ω", so e.key never says "z".
const isMac = /Mac|iPhone|iPad/i.test(navigator.userAgentData?.platform || navigator.platform || '')
const toggleKeys = isMac ? '⌥Z' : 'Alt+Z'
function onKey(e) {
  if (e.code !== 'KeyZ' || !e.altKey) return
  if (e.ctrlKey || e.metaKey || e.shiftKey || e.repeat || e.isComposing) return
  if (document.querySelector('[aria-modal="true"]')) return       // a dialog has the keyboard
  e.preventDefault()
  isOpen.value ? close() : open()
}

function close() {
  isOpen.value = false
  store.setOpen(false)
  rememberOpen(false)
}

// How wide the open panel is: the person's to choose, by dragging its edge, and
// kept. The page beside it follows (App.vue pads by the same variable).
const WIDTH_KEY = 'trinity-assistant-width'
const DEFAULT_W = 384
const MIN_W = 320
const width = ref(DEFAULT_W)
const resizing = ref(false)
// Never so wide that the page beside it has no room left.
const maxWidth = () => Math.max(MIN_W, Math.min(720, (window.innerWidth || 1280) - 560))
function setWidth(px, keep = false) {
  width.value = Math.round(Math.min(maxWidth(), Math.max(MIN_W, Number(px) || DEFAULT_W)))
  document.documentElement.style.setProperty('--assistant-w', `${width.value}px`)
  if (!keep) return
  try {
    localStorage.setItem(WIDTH_KEY, String(width.value))
  } catch { /* the usual width next time */ }
}
function startResize(e) {
  if (e.button !== 0) return
  e.preventDefault()
  resizing.value = true
  store.resizing = true
  const move = (ev) => setWidth(ev.clientX)
  const up = () => {
    resizing.value = false
    store.resizing = false
    setWidth(width.value, true)
    window.removeEventListener('pointermove', move)
    window.removeEventListener('pointerup', up)
    window.removeEventListener('pointercancel', up)
  }
  window.addEventListener('pointermove', move)
  window.addEventListener('pointerup', up)
  window.addEventListener('pointercancel', up)
}
const onWindowResize = () => setWidth(width.value)

function send(text) {
  const message = String(text || '').trim()
  if (!message || store.sending) return
  draft.value = ''
  // Kept until they agree; one click sends it. While the status is still being read
  // again (trinity-enterprise#1330) it is held too: nobody has agreed yet.
  if (needsAgreement.value || (store.settling && !store.agreed)) return store.hold(message)
  store.send(message, route)
}

// A starter: sent as it stands, or put in the box for the person to finish.
function pick(starter) {
  if (!starter.fill) return send(starter.text)
  draft.value = starter.text
  focusInput()
}

// A result opens beside the panel; the first one in an assistant-led session opens by itself (ent#1355).
const router = useRouter()
function openResult(path) {
  router.push(path).catch?.(() => {})
}
watch(() => store.openResult, (path) => {
  if (!path) return
  store.openResult = ''
  openResult(path)
})

function answer(messageId, cardId, confirm) {
  store.answerCard(messageId, cardId, confirm, route)
}

let stopKeeping = null
onMounted(() => {
  if (!store.status) store.loadStatus()
  setup.fetchChecklist()
  store.restore(route)
  stopKeeping = store.$subscribe(() => store.keep())
  let savedWidth = null
  try {
    savedWidth = localStorage.getItem(WIDTH_KEY)
  } catch { /* the usual width */ }
  setWidth(savedWidth || DEFAULT_W)
  window.addEventListener('resize', onWindowResize)
  window.addEventListener('keydown', onKey)
  wideQuery = typeof window.matchMedia === 'function' ? window.matchMedia(WIDE) : null
  wide.value = Boolean(wideQuery?.matches)
  wideQuery?.addEventListener?.('change', onWide)
  let chosen = null
  try {
    chosen = localStorage.getItem(OPEN_KEY)
  } catch { /* no choice known */ }
  firstRun.fetchState()
  if (wide.value && chosen !== 'false' && !firstRun.overlayOpen) open({ quiet: true })
})

// trinity-enterprise#1355: where the assistant leads the first session it opens by
// itself, whatever the width, unless the person closed it before.
watch(() => store.status?.leads_setup, async (leads) => {
  if (!leads || isOpen.value) return
  let chosen = null
  try {
    chosen = localStorage.getItem(OPEN_KEY)
  } catch { /* no choice known */ }
  if (chosen === 'false' || firstRun.overlayOpen) return
  await Promise.all([store.refreshFleet().catch(() => {}), firstRun.fetchState()])
  if (firstSession.value && !isOpen.value) open({ quiet: true })
}, { immediate: true })
onBeforeUnmount(() => {
  stopKeeping?.()
  wideQuery?.removeEventListener?.('change', onWide)
  window.removeEventListener('resize', onWindowResize)
  window.removeEventListener('keydown', onKey)
  store.setOpen(false)
  store.stopSettling()
})

// The re-read settled and the terms were already accepted: what was held goes now.
watch(() => store.settling, (on, was) => {
  if (!was || on || !store.agreed || !store.waiting) return
  const held = store.waiting
  store.waiting = ''
  store.send(held, route)
})

// What the assistant just did may have ticked a step: read them again when a turn ends.
watch(() => store.sending, (on, was) => { if (was && !on) setup.fetchChecklist(true) })

// A new conversation with someone it does not know starts the same way.
watch(() => store.messages.length, (n) => { if (!n) startTalking() })

// Moving about the app is a chance to hear of a next step, at most every ten minutes.
watch(() => route.path, () => store.recheck())

watch(() => store.account.step, (step) => {
  code.value = ''
  if (step !== 'code') accepted.value = false
  // A code that could not be sent to a known address leaves that address filled in.
  if (step === 'email' && store.account.email) email.value = store.account.email
})

watch(
  () => [store.messages.length, store.sending, store.steps.length, store.saying, store.limit, store.account.step],
  () => nextTick(() => {
    const el = messagesEl.value
    if (el) el.scrollTop = el.scrollHeight
  })
)
</script>

<style scoped>
/* The launcher sits a little above the page: a soft contact shadow under it. */
.assistant-orb-ground {
  position: absolute;
  left: 15%;
  right: 15%;
  bottom: -9px;
  height: 8px;
  border-radius: 9999px;
  background: theme('colors.action-primary.700 / 35%');
  filter: blur(5px);
  pointer-events: none;
}
/* The corner under the logo is part of the top bar: its shadow falls only below it. */
.assistant-corner {
  clip-path: inset(0 0 -8px 0);
}
/* The rail's name, read bottom to top like the spine of a book. */
.assistant-rail-label {
  writing-mode: vertical-rl;
  transform: rotate(180deg);
  text-align: right;
  padding-bottom: 4px;
}
/* The panel slides out of the edge it is docked to, and back into it. */
.assistant-dock-enter-active {
  transition: transform 0.22s cubic-bezier(0.2, 0.8, 0.2, 1), opacity 0.18s ease-out;
}
.assistant-dock-leave-active {
  transition: transform 0.16s ease-in, opacity 0.16s ease-in;
}
.assistant-dock-enter-from,
.assistant-dock-leave-to {
  opacity: 0;
  transform: translateX(-32px);
}
@media (prefers-reduced-motion: reduce) {
  .assistant-dock-enter-active,
  .assistant-dock-leave-active { transition: opacity 0.1s linear; }
  .assistant-dock-enter-from,
  .assistant-dock-leave-to { transform: none; }
}

/* The button arrives: it grows from a point with a small overshoot, like a bubble surfacing. */
.assistant-orb {
  animation: assistant-orb-in 0.5s cubic-bezier(0.34, 1.56, 0.64, 1) backwards;   /* backwards only: the hover lift keeps working after it */
}
@keyframes assistant-orb-in {
  from { opacity: 0; transform: scale(0.3) translateY(12px); }
  60% { opacity: 1; }
  to { opacity: 1; transform: scale(1) translateY(0); }
}
@media (prefers-reduced-motion: reduce) {
  .assistant-orb { animation: none; }
}

/* The hello arrives a moment after the page, so it reads as something said. */
.assistant-peek {
  animation: assistant-peek 0.4s ease-out 1.2s both;
}
@keyframes assistant-peek {
  from { opacity: 0; transform: translateY(6px); }
  to { opacity: 1; transform: translateY(0); }
}
@media (prefers-reduced-motion: reduce) {
  .assistant-peek { animation: none; }
}

/* One slow ripple, then a long rest. Sooner when an answer is waiting. */
.assistant-halo {
  position: absolute;
  inset: 0;
  border-radius: 9999px;
  border: 2px solid theme('colors.action-primary.400');
  opacity: 0;
  pointer-events: none;
  animation: assistant-halo 14s ease-out infinite;
}
.assistant-halo.eager {
  animation-duration: 4.5s;
}
@keyframes assistant-halo {
  0% { transform: scale(1); opacity: 0; }
  3% { opacity: 0.55; }
  18% { transform: scale(1.6); opacity: 0; }
  100% { transform: scale(1.6); opacity: 0; }
}
@media (prefers-reduced-motion: reduce) {
  .assistant-halo { animation: none; }
}
</style>
