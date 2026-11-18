<template>
  <!-- Launcher: the same corner the help button uses -->
  <button
    v-if="!isOpen && !hideLauncher"
    type="button"
    data-testid="assistant-launcher"
    class="fixed bottom-6 right-6 w-14 h-14 bg-action-primary-600 hover:bg-action-primary-700 text-white rounded-full shadow-lg flex items-center justify-center transition-transform hover:scale-105 motion-reduce:transition-none motion-reduce:hover:scale-100 focus:outline-none focus:ring-2 focus:ring-action-primary-500 focus:ring-offset-2 dark:focus:ring-offset-gray-900 z-50"
    :aria-label="store.unread ? 'Open the Trinity assistant: it has an answer for you' : busy ? 'Open the Trinity assistant: it is working' : 'Open the Trinity assistant'"
    @click="open"
  >
    <!-- Working while closed: a slow ring. An answer waiting: a count. -->
    <span
      v-if="busy && !store.unread"
      class="absolute inset-0 rounded-full ring-2 ring-action-primary-400 animate-ping motion-reduce:animate-none"
      data-testid="assistant-launcher-busy"
      aria-hidden="true"
    ></span>
    <img src="../../assets/trinity-logo-white.svg" alt="" class="w-7 h-7" />
    <span
      v-if="store.unread"
      class="absolute -top-1 -right-1 min-w-5 h-5 px-1 rounded-full bg-status-danger-600 text-white text-[11px] font-bold tabular-nums flex items-center justify-center ring-2 ring-white dark:ring-gray-900"
      data-testid="assistant-launcher-unread"
      aria-hidden="true"
    >{{ store.unread }}</span>
  </button>

  <section
    v-if="isOpen"
    ref="panelEl"
    data-testid="assistant-panel"
    class="fixed top-20 bottom-6 right-6 w-[27rem] max-w-[calc(100vw-2rem)] bg-white dark:bg-gray-800 rounded-xl shadow-2xl flex flex-col z-50 border border-gray-200 dark:border-gray-700 overflow-hidden"
    role="dialog"
    aria-label="Trinity assistant"
    @keydown.escape="close"
  >
    <!-- Header -->
    <header class="flex items-center gap-2.5 pl-4 pr-2 py-3 bg-action-primary-600 text-white shrink-0">
      <span class="w-8 h-8 rounded-full bg-white/15 flex items-center justify-center flex-none">
        <img src="../../assets/trinity-logo-white.svg" alt="" class="w-5 h-5" />
      </span>
      <div class="flex-1 min-w-0">
        <p class="flex items-center gap-1.5 text-[15px] font-semibold leading-tight">
          Trinity
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
        @click="close"
      >
        <svg class="w-[18px] h-[18px]" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 18L18 6M6 6l12 12" /></svg>
      </button>
    </header>

    <!-- Two tabs: the assistant, and the documentation's Ask -->
    <div class="px-4 py-2 border-b border-gray-200 dark:border-gray-700 shrink-0">
      <BaseSegmented v-model="tab" :options="TABS" aria-label="What to talk to" />
    </div>

    <!-- Ask: free, always there, needs nothing -->
    <div v-show="tab === 'ask'" class="flex flex-col flex-1 min-h-0" data-testid="assistant-ask">
      <HelpChatWidget embedded />
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
          <div v-if="store.messages.length === 0" class="space-y-3" data-testid="assistant-welcome">
            <p class="text-gray-900 dark:text-gray-100">
              I can look at your agents and tell you what's going on, fix what I can, and build new agents with you. It works right away: there's nothing to set up.
            </p>
            <p class="text-[12.5px] text-gray-500 dark:text-gray-400">
              Anything that switches something on or gives access, I'll ask you to confirm first.
            </p>
            <div v-for="g in starters" :key="g.heading" class="space-y-1.5" data-testid="assistant-starters">
              <p class="font-mono text-[11px] font-semibold uppercase tracking-wider text-gray-500 dark:text-gray-400">{{ g.heading }}</p>
              <div class="flex flex-wrap gap-2">
                <BaseButton v-for="s in g.items" :key="s.label" variant="secondary" size="sm" @click="pick(s)">{{ s.label }}</BaseButton>
              </div>
            </div>
          </div>

          <template v-for="m in store.messages" :key="m.id">
            <p
              v-if="m.role === 'note'"
              class="text-[12.5px] text-gray-500 dark:text-gray-400 text-center"
              data-testid="assistant-note"
            >{{ noteWords(m.content) }}</p>
            <ChatBubble v-else-if="m.content" :role="m.role" :content="m.content" boxed />

            <div
              v-for="c in m.cards || []"
              :key="c.id"
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
              <!-- A change that cannot be undone: exactly what will run, in the page's own words -->
              <dl v-if="c.card.action === 'tool'" class="text-[12.5px] text-gray-900 dark:text-gray-100 space-y-0.5" data-testid="assistant-tool">
                <div v-for="d in toolWords(c.card).details" :key="d.key" class="flex gap-2">
                  <dt class="text-gray-500 dark:text-gray-400 flex-none">{{ d.key }}</dt>
                  <dd class="min-w-0 break-words">{{ d.value }}</dd>
                </div>
              </dl>
              <p v-if="!(FOLLOWED.includes(c.card.action) && c.state !== 'making')" class="text-[12.5px] text-gray-600 dark:text-gray-300">{{ c.note }}</p>

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
              </div>
              <div v-else-if="c.state === 'open' || c.state === 'running'" class="flex flex-wrap gap-2 pt-1">
                <BaseButton
                  size="sm"
                  :loading="c.state === 'running'"
                  :loading-label="c.card.action === 'build' ? 'Building…' : 'Confirming…'"
                  :disabled="store.sending"
                  @click="answer(m.id, c.id, true)"
                >{{ CONFIRM[c.card.action] || 'Confirm' }}</BaseButton>
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

          <!-- The turn in hand: what it said it would do, each step so far, and how long -->
          <div v-if="store.sending" class="space-y-1.5" data-testid="assistant-working">
            <ChatBubble v-if="store.saying" role="assistant" :content="store.saying" boxed />
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
          <p class="font-medium text-gray-900 dark:text-gray-100">
            {{ store.subscribed ? "You've reached this period's allowance." : "You've used the free allowance." }}
          </p>
          <p class="text-[12.5px] text-gray-600 dark:text-gray-300">
            <template v-if="resetWords">It comes back {{ resetWords }}. </template>
            <template v-if="!store.subscribed">Trinity Pro gives you much more, and bigger teams. Ask stays free either way.</template>
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

          <form v-if="store.account.step === 'email'" class="space-y-3" @submit.prevent="store.startAccount(email)">
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
                <a v-if="store.account.terms?.url" :href="store.account.terms.url" target="_blank" rel="noopener" class="underline text-action-primary-600 dark:text-action-primary-400">Trinity terms</a>
                <template v-else>Trinity terms</template>
                <span class="text-gray-500 dark:text-gray-400"> (version {{ store.account.terms?.version }})</span>.
              </span>
            </label>
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
        <!-- Before the first message, once per instance: one button, no email -->
        <div
          v-if="needsAgreement"
          class="mx-3 mb-1 mt-3 rounded-lg border p-3 space-y-2 border-action-primary-200 dark:border-action-primary-500/40 bg-action-primary-50 dark:bg-action-primary-500/16 shrink-0"
          data-testid="assistant-terms-first"
        >
          <p class="text-[12.5px] text-gray-900 dark:text-gray-100">
            To answer you, your messages and details of your agents are sent to Ability AI, the company behind Trinity. It's free to try, with no sign-up. By continuing you agree to the
            <a
              v-if="store.status?.trial_terms?.url"
              :href="store.status.trial_terms.url"
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

        <!-- Enter sends; Shift+Enter starts a new line -->
        <form class="flex items-end gap-2 p-3 border-t border-gray-200 dark:border-gray-700 shrink-0" @submit.prevent="send(draft)">
          <BaseTextarea
            v-model="draft"
            class="flex-1 min-w-0"
            :rows="3"
            aria-label="Message the assistant"
            placeholder="Ask, or tell Trinity what to do"
            maxlength="4000"
            data-testid="assistant-input"
            @keydown.enter.exact.prevent="send(draft)"
          />
          <BaseButton type="submit" :disabled="store.sending || !draft.trim()">Send</BaseButton>
        </form>
      </template>
    </template>
  </section>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import BaseButton from '../base/BaseButton.vue'
import BaseInput from '../base/BaseInput.vue'
import BaseSegmented from '../base/BaseSegmented.vue'
import BaseTextarea from '../base/BaseTextarea.vue'
import InlineError from '../InlineError.vue'
import ChatBubble from '../chat/ChatBubble.vue'
import HelpChatWidget from '../HelpChatWidget.vue'
import { useAssistantStore } from '../../stores/assistant'
import { toolWords } from '../../utils/assistantCards'
import { describePage } from '../../utils/assistantContext'
import { starterGroups } from '../../utils/assistantStarters'
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
const CONFIRM = Object.freeze({ build: 'Build', upgrade: 'Continue' })
const HEADING = Object.freeze({ build: 'Ready to build', tool: 'Cannot be undone', job: 'Being made', run: 'Running' })
const SHOWN_STEPS = 4
const FOLLOWED = ['job', 'run']
// A followed card's heading follows what happened to it.
const ENDED = Object.freeze({ done: 'Finished', refused: "Didn't finish", unsure: 'Still going' })
const heading = (c) => (FOLLOWED.includes(c.card.action) && c.state !== 'making' && ENDED[c.state]) || HEADING[c.card.action] || 'Needs your OK'


const route = useRoute()
const store = useAssistantStore()
const isOpen = ref(false)
const tab = ref('trinity')
const draft = ref('')
const email = ref('')
const code = ref('')
const accepted = ref(false)
const panelEl = ref(null)
const messagesEl = ref(null)

// What the empty screen offers, for this page and this fleet.
const starters = computed(() => starterGroups(describePage(route), store.fleet))

// Nothing leaves this instance until someone here has agreed to the trial's terms.
const needsAgreement = computed(() => Boolean(store.status) && !store.agreed)

const termsOpen = ref(false)
function toggleTerms() {
  termsOpen.value = !termsOpen.value
  if (termsOpen.value) store.readTerms()
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

async function open() {
  isOpen.value = true
  store.setOpen(true)
  focusInput()
  // What the empty screen offers depends on the agents; reading them is local.
  if (!store.messages.length) store.refreshFleet().catch(() => {})
  if (!store.status) await store.loadStatus()
  focusInput()
}

function close() {
  isOpen.value = false
  store.setOpen(false)
}

function send(text) {
  const message = String(text || '').trim()
  if (!message || store.sending) return
  draft.value = ''
  if (needsAgreement.value) return store.hold(message)     // kept until they agree; one click sends it
  store.send(message, route)
}

// A starter: sent as it stands, or put in the box for the person to finish.
function pick(starter) {
  if (!starter.fill) return send(starter.text)
  draft.value = starter.text
  focusInput()
}

function answer(messageId, cardId, confirm) {
  store.answerCard(messageId, cardId, confirm, route)
}

onMounted(() => { if (!store.status) store.loadStatus() })

watch(() => store.account.step, (step) => {
  code.value = ''
  if (step !== 'code') accepted.value = false
})

watch(
  () => [store.messages.length, store.sending, store.steps.length, store.saying, store.limit, store.account.step],
  () => nextTick(() => {
    const el = messagesEl.value
    if (el) el.scrollTop = el.scrollHeight
  })
)
</script>
