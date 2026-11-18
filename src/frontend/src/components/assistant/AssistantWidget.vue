<template>
  <!-- Launcher: the same corner the help button uses -->
  <button
    v-if="!isOpen && !hideLauncher"
    type="button"
    data-testid="assistant-launcher"
    class="fixed bottom-6 right-6 w-14 h-14 bg-action-primary-600 hover:bg-action-primary-700 text-white rounded-full shadow-lg flex items-center justify-center transition-transform hover:scale-105 motion-reduce:transition-none motion-reduce:hover:scale-100 focus:outline-none focus:ring-2 focus:ring-action-primary-500 focus:ring-offset-2 dark:focus:ring-offset-gray-900 z-50"
    aria-label="Open the Trinity assistant"
    @click="open"
  >
    <img src="../../assets/trinity-logo-white.svg" alt="" class="w-7 h-7" />
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
          <span v-if="store.status?.plan === 'pro'" class="px-1.5 py-0.5 text-[10px] font-bold leading-none rounded bg-white/20">PRO</span>
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

    <!-- What it can see -->
    <p v-if="store.ready && !store.atDoor" class="flex items-center gap-2 px-4 py-2 bg-gray-100 dark:bg-gray-750 border-b border-gray-200 dark:border-gray-700 text-[12.5px] text-gray-600 dark:text-gray-300 shrink-0">
      <svg class="w-4 h-4 flex-none text-action-primary-600 dark:text-action-primary-400" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24" aria-hidden="true"><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z" /><circle cx="12" cy="12" r="3" /></svg>
      <span class="truncate" data-testid="assistant-context">Sees what you see: {{ contextLine }}</span>
    </p>

    <!-- Not loaded yet, or the load failed -->
    <div v-if="!store.status" class="flex-1 min-h-0 p-4 space-y-3">
      <InlineError v-if="store.statusError" :message="store.statusError" retryable @retry="store.loadStatus()" />
      <div v-else aria-busy="true" class="space-y-3">
        <span class="sr-only">Loading</span>
        <div class="h-4 w-2/3 rounded bg-gray-100 dark:bg-gray-750 animate-pulse motion-reduce:animate-none"></div>
        <div class="h-10 rounded-md bg-gray-100 dark:bg-gray-750 animate-pulse motion-reduce:animate-none"></div>
      </div>
    </div>

    <!-- The door: who serves the assistant -->
    <div v-else-if="store.atDoor" class="flex-1 min-h-0 overflow-y-auto p-4 space-y-4 text-sm" data-testid="assistant-door">
      <template v-if="store.status.gateway_configured">
        <div class="space-y-1">
          <p class="text-[18px] font-semibold text-gray-900 dark:text-gray-100">Start with Trinity</p>
          <p class="text-gray-600 dark:text-gray-300">
            Trinity runs on our model, so it works right away, before you connect anything. The free plan builds your first agents with you.
          </p>
        </div>

        <form v-if="store.door.step === 'email'" class="space-y-3" @submit.prevent="store.register(email)">
          <BaseInput
            v-model="email"
            type="email"
            label="Your email"
            placeholder="you@example.com"
            autocomplete="email"
            :error="store.door.error"
            data-testid="assistant-email"
          />
          <BaseButton type="submit" :loading="store.door.busy" loading-label="Sending…">Send me a code</BaseButton>
          <p class="text-[12.5px] text-gray-500 dark:text-gray-400">
            This registers this instance to your email with Trinity. Your messages and the state of your agents are sent to Trinity's model so it can answer.
          </p>
        </form>

        <form v-else class="space-y-3" @submit.prevent="store.verify(code)">
          <p class="text-gray-900 dark:text-gray-100">
            Enter the 6-digit code sent to <span class="font-medium">{{ store.door.email }}</span>.
          </p>
          <p
            v-if="store.door.devCode"
            class="rounded-md px-3 py-2 text-[12.5px] bg-status-warning-100 text-status-warning-700 dark:bg-status-warning-500/16 dark:text-status-warning-300"
            data-testid="assistant-dev-code"
          >
            Mock gateway: no email is sent. Your code is <span class="font-mono font-semibold">{{ store.door.devCode }}</span>.
          </p>
          <BaseInput
            v-model="code"
            label="Code"
            placeholder="123456"
            inputmode="numeric"
            autocomplete="one-time-code"
            maxlength="12"
            :error="store.door.error"
            data-testid="assistant-code"
          />
          <div class="flex flex-wrap gap-2">
            <BaseButton type="submit" :loading="store.door.busy" loading-label="Checking…">Start</BaseButton>
            <BaseButton variant="ghost" :disabled="store.door.busy" @click="store.backToEmail()">Use a different email</BaseButton>
          </div>
        </form>
      </template>
      <p v-else class="text-gray-600 dark:text-gray-300">
        This instance has no Trinity gateway configured, so the assistant runs on your own Claude.
      </p>

      <div class="pt-4 border-t border-gray-200 dark:border-gray-700 space-y-2">
        <template v-if="store.mode === 'local'">
          <BaseButton variant="secondary" size="sm" :disabled="store.door.busy" @click="store.closeDoor()">Back to the basic helper</BaseButton>
        </template>
        <template v-else>
          <BaseButton variant="secondary" size="sm" :disabled="store.door.busy" data-testid="assistant-own-claude" @click="store.useOwnClaude()">
            Use my own Claude instead
          </BaseButton>
          <p class="text-[12.5px] text-gray-500 dark:text-gray-400">
            The basic helper. It runs on this instance's Claude subscription or API key, with no agent recipes or guided builds.
          </p>
        </template>
        <InlineError v-if="!store.status.gateway_configured && store.door.error" :message="store.door.error" />
      </div>
    </div>

    <!-- Setting up -->
    <div v-else-if="!store.ready" class="flex-1 min-h-0 p-4 space-y-4">
      <InlineError
        v-if="store.prepareError"
        :message="store.prepareError"
        retryable
        @retry="store.prepare()"
      />
      <div v-else aria-busy="true" class="space-y-3">
        <p class="text-sm text-gray-600 dark:text-gray-300">Setting up your assistant. The first time takes about a minute.</p>
        <span class="sr-only">Loading</span>
        <div class="h-4 w-3/4 rounded bg-gray-100 dark:bg-gray-750 animate-pulse motion-reduce:animate-none"></div>
        <div class="h-4 w-1/2 rounded bg-gray-100 dark:bg-gray-750 animate-pulse motion-reduce:animate-none"></div>
        <div class="h-16 rounded-lg bg-gray-100 dark:bg-gray-750 animate-pulse motion-reduce:animate-none"></div>
      </div>
    </div>

    <template v-else>
      <!-- Conversation -->
      <div ref="messagesEl" class="flex-1 min-h-0 overflow-y-auto p-4 space-y-3 text-sm" aria-live="polite">
        <div v-if="store.messages.length === 0" class="space-y-3">
          <p class="text-gray-900 dark:text-gray-100">
            I can answer questions about Trinity, set things up for you, and build agents with you.
          </p>
          <p class="text-[12.5px] text-gray-500 dark:text-gray-400">
            Anything that gives access, I'll ask you to confirm first.
          </p>
          <div class="flex flex-wrap gap-2">
            <BaseButton v-for="s in starters" :key="s" variant="secondary" size="sm" @click="send(s)">{{ s }}</BaseButton>
          </div>
          <div v-if="store.catalog.templates.length" class="space-y-1.5 pt-1" data-testid="assistant-recipes">
            <p class="font-mono text-[11px] font-semibold uppercase tracking-wider text-gray-500 dark:text-gray-400">New recipes from Trinity</p>
            <button
              v-for="t in store.catalog.templates"
              :key="t.id"
              type="button"
              class="block w-full text-left rounded-lg border border-gray-200 dark:border-gray-700 px-3 py-2 hover:bg-gray-100 dark:hover:bg-gray-750 focus:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40"
              :disabled="store.sending"
              @click="send(`Build the ${t.name} agent with me.`)"
            >
              <span class="block font-medium text-gray-900 dark:text-gray-100">{{ t.name }}</span>
              <span class="block text-[12.5px] text-gray-600 dark:text-gray-300">{{ t.summary }}</span>
            </button>
          </div>
          <p v-if="store.mode === 'local' && store.status.gateway_configured" class="text-[12.5px] text-gray-500 dark:text-gray-400">
            You're on the basic helper.
            <button type="button" class="underline text-action-primary-600 dark:text-action-primary-400" data-testid="assistant-start-pro" @click="store.openDoor()">Start with Trinity</button>
            for agent recipes and guided builds.
          </p>
        </div>

        <template v-for="m in store.messages" :key="m.id">
          <p
            v-if="m.role === 'note'"
            class="text-[12.5px] text-gray-500 dark:text-gray-400 text-center"
            data-testid="assistant-note"
          >{{ m.content }}</p>
          <ChatBubble v-else :role="m.role" :content="m.content" />

          <div
            v-for="c in m.cards || []"
            :key="c.id"
            data-testid="assistant-card"
            class="rounded-lg border p-3 space-y-2 border-action-primary-200 dark:border-action-primary-500/40 bg-action-primary-50 dark:bg-action-primary-500/16"
          >
            <p class="font-mono text-[11px] font-semibold uppercase tracking-wider text-action-primary-700 dark:text-action-primary-300">
              Needs your OK
            </p>
            <p class="font-medium text-gray-900 dark:text-gray-100">{{ c.label }}</p>
            <p class="text-[12.5px] text-gray-600 dark:text-gray-300">
              {{ c.note }}<template v-if="c.card.action === 'upgrade' && store.status?.gateway_mock"> Mock checkout: no payment is taken.</template>
            </p>

            <div v-if="c.state === 'open' || c.state === 'running'" class="flex flex-wrap gap-2 pt-1">
              <BaseButton
                size="sm"
                :loading="c.state === 'running'"
                loading-label="Confirming…"
                :disabled="store.sending"
                @click="answer(m.id, c.id, true)"
              >Confirm</BaseButton>
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
              <span>{{ OUTCOME[c.state].words }}<template v-if="c.reason"> {{ c.reason }}</template></span>
            </p>
          </div>
        </template>

        <p v-if="store.sending" class="text-[12.5px] text-gray-500 dark:text-gray-400 animate-pulse motion-reduce:animate-none" data-testid="assistant-working">
          Working on it…
        </p>
      </div>

      <InlineError v-if="store.sendError" class="mx-4 mb-2" :message="store.sendError" />

      <div
        v-if="store.limitReached && !upgradeCardOpen"
        class="mx-4 mb-2 rounded-lg border px-3 py-2 flex items-center gap-3 border-action-primary-200 dark:border-action-primary-500/40 bg-action-primary-50 dark:bg-action-primary-500/16"
        data-testid="assistant-limit"
      >
        <p class="flex-1 text-[12.5px] text-gray-900 dark:text-gray-100">
          Free allowance used: {{ store.status.agents_built }} of {{ store.status.agent_limit }} agents built. I can still answer questions.
          <template v-if="store.status.gateway_mock">Mock checkout: no payment is taken.</template>
        </p>
        <BaseButton size="sm" :loading="store.upgrading" loading-label="Upgrading…" @click="store.upgrade(route)">Upgrade</BaseButton>
      </div>

      <!-- Composer -->
      <form class="flex items-start gap-2 p-3 border-t border-gray-200 dark:border-gray-700 shrink-0" @submit.prevent="send(draft)">
        <BaseInput
          ref="inputEl"
          v-model="draft"
          class="flex-1 min-w-0"
          aria-label="Message the assistant"
          placeholder="Ask, or tell Trinity what to do"
          maxlength="4000"
          autocomplete="off"
          data-testid="assistant-input"
        />
        <BaseButton type="submit" :disabled="store.sending || !draft.trim()">Send</BaseButton>
      </form>
    </template>
  </section>
</template>

<script setup>
import { computed, nextTick, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import BaseButton from '../base/BaseButton.vue'
import BaseInput from '../base/BaseInput.vue'
import InlineError from '../InlineError.vue'
import ChatBubble from '../chat/ChatBubble.vue'
import { useAssistantStore } from '../../stores/assistant'
import { describePage } from '../../utils/assistantContext'

defineProps({
  // A route may name tabs where the launcher would cover a page control (#3446).
  hideLauncher: { type: Boolean, default: false },
})

const OUTCOME = Object.freeze({
  done: { words: 'Done.', tone: 'text-status-success-700 dark:text-status-success-400', icon: 'M5 13l4 4L19 7' },
  declined: { words: 'Not now.', tone: 'text-gray-600 dark:text-gray-300', icon: 'M6 12h12' },
  refused: { words: "That didn't work.", tone: 'text-status-danger-700 dark:text-status-danger-400', icon: 'M6 18L18 6M6 6l12 12' },
  unsure: { words: 'Not sure it worked.', tone: 'text-status-warning-700 dark:text-status-warning-400', icon: 'M12 8v5M12 16.5h.01' },
})

const starters = ['What can you do?', 'Build an agent with me', 'How are my agents doing?']

const route = useRoute()
const store = useAssistantStore()
const isOpen = ref(false)
const draft = ref('')
const email = ref('')
const code = ref('')
const panelEl = ref(null)
const messagesEl = ref(null)
const inputEl = ref(null)

const contextLine = computed(() => {
  const count = store.fleet.length
  return `${describePage(route).label} · ${count} ${count === 1 ? 'agent' : 'agents'}`
})

// One primary action per view: while the assistant's own upgrade card is
// waiting for an answer, the banner that offers the same thing stays away.
const upgradeCardOpen = computed(() =>
  store.messages.some((m) => (m.cards || []).some((c) => c.card.action === 'upgrade' && (c.state === 'open' || c.state === 'running')))
)

const focusInput = () => nextTick(() => panelEl.value?.querySelector('input')?.focus())

async function open() {
  isOpen.value = true
  focusInput()
  await store.loadStatus()
  store.prepare() // a no-op until a mode is chosen at the door
  focusInput()
}

function close() {
  isOpen.value = false
}

function send(text) {
  const message = String(text || '').trim()
  if (!message || store.sending) return
  draft.value = ''
  store.send(message, route)
}

function answer(messageId, cardId, confirm) {
  store.answerCard(messageId, cardId, confirm, route)
}

watch(() => store.ready, (ready) => { if (ready && isOpen.value) focusInput() })
watch(() => store.door.step, () => { code.value = ''; if (isOpen.value) focusInput() })

watch(
  () => [store.messages.length, store.sending],
  () => nextTick(() => {
    const el = messagesEl.value
    if (el) el.scrollTop = el.scrollHeight
  })
)
</script>
